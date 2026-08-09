"""Job status enum and the Job record shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    CUTTING = "cutting"
    AWAITING_REVIEW = "awaiting_review"
    RENDERING = "rendering"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


# Statuses the worker still needs to move forward on its own.
ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.TRANSCRIBING,
    JobStatus.CUTTING,
    JobStatus.RENDERING,
    JobStatus.UPLOADING,
}

# Terminal / waiting-on-a-human statuses — the worker leaves these alone.
PAUSED_STATUSES = {JobStatus.AWAITING_REVIEW, JobStatus.DONE, JobStatus.FAILED}


@dataclass
class Job:
    id: str
    drive_folder_id: str
    project_name: str
    status: JobStatus
    created_by: str
    created_at: str
    updated_at: str
    error: str | None = None
    stats: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Job":
        import json
        return cls(
            id=row["id"],
            drive_folder_id=row["drive_folder_id"],
            project_name=row["project_name"],
            status=JobStatus(row["status"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            stats=json.loads(row["stats"]) if row["stats"] else {},
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "drive_folder_id": self.drive_folder_id,
            "project_name": self.project_name,
            "status": self.status.value,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "stats": self.stats,
        }
