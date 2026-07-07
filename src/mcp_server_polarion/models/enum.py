"""Enum option model — valid option set from list_*_enum_options."""

from __future__ import annotations

from pydantic import BaseModel


class EnumOption(BaseModel):
    """Single enum option from ``list_*_enum_options``."""

    id: str
    name: str
    description: str = ""
    default: bool = False
    hidden: bool = False
    terminal: bool = False
