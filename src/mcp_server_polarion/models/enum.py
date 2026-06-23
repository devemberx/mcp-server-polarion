"""Enum option model — the valid option set returned by list_*_enum_options."""

from __future__ import annotations

from pydantic import BaseModel


class EnumOption(BaseModel):
    """Single enum option returned by ``list_*_enum_options``."""

    id: str
    name: str
    description: str = ""
    default: bool = False
    hidden: bool = False
    terminal: bool = False
