"""Google Drive client — service account, read raw/, write edited/.

Deliberately decoupled from whoever's logged in (see auth.py): a session
expiring mid-render can't orphan a job or block an upload. The service
account just needs to be added as a member of both shared folders.

Project convention matches the CLI tool exactly: one subfolder of the raw
folder = one project, containing raw clips + script.md.
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


class DriveClient:
    def __init__(self, service_account_json_path: str):
        creds = service_account.Credentials.from_service_account_file(
            service_account_json_path, scopes=SCOPES
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_projects(self, raw_folder_id: str) -> list[dict]:
        """Each subfolder of raw_folder_id is one project."""
        results = []
        page_token = None
        query = f"'{raw_folder_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        while True:
            resp = self._svc.files().list(
                q=query,
                fields="nextPageToken, files(id, name, modifiedTime)",
                pageToken=page_token,
            ).execute()
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    def download_project(self, project_folder_id: str, dest_dir: Path) -> None:
        """Download every file directly inside project_folder_id into dest_dir.

        Google-native files (e.g. script.md pasted as a Google Doc instead of
        an uploaded .md) are exported as plain text so cut_engine.py sees the
        same script.md format either way.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        query = f"'{project_folder_id}' in parents and trashed = false"
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
