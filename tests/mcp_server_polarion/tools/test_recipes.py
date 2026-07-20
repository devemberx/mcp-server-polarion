"""Static SQL and HTML recipe gallery tool tests."""

from __future__ import annotations

from mcp_server_polarion.models import HtmlRecipeGallery, SqlRecipeGallery
from mcp_server_polarion.tools.recipes import get_html_recipes, get_sql_query_recipes
from mcp_server_polarion.utils.html import (
    _POLARION_TABLE_STYLE,
    _POLARION_TD_STYLE,
    _POLARION_TH_STYLE,
)


class TestGetSqlQueryRecipes:
    """``get_sql_query_recipes`` serve the SQL:(...) recipe gallery."""

    async def test_returns_gallery_with_schema_and_core_recipes(self) -> None:
        result = await get_sql_query_recipes()
        assert isinstance(result, SqlRecipeGallery)
        for marker in (
            "## Schema",
            "work items belonging to a document",
            "custom-field value search",
            "back-traceability",
            "forward-traceability",
        ):
            assert marker in result.recipes


class TestGetHtmlRecipes:
    """``get_html_recipes`` serve raw-HTML templates for update tools."""

    async def test_returns_gallery_with_core_templates(self) -> None:
        result = await get_html_recipes()
        assert isinstance(result, HtmlRecipeGallery)
        assert "polarion-Document-table" in result.recipes
        assert "polarion-rte-caption-paragraph" in result.recipes
        assert 'data-sequence="Table"' in result.recipes
        assert 'data-sequence="Figure"' in result.recipes

    async def test_returns_link_and_widget_templates(self) -> None:
        result = await get_html_recipes()
        for marker in (
            'data-type="workItem"',
            'data-type="crossReference"',
            'data-type="richPage"',
            "polarion_wiki macro name=toc",
            "polarion_wiki macro name=tof",
            "polarion_wiki macro name=page_break",
        ):
            assert marker in result.recipes

    async def test_recipes_warn_about_scope_and_macro_ids(self) -> None:
        result = await get_html_recipes()
        assert "custom field" in result.recipes.lower()
        assert "polarion_wiki" in result.recipes

    async def test_recipes_cover_work_item_attachment_images(self) -> None:
        result = await get_html_recipes()
        for marker in (
            "Image from work item attachments",
            "workitemimg:",
            "list_work_item_attachments",
        ):
            assert marker in result.recipes

    async def test_recipes_stay_in_sync_with_pipeline_constants(self) -> None:
        # Guide must quote the exact styles polarionify_html injects.
        result = await get_html_recipes()
        for constant in (
            _POLARION_TABLE_STYLE,
            _POLARION_TH_STYLE,
            _POLARION_TD_STYLE,
        ):
            assert constant in result.recipes
