"""Tests for the document query/read/create/update tools."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Annotated, cast, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    DocumentCreateResult,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentUpdateResult,
    PaginatedResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools import documents as _mod
from mcp_server_polarion.tools._shared import cache as _cache_mod
from mcp_server_polarion.tools.documents import (
    _build_create_document_payload,
    _build_update_document_payload,
    create_document,
    get_document,
    list_documents,
    read_document,
    read_document_parts,
    update_document,
)


def _make_part(
    *,
    type_: str,
    part_id: str = "polarion_x",
    title: str = "",
    content: str = "",
    description: str = "",
    level: int = 0,
    work_item_id: str = "",
    outline_number: str = "",
) -> DocumentPart:
    """Build a ``DocumentPart`` with sensible defaults for render-rule tests."""
    return DocumentPart(
        id=part_id,
        title=title,
        content=content,
        type=type_,  # type: ignore[arg-type]
        level=level,
        description=description,
        work_item_id=work_item_id,
        work_item_type="",
        work_item_status="",
        external=False,
        outline_number=outline_number,
        next_part_id="",
    )


def _stub_parts(
    monkeypatch: pytest.MonkeyPatch, parts: list[DocumentPart]
) -> AsyncMock:
    """Replace ``read_document_parts`` with an AsyncMock returning *parts*; page
    metadata derived from the list. Returns the mock for call assertions.
    """
    stub = AsyncMock(
        return_value=PaginatedResult[DocumentPart](
            items=parts,
            total_count=len(parts),
            page=1,
            page_size=100,
            has_more=False,
        )
    )
    monkeypatch.setattr(_mod, "read_document_parts", stub)
    return stub


def _enum_get_response(ids: list[str]) -> dict[str, object]:
    """Shape a ``getAvailableOptions`` reply for the guard tests."""
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


async def _call_create_doc(mock_ctx: MagicMock, **overrides: object) -> object:
    """Invoke ``create_document`` with explicit defaults for every Field."""
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "space_id": "_default",
        "module_name": "Doc",
        "title": "x",
        "type": "systemRequirementSpecification",
        "status": None,
        "home_page_content": None,
        "custom_fields": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await create_document(mock_ctx, **defaults)  # type: ignore[arg-type]


async def _call_update_doc(mock_ctx: MagicMock, **overrides: object) -> object:
    """Invoke ``update_document`` with explicit defaults for every Field."""
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "space_id": "_default",
        "document_name": "Doc",
        "title": None,
        "status": None,
        "type": None,
        "home_page_content_html": None,
        "custom_fields": None,
        "workflow_action": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await update_document(mock_ctx, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def reset_enum_guard_caches() -> None:
    """Drop guard caches between integration tests so each scenario starts cold."""
    _cache_mod._enum_option_cache.clear()
    _cache_mod._project_enum_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_type_custom_key_cache.clear()


def _module_resource(
    full_id: str,
    doc_type: str = "generic",
    *,
    status: str = "",
    updated: str = "",
    author_id: str = "",
    updated_by_id: str = "",
) -> dict:
    """An ``included`` document resource as returned by ``include=module``."""
    attributes: dict = {"type": doc_type}
    if status:
        attributes["status"] = status
    if updated:
        attributes["updated"] = updated
    resource: dict = {"type": "documents", "id": full_id, "attributes": attributes}
    relationships: dict = {}
    if author_id:
        relationships["author"] = {"data": {"type": "users", "id": author_id}}
    if updated_by_id:
        relationships["updatedBy"] = {"data": {"type": "users", "id": updated_by_id}}
    if relationships:
        resource["relationships"] = relationships
    return resource


def _user_resource(user_id: str, name: str) -> dict:
    """An ``included`` user resource as returned by an ``include=`` user path."""
    return {"type": "users", "id": user_id, "attributes": {"name": name}}


def _discovery_response(
    included: list[dict],
    *,
    total: int | None = None,
    data_count: int | None = None,
) -> dict:
    """Build a discovery page: ``data`` drives pagination, ``included`` the docs.

    Discovery reads documents from ``included``; ``data`` (one heading per doc)
    only feeds the page-walk loop, so its rows are bare placeholders.
    """
    rows = data_count if data_count is not None else len(included)
    response: dict = {
        "data": [{"type": "workitems"} for _ in range(rows)],
        "included": included,
    }
    if total is not None:
        response["meta"] = {"totalCount": total}
    return response


class TestListDocuments:
    """Tests for the ``list_documents`` tool."""

    @pytest.fixture(autouse=True)
    def _clear_doc_cache(self) -> Iterator[None]:
        """Reset the module-level TTL cache around every test.

        Several tests reuse ``project_id='proj1'``, so without this the
        first test would poison subsequent tests via cache hits.
        """
        _cache_mod._document_list_cache.clear()
        yield
        _cache_mod._document_list_cache.clear()

    async def test_extracts_documents_from_modules(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        included = [
            _module_resource("proj1/_default/Doc1", "generic"),
            _module_resource("proj1/_default/Doc2", "generic"),
            _module_resource("proj1/Design/SRS", "req_specification"),
            _module_resource("proj1/Design/SDD", "generic"),
            _module_resource("proj1/Testing/TestPlan", "testspecification"),
        ]
        # Single page — totalCount ≤ page_size, so no binary search.
        mock_client.get.return_value = _discovery_response(included, total=5)

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 5
        triples = [(d.space_id, d.document_name, d.type) for d in result.items]
        assert ("_default", "Doc1", "generic") in triples
        assert ("_default", "Doc2", "generic") in triples
        assert ("Design", "SDD", "generic") in triples
        assert ("Design", "SRS", "req_specification") in triples
        assert ("Testing", "TestPlan", "testspecification") in triples

    async def test_metadata_fields_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """status/updated come from attributes, editor names from included users."""
        included = [
            _module_resource(
                "proj1/_default/Doc1",
                "generic",
                status="draft",
                updated="2026-02-22T14:53:03.244Z",
                author_id="admin",
                updated_by_id="72c2462f",
            ),
            _user_resource("admin", "System Administrator"),
            _user_resource("72c2462f", "Dev Member"),
        ]
        mock_client.get.return_value = _discovery_response(
            included, total=1, data_count=1
        )

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        doc = result.items[0]
        assert doc.status == "draft"
        assert doc.updated == "2026-02-22T14:53:03.244Z"
        assert doc.author == "System Administrator"
        assert doc.last_updated_by == "Dev Member"

    async def test_unresolved_user_yields_empty_name(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """A relationship pointing at a user missing from ``included`` → ``""``."""
        included = [
            _module_resource("proj1/_default/Doc1", author_id="ghost"),
        ]
        mock_client.get.return_value = _discovery_response(
            included, total=1, data_count=1
        )

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.items[0].author == ""

    async def test_id_less_included_user_never_matches(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """An id-less included user must not join with an absent relationship."""
        included = [
            _module_resource("proj1/_default/Doc1"),
            {"type": "users", "attributes": {"name": "Phantom"}},
        ]
        mock_client.get.return_value = _discovery_response(
            included, total=1, data_count=1
        )

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        doc = result.items[0]
        assert doc.author == ""
        assert doc.last_updated_by == ""

    async def test_missing_metadata_defaults_empty(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Bare module resources (no status/updated/relationships) → empty fields."""
        included = [_module_resource("proj1/_default/Doc1")]
        mock_client.get.return_value = _discovery_response(included, total=1)

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        doc = result.items[0]
        assert doc.status == ""
        assert doc.updated == ""
        assert doc.author == ""
        assert doc.last_updated_by == ""

    async def test_deduplicates_documents(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        included = [_module_resource("proj1/Space1/DocA", "generic")] * 3
        mock_client.get.return_value = _discovery_response(included, total=3)

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 1
        assert result.items[0].space_id == "Space1"
        assert result.items[0].document_name == "DocA"
        assert result.items[0].type == "generic"

    async def test_pagination_slicing(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        included = [_module_resource(f"proj1/Space{i}/Doc") for i in range(5)]
        mock_client.get.return_value = _discovery_response(included, total=5)

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=2,
            page_number=2,
        )

        assert result.total_count == 5
        assert len(result.items) == 2
        assert result.page == 2

    async def test_empty_modules(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Malformed/under-segmented module ids yield no documents.
        included = [
            {"type": "documents", "id": None},
            {"type": "documents"},
            {"type": "documents", "id": "proj1/onlytwo"},
        ]
        mock_client.get.return_value = _discovery_response(
            included, total=3, data_count=3
        )

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 0
        assert result.items == []

    async def test_linear_scan_walks_all_pages(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Discovery walks every document page exactly once."""
        page = {
            1: ([_module_resource("p/S/DocA")], 100),
            2: ([_module_resource("p/S/DocA"), _module_resource("p/S/DocB")], 100),
            3: ([_module_resource("p/S/DocB")], 30),
        }

        async def _mock_get(_path, *, params=None):
            included, rows = page[params["page[number]"]]
            return _discovery_response(included, total=230, data_count=rows)

        mock_client.get.side_effect = _mock_get

        result = await list_documents(
            mock_ctx,
            project_id="p",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 2
        pairs = {(d.space_id, d.document_name) for d in result.items}
        assert pairs == {("S", "DocA"), ("S", "DocB")}
        # Page 1, 2, 3 — exactly 3 API calls, no extra probes.
        assert mock_client.get.call_count == 3

    async def test_linear_scan_stops_on_partial_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """A short final page (without totalCount/links.next) ends the loop."""
        # Partial page 2 (50 < 100) with no totalCount stops the scan.
        page = {
            1: ([_module_resource("p/S/DocA")], 100),
            2: ([_module_resource("p/S/DocB")], 50),
        }

        async def _mock_get(_path, *, params=None):
            included, rows = page[params["page[number]"]]
            return _discovery_response(included, data_count=rows)

        mock_client.get.side_effect = _mock_get

        result = await list_documents(
            mock_ctx,
            project_id="p",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 2
        assert mock_client.get.call_count == 2

    async def test_single_page_uses_one_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Multiple documents on a single page → one API call."""
        included = [
            _module_resource("p/S/Alpha"),
            _module_resource("p/S/Bravo"),
            _module_resource("p/S/Charlie"),
            _module_resource("p/S/Delta"),
        ]
        mock_client.get.return_value = _discovery_response(
            included, total=100, data_count=100
        )

        result = await list_documents(
            mock_ctx,
            project_id="p",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 4
        names = {d.document_name for d in result.items}
        assert names == {"Alpha", "Bravo", "Charlie", "Delta"}
        assert mock_client.get.call_count == 1

    async def test_cache_hit_skips_api_calls(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """A second call with the same project_id is served from cache."""
        mock_client.get.return_value = _discovery_response(
            [_module_resource("proj1/_default/Doc1")], total=1
        )

        first = await list_documents(
            mock_ctx, project_id="proj1", page_size=100, page_number=1
        )
        calls_after_first = mock_client.get.call_count

        second = await list_documents(
            mock_ctx, project_id="proj1", page_size=100, page_number=1
        )

        assert mock_client.get.call_count == calls_after_first
        assert [(d.space_id, d.document_name, d.type) for d in first.items] == [
            (d.space_id, d.document_name, d.type) for d in second.items
        ]

    async def test_cache_isolated_per_project(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Different project IDs do not share cache entries."""

        async def _mock_get(_path, *, params=None):
            # Return a distinct document per call so cross-project sharing shows up.
            return _discovery_response(
                [_module_resource(f"x/S/Doc{mock_client.get.call_count}")], total=1
            )

        mock_client.get.side_effect = _mock_get

        await list_documents(mock_ctx, project_id="projA", page_size=100, page_number=1)
        await list_documents(mock_ctx, project_id="projB", page_size=100, page_number=1)
        # Each project triggered its own fetch — no cross-project cache hit.
        assert mock_client.get.call_count == 2

    async def test_cache_expires_after_ttl(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After TTL elapses, the next call re-fetches from the API."""
        mock_client.get.return_value = _discovery_response(
            [_module_resource("proj1/_default/Doc1")], total=1
        )

        fake_now = [1000.0]

        def _fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr(_cache_mod, "_now", _fake_monotonic)

        await list_documents(mock_ctx, project_id="proj1", page_size=100, page_number=1)
        first_calls = mock_client.get.call_count

        # Advance time past the TTL.
        fake_now[0] += _cache_mod._DOCUMENT_LIST_TTL_SECONDS + 1.0

        await list_documents(mock_ctx, project_id="proj1", page_size=100, page_number=1)
        assert mock_client.get.call_count > first_calls

    async def test_api_params_include_query(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        call_args = mock_client.get.call_args
        params = call_args[1].get(
            "params",
            call_args[0][1] if len(call_args[0]) > 1 else {},
        )
        query = params["query"]
        # SQL GROUP BY collapses every heading to one row per document.
        assert query.startswith("SQL:(")
        assert "GROUP BY mod.c_uri" in query
        assert "wi.c_type = 'heading'" in query
        # Recycle-bin exclusion mirrors the Lucene type:heading default.
        assert "wi.c_deleted IS NOT TRUE" in query
        assert "p.c_id = 'proj1'" in query
        # module pulls document attributes, the dot-paths the editors' names;
        # the intermediate ``module`` must stay listed or documents drop out.
        assert params["include"] == "module,module.author,module.updatedBy"
        # Relationship names listed explicitly: sparse fieldsets drop them.
        assert params["fields[documents]"] == "type,status,updated,author,updatedBy"
        assert params["fields[users]"] == "name"
        # No sort=module: server-side sort costs more than client-side dedup.
        assert "sort" not in params

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await list_documents(
                mock_ctx,
                project_id="missing",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("Forbidden", status_code=403)

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_documents(
                mock_ctx,
                project_id="proj1",
                page_size=100,
                page_number=1,
            )


class TestGetDocument:
    """Tests for the ``get_document`` tool."""

    async def test_returns_document_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "documents",
                "id": "proj1/_default/SRS",
                "attributes": {
                    "id": "SRS",
                    "title": "Software Requirement Spec",
                    "type": "req_specification",
                    "status": "approved",
                    "updated": "2026-02-22T14:53:03.244Z",
                    "homePageContent": {
                        "type": "text/html",
                        "value": ("<p>This is the <strong>SRS</strong> document.</p>"),
                    },
                },
                "relationships": {
                    "author": {"data": {"type": "users", "id": "admin"}},
                    "updatedBy": {"data": {"type": "users", "id": "72c2462f"}},
                },
            },
            "included": [
                _user_resource("admin", "System Administrator"),
                _user_resource("72c2462f", "Dev Member"),
            ],
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            include_homepage_content_html=True,
        )

        assert isinstance(result, DocumentDetail)
        assert result.title == "Software Requirement Spec"
        assert result.type == "req_specification"
        assert result.status == "approved"
        assert result.updated == "2026-02-22T14:53:03.244Z"
        # Editor names resolved from the included users; ids never surface.
        assert result.author == "System Administrator"
        assert result.last_updated_by == "Dev Member"
        # Raw HTML round-trip: <p> and <strong> tags survive verbatim.
        assert result.content_html == (
            "<p>This is the <strong>SRS</strong> document.</p>"
        )
        assert result.custom_fields == {}

    async def test_editor_fields_default_empty(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """No relationships / included users → editor fields stay empty."""
        mock_client.get.return_value = {
            "data": {
                "attributes": {"title": "Doc", "type": "generic", "status": "draft"},
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
        )

        assert result.updated == ""
        assert result.author == ""
        assert result.last_updated_by == ""

    async def test_include_homepage_content_html_false_omits_content(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_homepage_content_html=False → body hidden even though ``@all``
        always carries it on the wire.
        """
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "id": "SRS",
                    "title": "SRS",
                    "type": "req_specification",
                    "status": "draft",
                    "homePageContent": {
                        "type": "text/html",
                        "value": "<p>should be ignored</p>",
                    },
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            include_homepage_content_html=False,
        )

        assert result.title == "SRS"
        assert result.type == "req_specification"
        assert result.status == "draft"
        assert result.content_html == ""

    async def test_include_homepage_content_html_returns_raw_html(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_homepage_content_html=True → raw HTML in content_html (no markdownify)."""  # noqa: E501
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "title": "Doc",
                    "type": "generic",
                    "status": "draft",
                    "homePageContent": {
                        "type": "text/html",
                        "value": "<p>body</p>",
                    },
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            include_homepage_content_html=True,
        )

        # Verbatim HTML; no Markdown conversion.
        assert result.content_html == "<p>body</p>"

    async def test_polarion_specific_markup_round_trips(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Core round-trip guarantee: Polarion spans / data-* attrs survive read, so
        the string can go back into update_document unchanged.
        """
        raw = (
            '<p>See <span class="polarion-rte-link" '
            'data-item-id="MCPT-7" data-scope="proj1">link</span>.</p>'
        )
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "title": "Doc",
                    "homePageContent": {"type": "text/html", "value": raw},
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            include_homepage_content_html=True,
        )

        assert result.content_html == raw

    async def test_encodes_document_name_with_spaces(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {"title": "Test", "id": "Test Doc"},
            },
        }

        await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Software Requirement Specification",
        )

        call_path = mock_client.get.call_args[0][0]
        assert "Software%20Requirement%20Specification" in call_path

    async def test_empty_content(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "id": "EmptyDoc",
                    "title": "Empty",
                    "homePageContent": {
                        "type": "text/html",
                        "value": "",
                    },
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="EmptyDoc",
            include_homepage_content_html=True,
        )

        assert result.content_html == ""

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_document(
                mock_ctx,
                project_id="proj1",
                space_id="_default",
                document_name="Missing",
            )

    async def test_no_content_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Missing homePageContent → content stays empty even when requested."""
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "id": "NoContent",
                    "title": "No Content",
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="NoContent",
            include_homepage_content_html=True,
        )

        assert result.content_html == ""

    async def test_custom_fields_populated_from_response(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Inline non-standard attributes flow through as ``custom_fields``."""
        rich_value = {"type": "text/html", "value": "<p>note</p>"}
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/_default/SRS",
                "attributes": {
                    # Standard attrs: excluded from custom_fields.
                    "title": "SRS",
                    "type": "req_specification",
                    "status": "approved",
                    # Inline customs: top-level keys.
                    "summary": rich_value,
                    "documentVersion": "1.0",
                    "reviewerName": "alice",
                    "complianceLevel": "L3",
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            include_homepage_content_html=False,
        )

        # Raw passthrough: rich-text and structured values stay verbatim.
        assert result.custom_fields == {
            "summary": rich_value,
            "documentVersion": "1.0",
            "reviewerName": "alice",
            "complianceLevel": "L3",
        }

    async def test_custom_fields_empty_when_only_standard_attrs(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """All-standard attributes → empty custom_fields dict."""
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "title": "Plain Doc",
                    "type": "generic",
                    "status": "draft",
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Plain",
            include_homepage_content_html=False,
        )

        assert result.custom_fields == {}

    async def test_sparse_fieldset_uses_all_token(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``fields[documents]=@all`` is sent regardless of the include flag."""
        mock_client.get.return_value = {
            "data": {
                "attributes": {"title": "x", "type": "generic", "status": "draft"},
            },
        }

        await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="A",
            include_homepage_content_html=False,
        )
        _, kwargs_a = mock_client.get.call_args
        assert kwargs_a["params"]["fields[documents]"] == "@all"
        # Editor names ride along on the same request.
        assert kwargs_a["params"]["include"] == "author,updatedBy"
        assert kwargs_a["params"]["fields[users]"] == "name"

        await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="B",
            include_homepage_content_html=True,
        )
        _, kwargs_b = mock_client.get.call_args
        assert kwargs_b["params"]["fields[documents]"] == "@all"


class TestReadDocumentParts:
    """Tests for the ``read_document_parts`` tool."""

    async def test_returns_document_parts(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/heading_MCPT-001",
                    "attributes": {
                        "id": "heading_MCPT-001",
                        "content": (
                            '<h1 id="polarion_wiki macro'
                            " name=module-workitem;"
                            'params=id=MCPT-001"></h1>'
                        ),
                        "type": "heading",
                    },
                    "relationships": {
                        "nextPart": {
                            "data": {
                                "type": "document_parts",
                                "id": "proj1/_default/SRS/workitem_MCPT-002",
                            }
                        },
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": "proj1/MCPT-001",
                            }
                        },
                    },
                },
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/workitem_MCPT-002",
                    "attributes": {
                        "id": "workitem_MCPT-002",
                        "content": (
                            '<div id="polarion_wiki macro'
                            " name=module-workitem;"
                            'params=id=MCPT-002"></div>'
                        ),
                        "type": "workitem",
                        "external": True,
                        "level": 0,
                        "layout": 0,
                    },
                    "relationships": {
                        "previousPart": {
                            "data": {
                                "type": "document_parts",
                                "id": "proj1/_default/SRS/heading_MCPT-001",
                            }
                        },
                        "nextPart": {
                            "data": {
                                "type": "document_parts",
                                "id": "proj1/_default/SRS/polarion_1",
                            }
                        },
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": "proj1/MCPT-002",
                            }
                        },
                    },
                },
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/polarion_1",
                    "attributes": {
                        "id": "polarion_1",
                        "content": "<p>Normal text content.</p>",
                        "type": "normal",
                    },
                    "relationships": {
                        "previousPart": {
                            "data": {
                                "type": "document_parts",
                                "id": "proj1/_default/SRS/workitem_MCPT-002",
                            }
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "type": "heading",
                        "title": "Introduction",
                        "status": "open",
                    },
                },
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "type": "requirement",
                        "title": "Login Feature",
                        "description": {
                            "type": "text/html",
                            "value": "<p>The system shall support login.</p>",
                        },
                        "status": "draft",
                    },
                },
            ],
            "meta": {"totalCount": 3},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.total_count == 3

        heading = result.items[0]
        assert isinstance(heading, DocumentPart)
        assert heading.id == "heading_MCPT-001"
        assert heading.type == "heading"
        assert heading.level == 1
        assert heading.title == "Introduction"
        assert heading.content == ""
        assert heading.work_item_id == "MCPT-001"
        assert heading.work_item_type == "heading"
        assert heading.work_item_status == "open"
        assert heading.external is False
        assert heading.next_part_id == "workitem_MCPT-002"

        work_item_part = result.items[1]
        assert work_item_part.id == "workitem_MCPT-002"
        assert work_item_part.type == "workitem"
        assert work_item_part.level == 0
        assert work_item_part.title == "Login Feature"
        assert work_item_part.content == ""
        assert "login" in work_item_part.description.lower()
        assert work_item_part.work_item_id == "MCPT-002"
        assert work_item_part.work_item_type == "requirement"
        assert work_item_part.work_item_status == "draft"
        assert work_item_part.external is True
        assert work_item_part.next_part_id == "polarion_1"

        normal_part = result.items[2]
        assert normal_part.type == "normal"
        assert normal_part.level == 0
        assert normal_part.title == ""
        assert "Normal text content" in normal_part.content
        assert normal_part.work_item_id == ""
        assert normal_part.work_item_type == ""
        assert normal_part.work_item_status == ""
        assert normal_part.external is False
        assert normal_part.next_part_id == ""

    async def test_pagination_params_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=10,
            page_number=2,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["fields[document_parts]"] == "@all"
        assert kwargs["params"]["include"] == "workItem"
        assert kwargs["params"]["page[size]"] == 10
        assert kwargs["params"]["page[number]"] == 2

    async def test_uses_tight_workitem_fieldset(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Embedded work items use WORK_ITEM_PART_FIELDS, not ``@all`` — ``@all`` ships
        KBs of unused inline customs per page.
        """
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert (
            kwargs["params"]["fields[workitems]"]
            == "title,type,status,description,outlineNumber"
        )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await read_document_parts(
                mock_ctx,
                project_id="proj1",
                space_id="_default",
                document_name="Missing",
                page_size=100,
                page_number=1,
            )

    async def test_string_content_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Plain string content (not dict) is handled for normal parts."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/polarion_42",
                    "attributes": {
                        "id": "polarion_42",
                        "content": "<p>Plain string content.</p>",
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert len(result.items) == 1
        assert result.items[0].type == "normal"
        assert "Plain string content" in result.items[0].content

    async def test_tof_part_reclassified_from_id_prefix(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Polarion reports TOF as ``type=normal``; the ID prefix wins."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/tof_20",
                    "attributes": {
                        "id": "tof_20",
                        "content": "",
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert result.items[0].type == "tof"

    async def test_page_break_part_reclassified_from_id_prefix(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``pagebreak_*`` IDs are surfaced as ``page_break`` parts."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/pagebreak_23",
                    "attributes": {
                        "id": "pagebreak_23",
                        "content": "",
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert result.items[0].type == "page_break"

    async def test_normal_part_without_special_prefix_stays_normal(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Plain ``polarion_*`` IDs must not be reclassified."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/polarion_99",
                    "attributes": {
                        "id": "polarion_99",
                        "content": "<p>body</p>",
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert result.items[0].type == "normal"

    async def test_outline_number_surfaced_on_heading_part(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Heading parts expose Polarion's ``outlineNumber`` verbatim."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/heading_MCPT-149",
                    "attributes": {
                        "id": "heading_MCPT-149",
                        "content": (
                            '<h3 id="polarion_wiki macro '
                            'name=module-workitem;params=id=MCPT-149"></h3>'
                        ),
                        "type": "heading",
                    },
                    "relationships": {
                        "workItem": {
                            "data": {"type": "workitems", "id": "proj1/MCPT-149"},
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-149",
                    "attributes": {
                        "type": "heading",
                        "title": "Purpose",
                        "status": "open",
                        "outlineNumber": "1.1",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        heading = result.items[0]
        assert heading.type == "heading"
        assert heading.outline_number == "1.1"

    async def test_richpage_link_in_normal_part_becomes_markdown_link(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``polarion-rte-link`` spans inside normal parts surface as Markdown."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/polarion_2",
                    "attributes": {
                        "id": "polarion_2",
                        "content": (
                            '<p>Browse using <span class="polarion-rte-link" '
                            'data-type="richPage" '
                            'data-item-name="Coverage" '
                            'data-space-name="Design"></span>.</p>'
                        ),
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert "[Coverage](polarion:Design/Coverage)" in result.items[0].content

    async def test_total_count_floor_when_api_returns_zero(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """totalCount=0 with items present uses item count."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/_default/Doc/heading_MCPT-001",
                    "attributes": {
                        "id": "heading_MCPT-001",
                        "content": "<h1>Intro</h1>",
                        "type": "heading",
                    },
                },
                {
                    "id": "proj1/_default/Doc/workitem_MCPT-002",
                    "attributes": {
                        "id": "workitem_MCPT-002",
                        "content": "",
                        "type": "workitem",
                    },
                },
            ],
            "meta": {"totalCount": 0},  # Polarion quirk
        }

        result = await read_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert len(result.items) == 2
        assert result.total_count >= 2


