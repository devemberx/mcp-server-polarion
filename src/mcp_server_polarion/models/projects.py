"""Project models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectSummary(BaseModel):
    """Summary of a Polarion project returned by ``list_projects``."""

    id: str
    name: str
    active: bool = Field(default=True, description="False means archived.")
