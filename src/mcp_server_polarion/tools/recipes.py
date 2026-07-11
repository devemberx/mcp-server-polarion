"""Static recipe tools — copy-paste SQL queries and Polarion HTML templates
served from ``tools/guides/``.
"""

from __future__ import annotations

from importlib import resources
from typing import Final

from mcp_server_polarion.models import HtmlRecipeGallery, SqlRecipeGallery
from mcp_server_polarion.server import mcp

_SQL_QUERY_RECIPES: Final[str] = (
    resources.files("mcp_server_polarion.tools")
    .joinpath("guides", "sql_query_recipes.md")
    .read_text(encoding="utf-8")
)

_HTML_RECIPES: Final[str] = (
    resources.files("mcp_server_polarion.tools")
    .joinpath("guides", "html_recipes.md")
    .read_text(encoding="utf-8")
)


@mcp.tool(
    tags={"read"},
    annotations={"readOnlyHint": True},
)
async def get_sql_query_recipes() -> SqlRecipeGallery:
    """Fetch copy-paste SQL recipes for the list_work_items SQL:(...) prefix.

    Call before writing any SQL query (document scope, custom-field,
    traceability); adapt a recipe instead of hand-writing joins. Includes the
    table schema.
    """
    return SqlRecipeGallery(recipes=_SQL_QUERY_RECIPES)


@mcp.tool(
    tags={"read"},
    annotations={"readOnlyHint": True},
)
async def get_html_recipes() -> HtmlRecipeGallery:
    """Fetch the required HTML templates for tables, captions, links, and
    widgets written via update_work_items / update_document.

    Any new <table>, numbered caption, link, or TOC / Table-of-Figures
    widget must be adapted from these templates — hand-written markup
    renders unstyled and breaks numbering. Also covers macro-id and
    metadata-scope caveats.
    """
    return HtmlRecipeGallery(recipes=_HTML_RECIPES)
