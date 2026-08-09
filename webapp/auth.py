"""Google OAuth login, restricted to the team's Workspace domain.

Deliberately separate from drive.py's service account (see its docstring).
Login only decides who's allowed to use the dashboard; it never touches
Drive itself.
"""

from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

ALLOWED_DOMAIN = os.environ.get("ALLOWED_GOOGLE_DOMAIN", "")

oauth = OAuth()
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    client_kwargs={"scope": "openid email profile"},
)

router = APIRouter()


@router.get("/login")
async def login(request: Request):
    # Railway (like most PaaS) terminates HTTPS at its edge and forwards plain
    # HTTP internally; without trusting X-Forwarded-Proto (see Dockerfile's
    # --proxy-headers) this would build an http:// callback URL here, which
    # Google rejects as a mismatch against the https:// one actually
    # registered. request.url_for reflects whatever scheme uvicorn believes
    # the request arrived on.
    redirect_uri = request.url_for("auth_callback")

    # prompt=select_account always shows Google's account chooser, instead of
    # silently authenticating with whatever Google session happens to already
    # be active in the browser.
    extra = {"prompt": "select_account"}
    if ALLOWED_DOMAIN:
        extra["hd"] = ALLOWED_DOMAIN
    return await oauth.google.authorize_redirect(request, redirect_uri, **extra)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")
    if not email:
        raise HTTPException(400, "Google did not return an email address")

    if ALLOWED_DOMAIN and not email.lower().endswith(f"@{ALLOWED_DOMAIN.lower()}"):
        raise HTTPException(403, f"Only @{ALLOWED_DOMAIN} accounts can use this tool")

    request.session["user"] = {"email": email, "name": userinfo.get("name", email)}
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


def current_user(request: Request) -> dict:
    """FastAPI dependency — raises 401 if nobody's logged in."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "not logged in")
    return user