class TestReadDocument:
    """Tests for the ``read_document`` tool."""

    # End-to-end wiring: exercises read_document_parts via client.get.
    async def test_end_to_end_renders_document(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Real fetch path: stub the HTTP layer, verify the rendered Markdown."""
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/heading_MCPT-001",
                    "attributes": {
                        "id": "heading_MCPT-001",
                        "content": (
                            '<h1 id="polarion_wiki macro'
                            " name=module-workitem;"
                            'params=id=MCPT-001"></h1>'
                        ),
                        "type": "heading",
                    },
                    "relationships": {
                        "workItem": {
                            "data": {"type": "workitems", "id": "proj1/MCPT-001"},
                        },
                    },
                },
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/workitem_MCPT-002",
                    "attributes": {
                        "id": "workitem_MCPT-002",
                        "content": "",
                        "type": "workitem",
                    },
                    "relationships": {
                        "workItem": {
                            "data": {"type": "workitems", "id": "proj1/MCPT-002"},
                        },
                    },
                },
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/polarion_1",
                    "attributes": {
                        "id": "polarion_1",
                        "content": (
                            "<p>Some prose between the work item and a heading.</p>"
                        ),
                        "type": "normal",
                    },
                    "relationships": {},
                },
                {
                    "type": "document_parts",
                    "id": "proj1/_default/SRS/polarion_2",
                    "attributes": {
                        "id": "polarion_2",
                        # Empty paragraph — should not appear in the render.
                        "content": "<p></p>",
                        "type": "normal",
                    },
                    "relationships": {},
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "type": "heading",
                        "title": "Introduction",
                        "status": "open",
                    },
                },
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "type": "requirement",
                        "title": "Login Feature",
                        "description": {
                            "type": "text/html",
                            "value": "<p>The system shall support login.</p>",
                        },
                        "status": "draft",
                    },
                },
            ],
            "meta": {"totalCount": 4},
        }

        result = await read_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, DocumentReadResult)
        assert result.part_count == 4
        assert result.total_parts == 4
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False

        # Heading, work item lead-in + description, prose; empty paragraph skipped.
        assert result.content == (
            "# Introduction\n"
            "\n"
            "**Login Feature** (`MCPT-002`)\n"
            "\n"
            "The system shall support login.\n"
            "\n"
            "Some prose between the work item and a heading."
        )

    async def test_pagination_params_forwarded_to_http(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``page_size`` / ``page_number`` reach the underlying client.get call."""
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await read_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=25,
            page_number=3,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["page[size]"] == 25
        assert kwargs["params"]["page[number]"] == 3

    async def test_empty_document_returns_empty_content(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        result = await read_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Empty",
            page_size=100,
            page_number=1,
        )

        assert result.content == ""
        assert result.part_count == 0
        assert result.total_parts == 0
        assert result.has_more is False

    async def test_not_found_propagates_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Delegation to ``read_document_parts`` surfaces its ValueError verbatim."""
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await read_document(
                mock_ctx,
                project_id="proj1",
                space_id="_default",
                document_name="Missing",
                page_size=100,
                page_number=1,
            )

    # Render-rule tests: isolated via monkeypatched read_document_parts.
    async def test_workitem_with_description_renders_lead_in_plus_body(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(
                    type_="workitem",
                    title="Login Feature",
                    description="The system shall support login.",
                    work_item_id="MCPT-002",
                ),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == (
            "**Login Feature** (`MCPT-002`)\n\nThe system shall support login."
        )

    async def test_workitem_without_description_renders_lead_in_only(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(
                    type_="workitem",
                    title="Stub Requirement",
                    description="",
                    work_item_id="MCPT-099",
                ),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "**Stub Requirement** (`MCPT-099`)"

    async def test_workitem_empty_title_and_description_renders_bare_id(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive guard for sparse-data work items (rare but observed)."""
        _stub_parts(
            monkeypatch,
            [
                _make_part(
                    type_="workitem",
                    title="",
                    description="",
                    work_item_id="MCPT-7",
                ),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "`MCPT-7`"

    async def test_empty_normal_parts_skipped(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Polarion's empty placeholder paragraphs (polarion_3, _4, ...) drop out."""
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="heading", title="Section", level=2),
                _make_part(type_="normal", content="   "),  # whitespace-only
                _make_part(type_="normal", content=""),
                _make_part(type_="normal", content="Real content."),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "## Section\n\nReal content."

    async def test_toc_emits_widget_placeholder(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="heading", title="Top", level=1),
                _make_part(type_="toc", content=""),
                _make_part(type_="normal", content="After TOC."),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == (
            "# Top\n\n*[Table of Contents (Polarion widget)]*\n\nAfter TOC."
        )

    async def test_tof_emits_widget_placeholder(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="heading", title="Top", level=1),
                _make_part(type_="tof", content=""),
                _make_part(type_="normal", content="After TOF."),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == (
            "# Top\n\n*[Table of Figures (Polarion widget)]*\n\nAfter TOF."
        )

    async def test_page_break_emits_thematic_break(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="normal", content="Before."),
                _make_part(type_="page_break", content=""),
                _make_part(type_="normal", content="After."),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "Before.\n\n---\n\nAfter."

    async def test_wikiblock_lifts_macro_name_as_info_string(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_parts(
            monkeypatch,
            [
                _make_part(
                    type_="wikiblock",
                    content='```\n#documentPanel(true "approved")\n```',
                ),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == (
            '```documentPanel\n#documentPanel(true "approved")\n```'
        )

    async def test_wikiblock_without_macro_falls_back_to_plain_fence(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wikiblock that doesn't start with ``#name(`` keeps its plain fence."""
        _stub_parts(
            monkeypatch,
            [_make_part(type_="wikiblock", content="```\njust text\n```")],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "```\njust text\n```"

    async def test_empty_wikiblock_skipped(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only wikiblock is dropped, mirroring empty normal parts."""
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="heading", title="Top", level=1),
                _make_part(type_="wikiblock", content="   \n  "),
                _make_part(type_="normal", content="After block."),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "# Top\n\nAfter block."

    async def test_heading_level_clamp_low(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``level=0`` (the model default for non-headings) becomes ``#``."""
        _stub_parts(
            monkeypatch,
            [_make_part(type_="heading", title="Corrupt", level=0)],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "# Corrupt"

    async def test_heading_level_clamp_high(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``level`` above 6 clamps to ``######`` to stay valid Markdown."""
        _stub_parts(
            monkeypatch,
            [_make_part(type_="heading", title="Deep", level=10)],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "###### Deep"

    async def test_heading_with_outline_number_prefix(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outline number is prefixed before the heading title."""
        _stub_parts(
            monkeypatch,
            [
                _make_part(
                    type_="heading", title="Purpose", level=3, outline_number="1.1"
                ),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "### 1.1 Purpose"

    async def test_heading_without_outline_number_unchanged(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Headings without an outline number render as before."""
        _stub_parts(
            monkeypatch,
            [_make_part(type_="heading", title="Standalone", level=2)],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "## Standalone"

    async def test_newline_collapse(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs of 3+ blank lines from joined chunks collapse to a single blank."""
        _stub_parts(
            monkeypatch,
            [
                _make_part(type_="normal", content="A\n\n\n\nB"),
                _make_part(type_="normal", content="C"),
            ],
        )

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=1,
        )

        assert result.content == "A\n\nB\n\nC"

    async def test_pagination_metadata_propagated(
        self, mock_ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``page``/``page_size``/``total_parts``/``has_more`` echo inner page."""
        stub = AsyncMock(
            return_value=PaginatedResult[DocumentPart](
                items=[_make_part(type_="normal", content="X")],
                total_count=250,
                page=2,
                page_size=100,
                has_more=True,
            )
        )
        monkeypatch.setattr(_mod, "read_document_parts", stub)

        result = await read_document(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            page_size=100,
            page_number=2,
        )

        assert result.part_count == 1
        assert result.total_parts == 250
        assert result.page == 2
        assert result.page_size == 100
        assert result.has_more is True


class TestReadDocumentFieldValidation:
    """Field constraints on ``read_document`` — direct calls bypass FastMCP's JSON
    Schema gate, so rebuild a ``TypeAdapter`` per parameter.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(read_document)
        sig = inspect.signature(read_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_size_accepts_max(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100

    def test_page_number_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_number").validate_python(0)

    def test_page_number_accepts_one(self) -> None:
        assert self._adapter_for("page_number").validate_python(1) == 1


class TestBuildUpdateDocumentPayload:
    """Tests for the private ``_build_update_document_payload`` helper."""

    def test_only_set_fields_appear_in_attributes(self) -> None:
        # None fields stay unserialized so JSON:API omit-preserve applies.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
        )

        # data is a single dict (PATCH-shape), NOT a list.
        assert payload == {
            "data": {
                "type": "documents",
                "id": "MyProj/Requirements/SRS",
                "attributes": {"title": "New Title"},
            }
        }
        assert isinstance(payload["data"], dict)

    def test_all_three_fields_serialised_when_set(self) -> None:
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="req_specification",
        )

        data = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {
            "title": "T",
            "status": "approved",
            "type": "req_specification",
        }

    def test_no_attributes_when_all_fields_are_none(self) -> None:
        # All-None yields no attributes key; the tool rejects this upstream.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
        )

        data = cast(dict[str, object], payload["data"])
        assert "attributes" not in data
        assert data["id"] == "MyProj/S/D"

    def test_homepagecontent_omitted_when_not_passed(self) -> None:
        # Omitting home_page_content_html leaves the body untouched.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="generic",
        )
        body_str = repr(payload)
        assert "homePageContent" not in body_str

    def test_home_page_content_html_wrapped_verbatim(self) -> None:
        # Raw HTML pass-through — no sanitization, no markdownify.
        raw = '<p>x <span class="polarion-rte-link" data-item-id="MCPT-1">y</span></p>'
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=raw,
        )

        data = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes["homePageContent"] == {"type": "text/html", "value": raw}

    def test_document_name_with_slashes_preserved_verbatim(self) -> None:
        # JSON body IDs must NOT be URL-encoded.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="Design",
            document_name="Folder/Sub Doc",
            title="t",
            status=None,
            type=None,
        )

        data = cast(dict[str, object], payload["data"])
        assert data["id"] == "MyProj/Design/Folder/Sub Doc"

    def test_custom_fields_inlined_in_document_patch(self) -> None:
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"complianceLevel": "L3", "reviewerName": "alice"},
        )
        data = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {"complianceLevel": "L3", "reviewerName": "alice"}

    def test_custom_fields_collision_raises_for_document_standard(self) -> None:
        # ``moduleFolder`` is in the document standard set — collision.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_update_document_payload(
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={"moduleFolder": "Other"},
            )


class TestUpdateDocumentValidation:
    """Tool-layer validation that protects against empty / no-op PATCHes."""

    async def test_no_fields_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="at least one"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_workflow_action_alone_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="workflow_action alone"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
                workflow_action="approve",
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_custom_fields_unresolvable_type_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Doc exists but carries no type attr: cannot key the schema guard, so the
        # write is refused rather than validated against an empty "" schema.
        mock_client.get.return_value = {"data": {"attributes": {"title": "D"}}}
        with pytest.raises(RuntimeError, match="no resolvable type"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={"documentVersion": "0.2"},
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_workflow_action_with_status_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action paired with at least one attribute is OK.
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_custom_fields_alone_satisfies_at_least_one_check(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # custom_fields counts as a body field; the type sample knows the key.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "D", "type": "generic"}}
        }
        _cache_mod.store_document_type_custom_keys(
            "MyProj", "generic", frozenset({"documentVersion"})
        )
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"documentVersion": "0.2"},
            workflow_action=None,
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_workflow_action_with_custom_fields_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action + custom_fields-only satisfies the body-field check.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "D", "type": "generic"}}
        }
        _cache_mod.store_document_type_custom_keys(
            "MyProj", "generic", frozenset({"documentVersion"})
        )
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"documentVersion": "0.2"},
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_workflow_action_with_home_page_content_html_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """workflow_action + home_page_content_html-only is OK.

        home_page_content_html counts as the required attribute, so the
        body-field check must not regress to title/status/type/customs only.
        """
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html='<p id="b1">new body</p>',
            custom_fields=None,
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True
        # Payload carries both the body and the workflow query param.
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["homePageContent"] == {
            "type": "text/html",
            "value": '<p id="b1">new body</p>',
        }

    async def test_custom_fields_collision_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={"title": "y"},
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_custom_fields_homepagecontent_collision_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """`homePageContent` collision raises — allowing it via custom_fields would
        bypass the explicit parameter and its empty-string guard.
        """
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={
                    "homePageContent": {
                        "type": "text/html",
                        "value": "<p>sneak</p>",
                    }
                },
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()


class TestUpdateDocumentDryRun:
    """Tests for ``update_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=True,
        )

        mock_client.patch.assert_not_called()
        assert isinstance(result, DocumentUpdateResult)
        assert result.updated is False
        assert result.dry_run is True
        assert result.payload_preview is not None
        data = cast(dict[str, object], result.payload_preview["data"])
        assert data["type"] == "documents"
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {"title": "New Title"}


class TestUpdateDocumentHappyPath:
    """Tests for a successful ``update_document`` call."""

    async def test_returns_updated_true_on_204(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        assert isinstance(result, DocumentUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.payload_preview is None

    async def test_patch_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="My Doc",
            title="T",
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        args, kwargs = mock_client.patch.call_args
        expected_path = "/projects/MyProj/spaces/Requirements/documents/My%20Doc"
        assert args == (expected_path,)
        body = kwargs["json"]
        assert isinstance(body["data"], dict)
        assert body["data"]["id"] == "MyProj/Requirements/My Doc"
        assert body["data"]["attributes"] == {"title": "T"}

    async def test_workflow_action_appended_as_query_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action="approve",
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        path = args[0]
        assert path.startswith("/projects/MyProj/spaces/S/documents/D")
        assert "workflowAction=approve" in path

    async def test_home_page_content_html_is_sent_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """home_page_content_html passes through with no sanitization."""
        mock_client.patch.return_value = {}

        raw = (
            '<p id="b1">Body with <span class="polarion-rte-link" '
            'data-item-id="MCPT-1">link</span></p>'
        )
        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=raw,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        assert attributes["homePageContent"] == {"type": "text/html", "value": raw}
        # Nothing else slipped in.
        assert set(attributes.keys()) == {"homePageContent"}

    async def test_home_page_content_html_omitted_when_not_passed(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Omit-preserve: no homePageContent in body when not passed."""
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="generic",
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        body_str = repr(kwargs["json"])
        assert "homePageContent" not in body_str

    async def test_home_page_content_html_empty_string_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Empty string is rejected at the tool layer (body-wipe guard)."""
        with pytest.raises(ValueError, match="would wipe"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html="",
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )
        mock_client.patch.assert_not_called()

    @pytest.mark.parametrize("whitespace", ["   ", "\n", "\t", "\n\n  \t"])
    async def test_home_page_content_html_whitespace_raises(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        whitespace: str,
    ) -> None:
        """Whitespace-only strings strip to '' on the server, so reject too."""
        with pytest.raises(ValueError, match="would wipe"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=whitespace,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )
        mock_client.patch.assert_not_called()

    async def test_home_page_content_html_alone_passes_has_attrs_guard(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """home_page_content_html alone counts as a body field."""
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html='<p id="b1">x</p>',
            custom_fields=None,
            workflow_action=None,
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_explicit_empty_title_is_serialized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # title="" (unlike None) is sent in attributes, clearing it server-side.
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="",
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        assert attributes == {"title": ""}

    async def test_path_url_encodes_special_chars_in_space_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="My Space",
            document_name="D",
            title="t",
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        assert args == ("/projects/MyProj/spaces/My%20Space/documents/D",)

    async def test_workflow_action_url_encoded_when_special(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Action IDs with reserved chars must be URL-encoded.
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            home_page_content_html=None,
            custom_fields=None,
            workflow_action="needs review",
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        path = args[0]
        # Space encodes to "+" or "%20"; Polarion accepts either.
        assert "workflowAction=needs+review" in path or (
            "workflowAction=needs%20review" in path
        )


class TestUpdateDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )

    async def test_404_raises_value_error_with_doc_in_message(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="ghost-document") as exc_info:
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="ghost-space",
                document_name="ghost-document",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )
        assert "ghost-space" in str(exc_info.value)

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )


class TestUpdateDocumentFieldValidation:
    """Verify ``min_length=1`` constraints on required path parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_document)
        sig = inspect.signature(update_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_space_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_document_name_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("document_name").validate_python("")

    def test_optional_metadata_fields_accept_none(self) -> None:
        for name in ("title", "status", "type", "workflow_action"):
            assert self._adapter_for(name).validate_python(None) is None

    def test_home_page_content_html_rejects_overlong_input(self) -> None:
        """``max_length=MAX_BODY_HTML_LEN`` defends against runaway HTML."""
        adapter = self._adapter_for("home_page_content_html")
        assert adapter.validate_python("<p>ok</p>") == "<p>ok</p>"
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


class TestUpdateDocumentPitfallDocumentation:
    """Lock the macro-div pitfall into ``update_document.__doc__`` — reproduced on
    the live testdrive server, user-facing (other MCP hosts never load CLAUDE.md).
    Anchorless-block pitfall intentionally absent: ids now auto-stamped.
    """

    def test_docstring_warns_about_macro_div_module_relationship_gap(self) -> None:
        """Macro <div> reference injected via update_document leaves module unset."""
        document = update_document.__doc__ or ""
        assert "polarion_wiki macro" in document, (
            "update_document docstring must mention the polarion_wiki macro pitfall"
        )
        assert "module" in document, (
            "update_document docstring must surface that the work item's module "
            "relationship stays unset after a macro <div> injection"
        )

    def test_docstring_directs_get_before_update(self) -> None:
        """Partial-PATCH semantics demand observing current state first."""
        document = update_document.__doc__ or ""
        assert "get_document" in document, (
            "update_document docstring must direct callers to read the "
            "document before patching it"
        )
        assert "BEFORE" in document, (
            "update_document docstring must state the read happens BEFORE the update"
        )


class TestBuildCreateDocumentPayload:
    """Tests for the private ``_build_create_document_payload`` helper."""

    def test_minimal_payload_has_only_required_attrs(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="My Spec",
            type="req_specification",
            home_page_content_html="",
            status=None,
        )

        assert payload == {
            "data": [
                {
                    "type": "documents",
                    "attributes": {
                        "moduleName": "MySpec",
                        "title": "My Spec",
                        "type": "req_specification",
                    },
                }
            ]
        }
        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert "status" not in attributes
        assert "homePageContent" not in attributes

    def test_status_attached_when_set(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status="draft",
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["status"] == "draft"

    def test_home_page_content_wrapped_as_html_block(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="<p>Hi</p>",
            status=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["homePageContent"] == {
            "type": "text/html",
            "value": "<p>Hi</p>",
        }

    def test_skips_none_status_and_empty_body(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert set(attributes.keys()) == {"moduleName", "title", "type"}

    def test_custom_fields_inlined_alongside_standard_attrs(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status=None,
            custom_fields={"projectOwner": "alice", "phase": "design"},
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["projectOwner"] == "alice"
        assert attributes["phase"] == "design"

    def test_custom_fields_collision_with_standard_attr_raises(self) -> None:
        with pytest.raises(ValueError, match="title"):
            _build_create_document_payload(
                module_name="MySpec",
                title="t",
                type="generic",
                home_page_content_html="",
                status=None,
                custom_fields={"title": "duplicate"},
            )


class TestCreateDocumentDryRun:
    """Tests for ``create_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="Dry test",
            type="req_specification",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, DocumentCreateResult)
        assert result.dry_run is True
        assert result.created is False
        assert result.document_name is None
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview, dict)
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {
            "moduleName": "MySpec",
            "title": "Dry test",
            "type": "req_specification",
        }


class TestCreateDocumentHappyPath:
    """Tests for a successful ``create_document`` call."""

    async def test_returns_document_name_on_201(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "documents",
                    "id": "MyProj/_default/MySpec",
                    "links": {"self": "..."},
                }
            ]
        }

        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="Real",
            type="req_specification",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert isinstance(result, DocumentCreateResult)
        assert result.created is True
        assert result.dry_run is False
        assert result.document_name == "MySpec"
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="req_specification",
            status="draft",
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        assert args == ("/projects/MyProj/spaces/_default/documents",)
        body = kwargs["json"]
        item = body["data"][0]
        assert item["type"] == "documents"
        assert item["attributes"]["moduleName"] == "MySpec"
        assert item["attributes"]["title"] == "t"
        assert item["attributes"]["type"] == "req_specification"
        assert item["attributes"]["status"] == "draft"

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "Proj With Space/My Space/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="Proj With Space",
            space_id="My Space",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        args, _ = mock_client.post.call_args
        assert args == ("/projects/Proj%20With%20Space/spaces/My%20Space/documents",)

    async def test_home_page_content_markdown_converted_and_sanitized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="**bold** [link](https://example.com)",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body = kwargs["json"]["data"][0]["attributes"]["homePageContent"]
        assert body["type"] == "text/html"
        assert "<strong>bold</strong>" in body["value"]
        assert 'href="https://example.com"' in body["value"]

    async def test_home_page_content_stamps_unique_block_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Every block-level element from the target set gets a unique
        ``polarion_mcp_N`` id; headings are intentionally left bare so
        Polarion can rewrite them to the macro form on save.
        """
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="# H\n\npara1\n\n* item\n\npara2",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body_html = kwargs["json"]["data"][0]["attributes"]["homePageContent"]["value"]
        assert '<p id="polarion_mcp_0">' in body_html
        assert '<ul id="polarion_mcp_1">' in body_html
        assert '<p id="polarion_mcp_2">' in body_html
        assert "<h1>" in body_html
        assert "<h1 id=" not in body_html

    async def test_defensive_guard_raises_when_stamp_leaves_anchorless(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stamp_block_ids regression that leaves a block anchorless is
        caught by the trailing first_anchorless_block guard before POST.
        """
        monkeypatch.setattr(_mod, "stamp_block_ids", lambda html: html)
        with pytest.raises(RuntimeError, match="anchorless block"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content="plain para",
                custom_fields=None,
                dry_run=False,
            )
        mock_client.post.assert_not_called()

    async def test_home_page_content_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="[click](javascript:alert(1))",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body_html = kwargs["json"]["data"][0]["attributes"]["homePageContent"]["value"]
        assert 'href="javascript:' not in body_html
        assert "href='javascript:" not in body_html

    async def test_document_name_with_slashes_extracted_correctly(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``split_module_id`` preserves slashes in the document_name segment."""
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/Folder/Sub/Doc"}]
        }

        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="Folder/Sub/Doc",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert result.document_name == "Folder/Sub/Doc"

    async def test_invalidates_documents_cache_on_success(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``create_document`` drops the project's docs cache entry on 201."""
        _cache_mod.store_cached_documents("MyProj", [("_default", "OldDoc")])
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert _cache_mod.get_cached_documents("MyProj") is None

    async def test_does_not_invalidate_cache_on_failure(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Cache is preserved when the POST raises — no half-state change."""
        _cache_mod.store_cached_documents("MyProj", [("_default", "OldDoc")])
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

        cached = _cache_mod.get_cached_documents("MyProj")
        assert cached == [("_default", "OldDoc")]
        _cache_mod.invalidate_documents_cache("MyProj")


class TestCreateDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_404_raises_value_error_mentioning_project_and_space(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="space") as exc_info:
            await create_document(
                mock_ctx,
                project_id="ghost",
                space_id="ghost_space",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )
        assert "ghost" in str(exc_info.value)
        assert "ghost_space" in str(exc_info.value)

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("conflict", status_code=409)

        with pytest.raises(RuntimeError, match="conflict"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )


class TestCreateDocumentResponseParsing:
    """Tests for unexpected 2xx response shapes from Polarion."""

    async def test_empty_data_array_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_data_not_a_list_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": {"id": "MyProj/_default/MySpec"}}

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_two_segment_id_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``split_module_id`` returns ``('','')`` for under-3-segment IDs."""
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/MySpec"}]
        }

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )


class TestCreateDocumentFieldValidation:
    """Verify ``min_length`` / ``max_length`` constraints on parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_document)
        sig = inspect.signature(create_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_space_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_module_name_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("module_name").validate_python("")

    def test_module_name_accepts_non_empty(self) -> None:
        assert self._adapter_for("module_name").validate_python("MySpec") == "MySpec"

    def test_title_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("title").validate_python("")

    def test_type_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("type").validate_python("")

    def test_home_page_content_rejects_overlong_input(self) -> None:
        adapter = self._adapter_for("home_page_content")
        assert adapter.validate_python("hello") == "hello"
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


class TestCreateDocumentRegistration:
    """The tool must be registered on the FastMCP server instance."""

    async def test_create_document_tool_registered(self) -> None:
        tools = await mcp.list_tools()
        assert any(tool.name == "create_document" for tool in tools)


class TestCreateDocumentDocstringGuidance:
    """Lock load-bearing steers into the public docstring.

    Enum values (standard and custom-field) are tool-guarded, so no
    enum-resolution steer is needed; uniqueness is not.
    """

    def test_docstring_mentions_module_name_uniqueness(self) -> None:
        document = create_document.__doc__ or ""
        assert "unique" in document.lower()
        assert "409" in document or "conflict" in document.lower()


class TestEnumGuardCreateDocument:
    """Integration: ``create_document`` rejects ghost document types."""

    async def test_unlisted_type_raises_before_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = _enum_get_response(
            ["systemRequirementSpecification", "softwareRequirementSpecification"]
        )

        with pytest.raises(ValueError, match="type='productRequirementSpecification'"):
            await _call_create_doc(
                mock_ctx,
                module_name="NewSpec",
                type="productRequirementSpecification",
            )
        mock_client.post.assert_not_called()

    async def test_custom_fields_pass_when_in_type_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: type options, the heading + include=module sample, then the
        # enum-value probe for the key (404 = not an enum field, defers).
        dtype = "systemRequirementSpecification"
        mock_client.get.side_effect = [
            _enum_get_response([dtype]),
            {
                "data": [{"type": "workitems"}],
                "included": [
                    {"type": "documents", "attributes": {"type": dtype, "version": "1"}}
                ],
            },
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/NewSpec"}]
        }

        result = await _call_create_doc(
            mock_ctx, module_name="NewSpec", type=dtype, custom_fields={"version": "2"}
        )

        assert result.created is True  # type: ignore[attr-defined]
        mock_client.post.assert_awaited_once()

    async def test_custom_field_enum_value_rejected_on_create(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: type options, heading sample (knows docRisk), then the
        # enum-value probe -- 'severe' is not among the field's options.
        dtype = "generic"
        mock_client.get.side_effect = [
            _enum_get_response([dtype]),
            {
                "data": [{"type": "workitems"}],
                "included": [
                    {
                        "type": "documents",
                        "attributes": {"type": dtype, "docRisk": "low"},
                    }
                ],
            },
            _enum_get_response(["high", "moderate", "low"]),
        ]

        with pytest.raises(ValueError, match=r"'docRisk'.*'severe'"):
            await _call_create_doc(
                mock_ctx,
                module_name="NewSpec",
                type=dtype,
                custom_fields={"docRisk": "severe"},
            )
        mock_client.post.assert_not_called()

    async def test_custom_fields_reject_ghost_key(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        dtype = "systemRequirementSpecification"
        sample = {
            "data": [{"type": "workitems"}],
            "included": [
                {"type": "documents", "attributes": {"type": dtype, "version": "1"}}
            ],
        }
        mock_client.get.side_effect = [_enum_get_response([dtype]), sample, sample]

        with pytest.raises(ValueError, match="ghostField"):
            await _call_create_doc(
                mock_ctx,
                module_name="NewSpec",
                type=dtype,
                custom_fields={"ghostField": "x"},
            )
        mock_client.post.assert_not_called()

    async def test_custom_fields_fail_closed_on_empty_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        dtype = "systemRequirementSpecification"
        mock_client.get.side_effect = [
            _enum_get_response([dtype]),
            {"data": []},
            {"data": []},
        ]

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _call_create_doc(
                mock_ctx,
                module_name="NewSpec",
                type=dtype,
                custom_fields={"version": "2"},
            )
        mock_client.post.assert_not_called()


class TestEnumGuardUpdateDocument:
    """Integration: ``update_document`` rejects ghost type / status."""

    async def test_unlisted_status_raises_before_patch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = _enum_get_response(["draft", "approved"])

        with pytest.raises(ValueError, match="status='ghost'"):
            await _call_update_doc(mock_ctx, status="ghost")
        mock_client.patch.assert_not_called()

    async def test_unknown_custom_field_key_raises_via_priming_get(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: resolve the doc's type, then the type sample (+ bypass-retry).
        # The sample knows only doc_risk, so ghost_key is rejected.
        type_resp = {"data": {"attributes": {"title": "x", "type": "generic"}}}
        sample = {
            "data": [{"type": "workitems"}],
            "included": [
                {"type": "documents", "attributes": {"type": "generic", "doc_risk": 3}}
            ],
        }
        mock_client.get.side_effect = [type_resp, sample, sample]

        with pytest.raises(ValueError, match="ghost_key"):
            await _call_update_doc(mock_ctx, custom_fields={"ghost_key": 1})
        mock_client.patch.assert_not_called()

    async def test_custom_field_enum_value_rejected_on_update(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: resolve the doc's type, type sample (knows doc_risk), enum probe.
        type_resp = {"data": {"attributes": {"title": "x", "type": "generic"}}}
        sample = {
            "data": [{"type": "workitems"}],
            "included": [
                {
                    "type": "documents",
                    "attributes": {"type": "generic", "doc_risk": "low"},
                }
            ],
        }
        mock_client.get.side_effect = [
            type_resp,
            sample,
            _enum_get_response(["high", "moderate", "low"]),
        ]

        with pytest.raises(ValueError, match=r"'doc_risk'.*'severe'"):
            await _call_update_doc(mock_ctx, custom_fields={"doc_risk": "severe"})
        mock_client.patch.assert_not_called()

    async def test_known_custom_field_key_passes_guard(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Resolve the doc's type; the cached type schema knows doc_risk.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "type": "generic"}}
        }
        _cache_mod.store_document_type_custom_keys(
            "MyProj", "generic", frozenset({"doc_risk"})
        )

        result = await _call_update_doc(
            mock_ctx, custom_fields={"doc_risk": 9}, dry_run=True
        )
        assert result.dry_run is True  # type: ignore[attr-defined]


def _doc_body_value(result: object) -> str:
    """Pull ``homePageContent.value`` out of an update_document dry-run preview."""
    preview = result.payload_preview  # type: ignore[attr-defined]
    assert preview is not None
    data = cast(dict[str, object], preview["data"])
    attrs = cast(dict[str, object], data["attributes"])
    body = cast(dict[str, object], attrs["homePageContent"])
    return cast(str, body["value"])


class TestUpdateDocumentAnchorlessGuard:
    """Integration: ``update_document`` auto-stamps anchorless body blocks."""

    async def test_anchorless_paragraph_is_stamped(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        result = await _call_update_doc(
            mock_ctx, home_page_content_html="<p>Note</p>", dry_run=True
        )
        body = _doc_body_value(result)
        assert "Note" in body
        assert 'id="polarion_mcp_' in body
        mock_client.patch.assert_not_called()

    async def test_anchored_body_sent_verbatim(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        """An already-anchored body round-trips byte-for-byte: stamp_block_ids
        short-circuits before reserializing, so ``&nbsp;`` is not mangled.
        """
        raw = '<p id="polarion_mcp_1">Note&nbsp;here</p>'
        result = await _call_update_doc(
            mock_ctx, home_page_content_html=raw, dry_run=True
        )
        assert _doc_body_value(result) == raw

    async def test_heading_only_passes(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        result = await _call_update_doc(
            mock_ctx, home_page_content_html="<h1>Title</h1>", dry_run=True
        )
        assert result.dry_run is True  # type: ignore[attr-defined]

    async def test_defensive_guard_raises_when_stamp_leaves_anchorless(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a future stamp_block_ids regression leaves a block anchorless,
        the trailing first_anchorless_block guard blocks the PATCH.
        """
        monkeypatch.setattr(_mod, "stamp_block_ids", lambda html: html)
        with pytest.raises(RuntimeError, match="anchorless block"):
            await _call_update_doc(
                mock_ctx, home_page_content_html="<p>Note</p>", dry_run=True
            )
        mock_client.patch.assert_not_called()
