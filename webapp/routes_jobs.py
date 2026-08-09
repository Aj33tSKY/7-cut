"""Job status, and the review endpoints (job-scoped port of review_server.py)."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import db
import worker
from auth import current_user
from models import JobStatus

HELPERS_DIR = Path(__file__).resolve().parent.parent / "helpers"
sys.path.insert(0, str(HELPERS_DIR))
from render import resolve_path  # noqa: E402

router = APIRouter(prefix="/api/jobs")

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _get_job_or_404(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


@router.get("")
async def list_jobs(user: dict = Depends(current_user)):
    return [j.to_dict() for j in db.list_jobs()]


@router.get("/{job_id}")
async def get_job(job_id: str, user: dict = Depends(current_user)):
    return _get_job_or_404(job_id).to_dict()


@router.delete("/{job_id}")
async def delete_job(job_id: str, user: dict = Depends(current_user)):
    _get_job_or_404(job_id)
    db.delete_job(job_id)
    shutil.rmtree(worker.job_dir(job_id), ignore_errors=True)
    return {"ok": True}


@router.get("/{job_id}/edl")
async def get_edl(job_id: str, user: dict = Depends(current_user)):
    job = _get_job_or_404(job_id)
    edl_path = worker.edit_dir(job_id) / "edl.json"
    if not edl_path.exists():
        raise HTTPException(409, f"no edl yet — job status is {job.status.value}")

    edl = json.loads(edl_path.read_text())
    sources = edl.get("sources", {})
    durations = {
        name: _probe_duration(resolve_path(rel, worker.edit_dir(job_id)))
        for name, rel in sources.items()
    }
    return {
        "ranges": edl.get("ranges", []),
        "source_durations": durations,
        "total_duration_s": edl.get("total_duration_s", 0),
    }


@router.post("/{job_id}/save")
async def save_edl(job_id: str, request: Request, user: dict = Depends(current_user)):
    job = _get_job_or_404(job_id)
    body = await request.json()
    ranges = body.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise HTTPException(400, "ranges required")

    edl_path = worker.edit_dir(job_id) / "edl.json"
    edl = json.loads(edl_path.read_text())
    backup = edl_path.with_name(f"edl.backup.{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
    shutil.copy2(edl_path, backup)

    edl["ranges"] = ranges
    edl["total_duration_s"] = round(sum(r["end"] - r["start"] for r in ranges), 3)
    edl_path.write_text(json.dumps(edl, indent=2))

    return {"ok": True, "total_duration_s": edl["total_duration_s"]}


@router.get("/{job_id}/media/{source_name}")
async def get_media(job_id: str, source_name: str, request: Request, user: dict = Depends(current_user)):
    edl_path = worker.edit_dir(job_id) / "edl.json"
    if not edl_path.exists():
        raise HTTPException(404, "no edl yet")
    edl = json.loads(edl_path.read_text())
    sources = edl.get("sources", {})
    if source_name not in sources:
        raise HTTPException(404, "unknown source")
    path = resolve_path(sources[source_name], worker.edit_dir(job_id))
    if not path.exists():
        raise HTTPException(404, "file missing")

    file_size = path.stat().st_size
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    range_header = request.headers.get("range")

    start, end = 0, file_size - 1
    status_code = 200
    if range_header:
        m = RANGE_RE.match(range_header)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            status_code = 206
    length = end - start + 1

    def iterfile():
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            chunk = 1024 * 1024
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                yield data
                remaining -= len(data)

    headers = {"Content-Length": str(length), "Accept-Ranges": "bytes"}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iterfile(), status_code=status_code, media_type=content_type, headers=headers)


@router.post("/{job_id}/render")
async def start_render(job_id: str, user: dict = Depends(current_user)):
    job = _get_job_or_404(job_id)
    if job.status != JobStatus.AWAITING_REVIEW:
        raise HTTPException(409, f"job is {job.status.value}, not awaiting_review")
    worker.dispatch(job_id, finish=True)
    return {"ok": True}
