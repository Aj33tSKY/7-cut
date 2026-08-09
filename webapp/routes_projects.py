"""GET /api/projects, POST /api/projects/{video_folder_id}/queue."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request

import db
import worker
from auth import current_user

router = APIRouter(prefix="/api/projects")

DRIVE_ROOT_FOLDER_ID = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")


@router.get("")
async def list_projects(user: dict = Depends(current_user)):
    """Every video folder under the root that has a raw/ + cut/ pair,
    cross-referenced with whether a job already exists for it."""
    # to_thread: this is a blocking Drive walk (see drive.py) — running it
    # directly in this async handler would stall the whole event loop,
    # including every in-flight job, for however long the walk takes.
    drive_projects = await asyncio.to_thread(worker._drive_client.list_projects, DRIVE_ROOT_FOLDER_ID)
    all_jobs = await asyncio.to_thread(db.list_jobs)
    jobs_by_folder = {}
    for job in all_jobs:
        jobs_by_folder.setdefault(job.drive_folder_id, job)  # most recent (list is DESC)

    out = []
    for p in drive_projects:
        job = jobs_by_folder.get(p["video_folder_id"])
        out.append({
            "video_folder_id": p["video_folder_id"],
            "path": p["path"],
            "raw_folder_id": p["raw_folder_id"],
            "cut_folder_id": p["cut_folder_id"],
            "job": job.to_dict() if job else None,
        })
    return out


@router.post("/{video_folder_id}/queue")
async def queue_project(video_folder_id: str, request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    path = body.get("path")
    raw_folder_id = body.get("raw_folder_id")
    cut_folder_id = body.get("cut_folder_id")
    if not (path and raw_folder_id and cut_folder_id):
        raise HTTPException(400, "path, raw_folder_id, and cut_folder_id are required")

    job = await asyncio.to_thread(
        db.create_job,
        drive_folder_id=video_folder_id,
        drive_raw_folder_id=raw_folder_id,
        drive_cut_folder_id=cut_folder_id,
        project_name=path,
        created_by=user["email"],
    )
    worker.dispatch(job.id)
    return job.to_dict()
