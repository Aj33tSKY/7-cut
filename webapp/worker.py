"""Concurrency-bounded job runner.

The concurrency limit that actually matters is CPU (ffmpeg/transcription
are real compute), not the database — see webapp/README.md. One
asyncio.Semaphore, sized by MAX_CONCURRENT_JOBS, gates how many jobs are
actively doing work at once. It does not matter whether those jobs came
from one user queuing five projects or five users queuing one each — the
semaphore doesn't know or care who queued what.

A job holds a semaphore slot only while it's actually running a pipeline
stage. It releases the slot the moment it reaches "awaiting_review" (a human
is now the bottleneck, not CPU) and re-acquires one when the review step
finishes rendering — so a project sitting in review for an hour doesn't
starve two other jobs that are ready to run.

Each blocking helper-script subprocess runs via asyncio.to_thread so N jobs'
subprocess calls genuinely overlap instead of serializing behind one event
loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import db
from models import JobStatus

HELPERS_DIR = Path(__file__).resolve().parent.parent / "helpers"
JOBS_DIR = Path(__file__).resolve().parent / "data" / "jobs"

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
POLL_INTERVAL_S = float(os.environ.get("WORKER_POLL_INTERVAL_S", "2"))

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_in_flight: set[str] = set()
_drive_client = None  # set by app.py at startup


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def edit_dir(job_id: str) -> Path:
    return job_dir(job_id) / "edit"


def _run_helper(script: str, args: list[str]) -> None:
    cmd = [sys.executable, str(HELPERS_DIR / script), *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed:\n{result.stderr[-4000:]}")


async def _run_helper_async(script: str, args: list[str]) -> None:
    await asyncio.to_thread(_run_helper, script, args)


async def run_intake_pipeline(job_id: str) -> None:
    """queued -> downloading -> transcribing -> cutting -> awaiting_review."""
    async with _semaphore:
        try:
            job = db.get_job(job_id)
            jdir = job_dir(job_id)
            edir = edit_dir(job_id)

            db.update_status(job_id, JobStatus.DOWNLOADING)
            await asyncio.to_thread(_drive_client.download_project, job.drive_raw_folder_id, jdir)

            db.update_status(job_id, JobStatus.TRANSCRIBING)
            await _run_helper_async(
                "transcribe_batch.py", [str(jdir), "--edit-dir", str(edir)]
            )
            await _run_helper_async("pack_transcripts.py", ["--edit-dir", str(edir)])

            db.update_status(job_id, JobStatus.CUTTING)
            await _run_helper_async(
                "cut_engine.py", [str(jdir), "--edit-dir", str(edir)]
            )

            edl_path = edir / "edl.json"
            stats = {}
            if edl_path.exists():
                stats = json.loads(edl_path.read_text()).get("stats", {})
            db.update_stats(job_id, stats)
            db.update_status(job_id, JobStatus.AWAITING_REVIEW)
        except Exception as e:
            db.update_status(job_id, JobStatus.FAILED, error=str(e))
        finally:
            _in_flight.discard(job_id)


async def run_finish_pipeline(job_id: str) -> None:
    """awaiting_review -> rendering -> uploading -> done, triggered by /render."""
    async with _semaphore:
        try:
            job = db.get_job(job_id)
            edir = edit_dir(job_id)
            out_path = edir / "final.mp4"

            db.update_status(job_id, JobStatus.RENDERING)
            await _run_helper_async(
                "render.py", [str(edir / "edl.json"), "-o", str(out_path)]
            )

            db.update_status(job_id, JobStatus.UPLOADING)
            leaf_name = job.project_name.rsplit("/", 1)[-1]
            await asyncio.to_thread(
                _drive_client.upload_file, out_path, job.drive_cut_folder_id, f"{leaf_name}.mp4"
            )

            db.update_status(job_id, JobStatus.DONE)
        except Exception as e:
            db.update_status(job_id, JobStatus.FAILED, error=str(e))
        finally:
            _in_flight.discard(job_id)


def dispatch(job_id: str, finish: bool = False) -> None:
    """Fire off a job's next phase as a background task, once, idempotently."""
    if job_id in _in_flight:
        return
    _in_flight.add(job_id)
    coro = run_finish_pipeline(job_id) if finish else run_intake_pipeline(job_id)
    asyncio.create_task(coro)


async def dispatcher_loop() -> None:
    """Picks up jobs left in `queued` (e.g. after a restart) and starts them."""
    while True:
        for job in db.list_jobs_by_status(JobStatus.QUEUED):
            dispatch(job.id)
        await asyncio.sleep(POLL_INTERVAL_S)


def recover_interrupted_jobs() -> None:
    """On startup, anything stuck mid-stage from a crashed process is dead —
    there's no in-memory work to resume. Fail them visibly rather than let
    them sit in a status the dispatcher will never revisit.
    """
    stuck = db.list_jobs_by_status(
        JobStatus.DOWNLOADING, JobStatus.TRANSCRIBING, JobStatus.CUTTING,
        JobStatus.RENDERING, JobStatus.UPLOADING,
    )
    for job in stuck:
        db.update_status(job.id, JobStatus.FAILED, error="interrupted by service restart — re-queue")


def set_drive_client(client) -> None:
    global _drive_client
    _drive_client = client
