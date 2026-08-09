"""One-time LOCAL script: get an offline refresh token for a real Google
account, so Drive uploads count against that account's real storage quota
instead of a service account's — which is always zero, see drive.py's
module docstring for why that fails with storageQuotaExceeded.

Run this on your own machine, logged in (in the browser window it opens)
as whichever Google account should own uploaded files — typically whoever
owns/manages the root Drive folder. The printed refresh token then goes
into Railway as GOOGLE_DRIVE_REFRESH_TOKEN. This is a one-time setup step,
not something that runs as part of the deployed app.

Requires the SAME OAuth client already used for login (GOOGLE_OAUTH_
CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET) to also have
http://localhost:8765/ added as an Authorized redirect URI in Google Cloud
Console (Credentials -> that OAuth client -> Authorized redirect URIs) —
add it ALONGSIDE the existing production callback URL, don't replace it.

Usage:
    GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
        uv run --extra webapp python webapp/get_drive_refresh_token.py
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
PORT = 8765


def main() -> None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first "
            "(same values already in Railway)"
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{PORT}/"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # access_type=offline + prompt=consent: without both, Google often
    # omits the refresh_token (e.g. if this account has authorized this
    # OAuth client before) — prompt=consent forces the consent screen
    # again so a refresh_token is issued every time this script runs.
    creds = flow.run_local_server(port=PORT, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        sys.exit(
            "Google didn't return a refresh_token. Revoke this app's access at "
            "https://myaccount.google.com/permissions and run this script again."
        )

    print("\nLogged in successfully. Add this to Railway as GOOGLE_DRIVE_REFRESH_TOKEN:\n")
    print(creds.refresh_token)
    print()


if __name__ == "__main__":
    main()
