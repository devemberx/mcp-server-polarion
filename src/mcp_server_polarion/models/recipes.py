"""Recipe gallery models served by the static guide tools."""

from __future__ import annotations

from pydantic import BaseModel


class SqlRecipeGallery(BaseModel):
    """Copy-paste SQL recipe gallery returned by ``get_sql_query_recipes``."""

    recipes: str


class HtmlRecipeGallery(BaseModel):
    """Copy-paste Polarion HTML templates returned by ``get_html_recipes``."""

    recipes: str
