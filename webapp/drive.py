"""Google Drive client — a real user's OAuth credential (not a service
account), walks a nested client/batch/video tree, reads each video's raw/
subfolder, writes to its cut/ subfolder.

Deliberately NOT a service account: service accounts have zero personal
storage quota, and Drive attributes a newly-created file's storage to
whoever's credential created it — so a service account uploading into a
folder it doesn't own fails with storageQuotaExceeded (reads/lists/
downloads are unaffected; only creating new files consumes quota). Google's
own fix (Shared Drives, or domain-wide delegation) both require Google
Workspace. This runs on personal Google accounts instead, so uploads use a
real user's OAuth refresh token — see get_drive_refresh_token.py — and
count against that account's real quota, same as uploading by hand. That
also means Drive access is decoupled from whoever's logged into the
dashboard (see auth.py): a session expiring mid-render can't orphan a job.
The Drive account (whichever one authorized get_drive_refresh_token.py)
just needs to be a member of (or shared on) the single root folder — Drive
permissions inherit down the whole tree, current and future subfolders
alike.

Project convention: an arbitrary-depth tree (e.g. CLIENT/BATCH/VIDEO) where
a "project" is any folder that directly contains both a raw/ and a cut/
subfolder (case-insensitive). Recursion stops at the first such folder found
on a branch — nothing nests a project inside another project.

Thread safety: worker.py runs each job's Drive calls in its own thread (via
asyncio.to_thread) so N jobs' downloads/uploads genuinely overlap — that's
the whole point of the concurrency model. googleapiclient's default
transport (httplib2) is NOT thread-safe, so a single shared service object
reused across threads corrupts connection state under real concurrency —
this surfaces as an intermittent "[SSL] record layer failure", not a clean
error. Every public method here builds its own fresh service object instead
of sharing one; credentials themselves are safe to share (self._creds).
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
MAX_WALK_DEPTH = 6

RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY_S = 1.0


def _with_retry(fn, *args, **kwargs):
    """Google's own docs recommend retrying transient errors (dropped
    connections, SSL record-layer failures, 5xx) on chunked download/upload
    and list calls — these happen routinely with httplib2's transport, not
    just under the thread-safety bug above."""
    delay = RETRY_BASE_DELAY_S
    last_err: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - deliberately broad: SSLError, socket errors, HttpError all apply
            last_err = e
            if attempt == RETRY_ATTEMPTS - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise last_err


class DriveClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        """refresh_token comes from a one-time run of get_drive_refresh_token.py
        by whichever real Google account should own uploaded files. No access
        token is needed up front — google-auth fetches one automatically on
        first use and refreshes it as needed from here on."""
        self._creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )

    def _service(self) -> Resource:
        """A fresh service (and transport) per call — see module docstring."""
        return build("drive", "v3", credentials=self._creds, cache_discovery=False)

    def _list_subfolders(self, svc: Resource, folder_id: str) -> list[dict]:
        results = []
        page_token = None
        query = f"'{folder_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        while True:
            resp = _with_retry(
                svc.files().list(
                    q=query,
                    fields="nextPageToken, files(id, name)",
                    pageToken=page_token,
                ).execute
            )
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    def list_projects(self, root_folder_id: str) -> list[dict]:
        """Recursively walk root_folder_id; one entry per video folder found.

        Each result: {video_folder_id, path, raw_folder_id, cut_folder_id}.
        `path` is the full "CLIENT/BATCH/VIDEO"-style path from the root.
        """
        svc = self._service()
        results: list[dict] = []
        for child in self._list_subfolders(svc, root_folder_id):
            self._walk(svc, child, [child["name"]], results, depth=1)
        return results

    def _walk(self, svc: Resource, folder: dict, path_parts: list[str], results: list[dict], depth: int) -> None:
        subfolders = self._list_subfolders(svc, folder["id"])
        by_lower_name = {f["name"].strip().lower(): f for f in subfolders}

        if "raw" in by_lower_name and "cut" in by_lower_name:
            results.append({
                "video_folder_id": folder["id"],
                "path": "/".join(path_parts),
                "raw_folder_id": by_lower_name["raw"]["id"],
                "cut_folder_id": by_lower_name["cut"]["id"],
            })
            return

        if depth >= MAX_WALK_DEPTH:
            return
        for sub in subfolders:
            self._walk(svc, sub, [*path_parts, sub["name"]], results, depth + 1)

    def download_project(self, raw_folder_id: str, dest_dir: Path) -> None:
        """Download every file directly inside a project's raw/ folder into dest_dir.

        Google-native files (e.g. script.md pasted as a Google Doc instead of
        an uploaded .md) are exported as plain text so cut_engine.py sees the
        same script.md format either way.
        """
        svc = self._service()
        dest_dir.mkdir(parents=True, exist_ok=True)
        query = f"'{raw_folder_id}' in parents and trashed = false"
        page_token = None
        while True:
            resp = _with_retry(
                svc.files().list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token,
                ).execute
            )
            for f in resp.get("files", []):
                self._download_one(svc, f, dest_dir)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _download_one(self, svc: Resource, file_meta: dict, dest_dir: Path) -> None:
        file_id = file_meta["id"]
        name = file_meta["name"]
        mime = file_meta["mimeType"]

        if mime.startswith(GOOGLE_NATIVE_PREFIX):
            if not name.lower().endswith((".md", ".txt")):
                name = f"{Path(name).stem}.md"
            request = svc.files().export_media(fileId=file_id, mimeType="text/plain")
        else:
            request = svc.files().get_media(fileId=file_id)

        out_path = dest_dir / name
        buf = io.FileIO(out_path, "wb")
        try:
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = _with_retry(downloader.next_chunk)
        finally:
            buf.close()

    def upload_file(self, local_path: Path, dest_folder_id: str, name: str | None = None) -> str:
        """Upload local_path into dest_folder_id, returns the new file's id."""
        svc = self._service()
        media = MediaFileUpload(str(local_path), resumable=True)
        metadata = {"name": name or local_path.name, "parents": [dest_folder_id]}
        request = svc.files().create(body=metadata, media_body=media, fields="id")

        response = None
        while response is None:
            _, response = _with_retry(request.next_chunk)
        return response["id"]
