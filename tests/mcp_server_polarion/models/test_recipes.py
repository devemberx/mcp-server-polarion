"""Tests for the recipe gallery models."""

from __future__ import annotations

from mcp_server_polarion.models import HtmlRecipeGallery, SqlRecipeGallery


class TestRecipeGalleries:
    """Galleries are plain string carriers that round-trip through JSON."""

    def test_sql_gallery_round_trip(self) -> None:
        gallery = SqlRecipeGallery(recipes="SELECT wi.c_uri FROM workitem wi")
        assert (
            SqlRecipeGallery.model_validate_json(gallery.model_dump_json()) == gallery
        )

    def test_html_gallery_round_trip(self) -> None:
        gallery = HtmlRecipeGallery(recipes="<table></table>")
        assert (
            HtmlRecipeGallery.model_validate_json(gallery.model_dump_json()) == gallery
        )
