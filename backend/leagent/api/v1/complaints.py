"""Customer complaint ticket API endpoints."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from leagent.db import DatabaseService, get_database_service
from leagent.db.models.base import utc_now
from leagent.db.models.complaint import (
    COMPLAINT_STATUSES,
    Complaint,
    ComplaintRead,
)
from leagent.db.sqlite_compat import same_user_id
from leagent.services.auth import CurrentUserId

router = APIRouter()

DbDep = Annotated[DatabaseService, Depends(get_database_service)]

ComplaintStatus = Literal["open", "in_progress", "resolved", "closed"]
ComplaintCategory = Literal["product", "service", "billing", "logistics", "other"]
ComplaintSeverity = Literal["low", "medium", "high", "urgent"]


class ComplaintCreateRequest(BaseModel):
    """Request schema for creating a complaint."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    customer_name: str | None = Field(default=None, max_length=200)
    customer_contact: str | None = Field(default=None, max_length=200)
    category: ComplaintCategory = "other"
    severity: ComplaintSeverity = "medium"
    source: Literal["manual", "chat"] = "manual"
    session_id: str | None = Field(default=None, max_length=100)


class ComplaintUpdateRequest(BaseModel):
    """Request schema for updating a complaint."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    customer_name: str | None = Field(default=None, max_length=200)
    customer_contact: str | None = Field(default=None, max_length=200)
    category: ComplaintCategory | None = None
    severity: ComplaintSeverity | None = None
    status: ComplaintStatus | None = None
    resolution: str | None = Field(default=None, max_length=10000)


class ComplaintStats(BaseModel):
    """Per-status complaint counts."""

    total: int
    by_status: dict[str, int]


async def _get_owned_complaint(
    db: DatabaseService, complaint_id: UUID, user_id: UUID
) -> Complaint:
    complaint = await db.repositories.complaints.get(complaint_id)
    if complaint is None or complaint.is_deleted or not same_user_id(complaint.user_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Complaint not found"
        )
    return complaint


@router.post("", response_model=ComplaintRead, status_code=status.HTTP_201_CREATED)
async def create_complaint(
    data: ComplaintCreateRequest,
    user_id: CurrentUserId,
    db: DbDep,
) -> Complaint:
    """Create a new complaint ticket."""
    complaint = Complaint(
        title=data.title,
        description=data.description,
        customer_name=data.customer_name,
        customer_contact=data.customer_contact,
        category=data.category,
        severity=data.severity,
        source=data.source,
        session_id=data.session_id,
        user_id=user_id,
    )
    return await db.repositories.complaints.create(complaint)


@router.get("", response_model=list[ComplaintRead])
async def list_complaints(
    user_id: CurrentUserId,
    db: DbDep,
    status_filter: Annotated[ComplaintStatus | None, Query(alias="status")] = None,
    category: ComplaintCategory | None = None,
    severity: ComplaintSeverity | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Complaint]:
    """List the current user's complaints, newest first."""
    return await db.repositories.complaints.list_for_user(
        user_id,
        status=status_filter,
        category=category,
        severity=severity,
        limit=limit,
    )


@router.get("/stats", response_model=ComplaintStats)
async def complaint_stats(
    user_id: CurrentUserId,
    db: DbDep,
) -> ComplaintStats:
    """Return per-status complaint counts for the current user."""
    counts = await db.repositories.complaints.count_by_status(user_id)
    by_status = {s: counts.get(s, 0) for s in COMPLAINT_STATUSES}
    return ComplaintStats(total=sum(by_status.values()), by_status=by_status)


@router.get("/{complaint_id}", response_model=ComplaintRead)
async def get_complaint(
    complaint_id: UUID,
    user_id: CurrentUserId,
    db: DbDep,
) -> Complaint:
    """Return one complaint owned by the current user."""
    return await _get_owned_complaint(db, complaint_id, user_id)


@router.patch("/{complaint_id}", response_model=ComplaintRead)
async def update_complaint(
    complaint_id: UUID,
    data: ComplaintUpdateRequest,
    user_id: CurrentUserId,
    db: DbDep,
) -> Complaint:
    """Update fields / transition the status of a complaint."""
    complaint = await _get_owned_complaint(db, complaint_id, user_id)

    updates = data.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(complaint, field_name, value)

    if updates.get("status") == "resolved" and complaint.resolved_at is None:
        complaint.resolved_at = utc_now()

    complaint.updated_at = utc_now()
    return await db.repositories.complaints.update(complaint)


@router.delete("/{complaint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_complaint(
    complaint_id: UUID,
    user_id: CurrentUserId,
    db: DbDep,
) -> None:
    """Soft-delete a complaint owned by the current user."""
    await _get_owned_complaint(db, complaint_id, user_id)
    await db.repositories.complaints.soft_delete(complaint_id, user_id)
