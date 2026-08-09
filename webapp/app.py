"""FastAPI entrypoint.

Run locally:
    cd webapp && uv run --extra webapp uvicorn app:app --reload

One process, one event loop: the API, the OAuth login, and the background
worker dispatcher all run here. No separate worker dyno — see worker.py's
docstring for why that's fine at this scale.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import db
import worker
from auth import current_user, router as auth_router
from drive import DriveClient
from routes_jobs import router as jobs_router
from routes_projects import router as projects_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_path:
        worker.set_drive_client(DriveClient(creds_path))

    worker.recover_interrupted_jobs()
    dispatcher_task = asyncio.create_task(worker.dispatcher_loop())
    yield
    dispatcher_task.cancel()


app = FastAPI(title="video-use", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "dev-secret-change-me"))

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(jobs_router)


@app.get("/health")
async def health():
    """Unauthenticated — Railway (or any platform healthcheck) needs a path
    that doesn't require login. Pointing a healthcheck at `/` would get a
    redirect/401 on every anonymous probe, which can read as "unhealthy" and
    trigger a restart — killing whatever job happens to be running at the
    time. Set this as the service's Healthcheck Path in Railway settings.
    """
    return {"ok": True}


@app.get("/")
async def dashboard(user: dict = Depends(current_user)):
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return user


@app.get("/review/{job_id}")
async def review_page(job_id: str, user: dict = Depends(current_user)):
    return FileResponse(STATIC_DIR / "review.html")


@app.exception_handler(401)
async def unauthorized(request, exc):
    return RedirectResponse("/login")
