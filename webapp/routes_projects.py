"""GET /api/projects, POST /api/projects/{drive_folder_id}/queue."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request

import db
import worker
from auth import current_user

router = APIRouter(prefix="/api/projects")

DRIVE_RAW_FOLDER_ID = os.environ.get("DRIVE_RAW_FOLDER_ID", "")


@router.get("")
async def list_projects(user: dict = Depends(current_user)):
    """Drive raw/ folders, cross-referenced with whether a job already exists."""
    drive_projects = worker._drive_client.list_projects(DRIVE_RAW_FOLDER_ID)
    jobs_by_folder = {}
    for job in db.list_jobs():
        jobs_by_folder.setdefault(job.drive_folder_id, job)  # most recent (list is DESC)

    out = []
    for p in drive_projects:
        job = jobs_by_folder.get(p["id"])
        out.append({
            "drive_folder_id": p["id"],
            "name": p["name"],
            "modified_time": p.get("modifiedTime"),
            "job": job.to_dict() if job else None,
        })
    return out


@router.post("/{drive_folder_id}/queue")
async def queue_project(drive_folder_id: str, request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    project_name = body.get("name") or drive_folder_id
    job = db.create_job(drive_folder_id, project_name, created_by=user["email"])
    worker.dispatch(job.id)
    return job.to_dict()
