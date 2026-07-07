"""Project models."""

from __future__ import annotations

from pydantic import BaseModel


class ProjectSummary(BaseModel):
    """Polarion project summary from ``list_projects``."""

    id: str
    name: str
    active: bool = True
