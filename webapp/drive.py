"""Google Drive client — service account, walks a nested client/batch/video
tree, reads each video's raw/ subfolder, writes to its cut/ subfolder.

Deliberately decoupled from whoever's logged in (see auth.py): a session
expiring mid-render can't orphan a job or block an upload. The service
account only needs to be a member of (or shared on) the single root folder
— Drive permissions inherit down the whole tree, current and future
subfolders alike.

Project convention: an arbitrary-depth tree (e.g. CLIENT/BATCH/VIDEO) where
a "project" is any folder that directly contains both a raw/ and a cut/
subfolder (case-insensitive). Recursion stops at the first such folder found
on a branch — nothing nests a project inside another project.
"""

from __future__ import annotations

import io
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
MAX_WALK_DEPTH = 6


class DriveClient:
    def __init__(self, service_account_json_path: str):
        creds = service_account.Credentials.from_service_account_file(
            service_account_json_path, scopes=SCOPES
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _list_subfolders(self, folder_id: str) -> list[dict]:
        results = []
        page_token = None
        query = f"'{folder_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        while True:
            resp = self._svc.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
            ).execute()
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
        results: list[dict] = []
        for child in self._list_subfolders(root_folder_id):
            self._walk(child, [child["name"]], results, depth=1)
        return results

    def _walk(self, folder: dict, path_parts: list[str], results: list[dict], depth: int) -> None:
        subfolders = self._list_subfolders(folder["id"])
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
            self._walk(sub, [*path_parts, sub["name"]], results, depth + 1)

    def download_project(self, raw_folder_id: str, dest_dir: Path) -> None:
        """Download every file directly inside a project's raw/ folder into dest_dir.

        Google-native files (e.g. script.md pasted as a Google Doc instead of
        an uploaded .md) are exported as plain text so cut_engine.py sees the
        same script.md format either way.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        query = f"'{raw_folder_id}' in parents and trashed = false"
        page_token = None
        while True:
            resp = self._svc.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                self._download_one(f, dest_dir)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _download_one(self, file_meta: dict, dest_dir: Path) -> None:
        file_id = file_meta["id"]
        name = file_meta["name"]
        mime = file_meta["mimeType"]

        if mime.startswith(GOOGLE_NATIVE_PREFIX):
            if not name.lower().endswith((".md", ".txt")):
                name = f"{Path(name).stem}.md"
            request = self._svc.files().export_media(fileId=file_id, mimeType="text/plain")
        else:
            request = self._svc.files().get_media(fileId=file_id)

        out_path = dest_dir / name
        buf = io.FileIO(out_path, "wb")
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.close()

    def upload_file(self, local_path: Path, dest_folder_id: str, name: str | None = None) -> str:
        """Upload local_path into dest_folder_id, returns the new file's id."""
        media = MediaFileUpload(str(local_path), resumable=True)
        metadata = {"name": name or local_path.name, "parents": [dest_folder_id]}
        created = self._svc.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        return created["id"]
