"""Tests for the 8 read-only MCP tools.

Each tool is tested by calling the async function directly with a mock
``PolarionClient`` injected via a mock ``Context``.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Annotated, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    LinkedWorkItemSummary,
    PaginatedResult,
    ProjectSummary,
    WorkItemDetail,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools import read as _read_mod

# In FastMCP 3.0, @mcp.tool returns the original function unchanged
# (not a FunctionTool wrapper), so we reference them directly.
get_document = _read_mod.get_document
get_document_parts = _read_mod.get_document_parts
get_linked_work_items = _read_mod.get_linked_work_items
get_work_item = _read_mod.get_work_item
list_documents = _read_mod.list_documents
list_projects = _read_mod.list_projects
list_work_items = _read_mod.list_work_items
read_document = _read_mod.read_document


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    client.get = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Return a mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx


class TestListProjects:
    """Tests for the ``list_projects`` tool."""

    async def test_returns_projects(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "Project One", "active": True},
                },
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Project Two", "active": False},
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        p1 = ProjectSummary(id="proj1", name="Project One", active=True)
        assert result.items[0] == p1
        p2 = ProjectSummary(id="proj2", name="Project Two", active=False)
        assert result.items[1] == p2

    async def test_active_defaults_true_when_missing(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "No Active Field"},
                },
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Non-bool Active", "active": "yes"},
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].active is True
        assert result.items[1].active is True

    async def test_requests_active_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "active" in kwargs["params"]["fields[projects]"].split(",")

    async def test_empty_projects(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0
        assert result.has_more is False

    async def test_pagination_params_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Project 2"},
                },
                {
                    "type": "projects",
                    "id": "proj3",
                    "attributes": {"name": "Project 3"},
                },
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=2,
            page_number=2,
        )

        assert result.total_count == 5
        assert len(result.items) == 2
        assert result.page == 2
        assert result.has_more is True
        assert result.items[0].id == "proj2"
        assert result.items[1].id == "proj3"

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["page[size]"] == 2
        assert kwargs["params"]["page[number]"] == 2

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_projects(
                mock_ctx,
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_generic_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Server error",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Failed to list"):
            await list_projects(
                mock_ctx,
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query="name:ILCU*",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "name:ILCU*"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_query_returns_matching_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "ILCU Main"},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_projects(
            mock_ctx,
            query="name:ILCU*",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1
        assert result.items[0].id == "proj1"

    async def test_total_count_floor_when_api_returns_zero(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """totalCount=0 with items present uses item count."""
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "Project One"},
                },
            ],
            "meta": {"totalCount": 0},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.total_count >= 1


class TestListDocuments:
    """Tests for the ``list_documents`` tool."""

    @pytest.fixture(autouse=True)
    def _clear_doc_cache(self) -> Iterator[None]:
        """Reset the module-level TTL cache around every test.

        Several tests reuse ``project_id='proj1'``, so without this the
        first test would poison subsequent tests via cache hits.
        """
        _read_mod._documents_cache.clear()
        yield
        _read_mod._documents_cache.clear()

    async def test_extracts_documents_from_modules(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        items = [
            {
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": "proj1/_default/Doc1"}
                    }
                }
            },
            {
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": "proj1/_default/Doc2"}
                    }
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Design/SRS"}}
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Design/SDD"}}
                }
            },
            {
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": "proj1/Testing/TestPlan"}
                    }
                }
            },
        ]
        # Single page — totalCount ≤ page_size, so no binary search.
        mock_client.get.return_value = {
            "data": items,
            "meta": {"totalCount": 5},
        }

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 5
        space_doc_pairs = [(d.space_id, d.document_name) for d in result.items]
        assert ("_default", "Doc1") in space_doc_pairs
        assert ("_default", "Doc2") in space_doc_pairs
        assert ("Design", "SDD") in space_doc_pairs
        assert ("Design", "SRS") in space_doc_pairs
        assert ("Testing", "TestPlan") in space_doc_pairs

    async def test_deduplicates_documents(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        items = [
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocA"}}
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocA"}}
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocA"}}
                }
            },
        ]
        mock_client.get.return_value = {
            "data": items,
            "meta": {"totalCount": 3},
        }

        result = await list_documents(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 1
        assert result.items[0].space_id == "Space1"
        assert result.items[0].document_name == "DocA"

    async def test_pagination_slicing(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        items = [
            {
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": f"proj1/Space{i}/Doc"}
                    }
                }
            }
            for i in range(5)
        ]
        mock_client.get.return_value = {
            "data": items,
            "meta": {"totalCount": 5},
        }

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
        items = [
            {"relationships": {"module": {"data": None}}},
            {"relationships": {}},
            {"relationships": {"module": {}}},
        ]
        mock_client.get.return_value = {
            "data": items,
            "meta": {"totalCount": 3},
        }

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
        """Linear scan visits every heading page exactly once."""

        def _make_item(doc: str) -> dict:
            return {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": f"p/S/{doc}"}}
                }
            }

        page_data = {
            1: [_make_item("DocA")] * 100,
            2: [_make_item("DocA")] * 50 + [_make_item("DocB")] * 50,
            3: [_make_item("DocB")] * 30,
        }

        async def _mock_get(_path, *, params=None):
            page_num = params["page[number]"]
            return {
                "data": page_data.get(page_num, []),
                "meta": {"totalCount": 230},
            }

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

        def _make_item(doc: str) -> dict:
            return {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": f"p/S/{doc}"}}
                }
            }

        # Page 2 is partial (50 < 100), and totalCount is omitted →
        # compute_has_more's heuristic must stop the scan.
        page_data = {
            1: [_make_item("DocA")] * 100,
            2: [_make_item("DocB")] * 50,
        }

        async def _mock_get(_path, *, params=None):
            page_num = params["page[number]"]
            return {"data": page_data.get(page_num, [])}

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

        def _make_item(doc: str) -> dict:
            return {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": f"p/S/{doc}"}}
                }
            }

        items = (
            [_make_item("Alpha")] * 25
            + [_make_item("Bravo")] * 25
            + [_make_item("Charlie")] * 25
            + [_make_item("Delta")] * 25
        )
        mock_client.get.return_value = {
            "data": items,
            "meta": {"totalCount": 100},
        }

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
        mock_client.get.return_value = {
            "data": [
                {
                    "relationships": {
                        "module": {
                            "data": {"type": "documents", "id": "proj1/_default/Doc1"}
                        }
                    }
                },
            ],
            "meta": {"totalCount": 1},
        }

        first = await list_documents(
            mock_ctx, project_id="proj1", page_size=100, page_number=1
        )
        calls_after_first = mock_client.get.call_count

        second = await list_documents(
            mock_ctx, project_id="proj1", page_size=100, page_number=1
        )

        assert mock_client.get.call_count == calls_after_first
        assert [(d.space_id, d.document_name) for d in first.items] == [
            (d.space_id, d.document_name) for d in second.items
        ]

    async def test_cache_isolated_per_project(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Different project IDs do not share cache entries."""

        async def _mock_get(_path, *, params=None):
            # Echo project from path? Simpler: return distinct doc per call.
            return {
                "data": [
                    {
                        "relationships": {
                            "module": {
                                "data": {
                                    "type": "documents",
                                    "id": f"x/S/Doc{mock_client.get.call_count}",
                                }
                            }
                        }
                    },
                ],
                "meta": {"totalCount": 1},
            }

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
        mock_client.get.return_value = {
            "data": [
                {
                    "relationships": {
                        "module": {
                            "data": {"type": "documents", "id": "proj1/_default/Doc1"}
                        }
                    }
                },
            ],
            "meta": {"totalCount": 1},
        }

        fake_now = [1000.0]

        def _fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr(_read_mod.time, "monotonic", _fake_monotonic)

        await list_documents(mock_ctx, project_id="proj1", page_size=100, page_number=1)
        first_calls = mock_client.get.call_count

        # Advance time past the TTL.
        fake_now[0] += _read_mod._CACHE_TTL_SECONDS + 1.0

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
        assert params["query"] == "type:heading"
        # sort=module was dropped — server-side sort cost > client-side dedup
        # (benchmarked: ~4x slower on cold server cache, equal warm).
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
                    "homePageContent": {
                        "type": "text/html",
                        "value": ("<p>This is the <strong>SRS</strong> document.</p>"),
                    },
                },
            },
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
        # Raw HTML round-trip: <p> and <strong> tags survive verbatim.
        assert result.content_html == (
            "<p>This is the <strong>SRS</strong> document.</p>"
        )
        assert result.custom_fields == {}

    async def test_include_homepage_content_html_false_omits_content(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_homepage_content_html=False → body skipped even when API echoes it.

        With ``fields[documents]=@all`` the wire always carries
        ``homePageContent``; the tool layer is responsible for hiding it
        from the LLM unless asked.
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
        """Polarion-specific spans / data-* attrs must survive on read.

        This is the core round-trip guarantee — the same string must
        be passable back into update_document(home_page_content_html=...)
        unchanged. If sanitization / markdownify ever creeps back in,
        these markers would be the first thing to disappear.
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
                    # Standard attrs — present but excluded from custom_fields.
                    "title": "SRS",
                    "type": "req_specification",
                    "status": "approved",
                    # Inline custom attributes — top-level keys.
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

        await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="B",
            include_homepage_content_html=True,
        )
        _, kwargs_b = mock_client.get.call_args
        assert kwargs_b["params"]["fields[documents]"] == "@all"


class TestGetDocumentParts:
    """Tests for the ``get_document_parts`` tool."""

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

        result = await get_document_parts(
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

        wi_part = result.items[1]
        assert wi_part.id == "workitem_MCPT-002"
        assert wi_part.type == "workitem"
        assert wi_part.level == 0
        assert wi_part.title == "Login Feature"
        assert wi_part.content == ""
        assert "login" in wi_part.description.lower()
        assert wi_part.work_item_id == "MCPT-002"
        assert wi_part.work_item_type == "requirement"
        assert wi_part.work_item_status == "draft"
        assert wi_part.external is True
        assert wi_part.next_part_id == "polarion_1"

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

        await get_document_parts(
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
        """Embedded WIs are fetched with WI_PART_FIELDS, not ``@all``.

        DocumentPart only consumes title/type/status/description from the
        linked WI; sending ``@all`` would ship every inline custom field
        (KBs per page) for no downstream use. This guard prevents a
        regression to the old over-fetching behaviour.
        """
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await get_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["fields[workitems]"] == "title,type,status,description"

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_document_parts(
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

        result = await get_document_parts(
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

        result = await get_document_parts(
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

        result = await get_document_parts(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="Doc",
            page_size=100,
            page_number=1,
        )

        assert len(result.items) == 2
        assert result.total_count >= 2


def _make_part(
    *,
    type_: str,
    part_id: str = "polarion_x",
    title: str = "",
    content: str = "",
    description: str = "",
    level: int = 0,
    work_item_id: str = "",
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
        next_part_id="",
    )


def _stub_parts(
    monkeypatch: pytest.MonkeyPatch, parts: list[DocumentPart]
) -> AsyncMock:
    """Replace ``get_document_parts`` with an AsyncMock returning *parts*.

    Returns the mock so individual tests can assert call arguments. Page
    metadata is derived from the part list so pagination defaults stay
    sensible without each test having to spell them out.
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
    monkeypatch.setattr(_read_mod, "get_document_parts", stub)
    return stub


class TestReadDocument:
    """Tests for the ``read_document`` tool."""

    # -- end-to-end wiring (exercises get_document_parts via client.get) --

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
                        "content": "<p>Some prose between the WI and a heading.</p>",
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

        # Heading rendered at level 1, then workitem with bold lead-in
        # plus its description, then prose. Empty paragraph is skipped.
        assert result.content == (
            "# Introduction\n"
            "\n"
            "**Login Feature** (`MCPT-002`)\n"
            "\n"
            "The system shall support login.\n"
            "\n"
            "Some prose between the WI and a heading."
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
        """Delegation to ``get_document_parts`` surfaces its ValueError verbatim."""
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

    # -- render-rule tests (isolated via monkeypatched get_document_parts) --

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
        """Defensive guard for sparse-data WIs (rare but observed)."""
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

    async def test_toc_parts_skipped(
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

        assert result.content == "# Top\n\nAfter TOC."

    async def test_wikiblock_content_emitted(
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

        assert result.content == '```\n#documentPanel(true "approved")\n```'

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
        monkeypatch.setattr(_read_mod, "get_document_parts", stub)

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
    """Verify Field constraints on ``read_document`` parameters.

    Direct invocation bypasses FastMCP's JSON Schema gate; rebuild a
    ``TypeAdapter`` from each parameter's annotation + ``FieldInfo`` to
    prove the constraint is wired correctly.
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


class TestListWorkItems:
    """Tests for the ``list_work_items`` tool."""

    async def test_returns_work_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "Login Feature",
                        "type": "requirement",
                        "status": "draft",
                        "priority": "90.0",
                        "updated": "2026-04-29T10:23:00Z",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Design/Software Requirement Specification",
                            }
                        },
                        "assignee": {
                            "data": [
                                {"type": "users", "id": "proj1/alice"},
                                {"type": "users", "id": "proj1/bob"},
                            ]
                        },
                    },
                },
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "title": "Logout Feature",
                        "type": "requirement",
                        "status": "approved",
                    },
                    "relationships": {
                        "module": {"data": None},
                        "assignee": {"data": []},
                    },
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2

        first = result.items[0]
        assert first.id == "MCPT-001"
        assert first.title == "Login Feature"
        assert first.priority == "90.0"
        assert first.updated == "2026-04-29T10:23:00Z"
        assert first.space_id == "Design"
        assert first.document_name == "Software Requirement Specification"
        assert first.assignee_ids == ["alice", "bob"]

        second = result.items[1]
        assert second.id == "MCPT-002"
        assert second.priority == ""
        assert second.updated == ""
        assert second.space_id == ""
        assert second.document_name == ""
        assert second.assignee_ids == []

    async def test_sparse_fieldset_requested(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "fields[workitems]" in kwargs["params"]

    async def test_project_not_found(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await list_work_items(
                mock_ctx,
                project_id="missing",
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_strips_project_prefix_from_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "myproject/WI-100",
                    "attributes": {
                        "title": "Test",
                        "type": "task",
                        "status": "open",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="myproject",
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].id == "WI-100"

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:testCase",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "type:testCase"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_query_returns_matching_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "Login Feature",
                        "type": "requirement",
                        "status": "approved",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:requirement AND status:approved",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1
        assert result.items[0].id == "MCPT-001"

    async def test_total_count_floor_when_api_returns_zero(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """When Polarion omits totalCount (returns 0), use item count as minimum."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "A",
                        "type": "requirement",
                        "status": "open",
                    },
                },
                {
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "title": "B",
                        "type": "requirement",
                        "status": "open",
                    },
                },
            ],
            "meta": {"totalCount": 0},  # Polarion quirk: 0 even when items exist
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:requirement",
            page_size=100,
            page_number=1,
        )

        # total_count should be at least 2 (the number of returned items)
        assert result.total_count >= 2


class TestGetWorkItem:
    """Tests for the ``get_work_item`` tool."""

    async def test_returns_work_item_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "workitems",
                "id": "proj1/MCPT-001",
                "attributes": {
                    "title": "Login Feature",
                    "type": "requirement",
                    "status": "draft",
                    "priority": "75.0",
                    "updated": "2026-04-29T10:23:00Z",
                    "created": "2026-04-01T09:00:00Z",
                    "outlineNumber": "1.2.3",
                    "hyperlinks": [
                        {
                            "role": "ref_ext",
                            "title": "Spec",
                            "uri": "https://example.com/spec",
                        },
                        {"role": "impl", "title": "", "uri": ""},
                    ],
                    "description": {
                        "type": "text/html",
                        "value": (
                            "<p>User must be able to <strong>log in</strong>.</p>"
                        ),
                    },
                },
                "relationships": {
                    "module": {
                        "data": {
                            "type": "documents",
                            "id": "proj1/Design/SRS",
                        }
                    },
                    "assignee": {"data": [{"type": "users", "id": "proj1/alice"}]},
                    "author": {"data": {"type": "users", "id": "proj1/bob"}},
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            include_description_html=True,
        )

        assert isinstance(result, WorkItemDetail)
        assert result.id == "MCPT-001"
        assert result.title == "Login Feature"
        assert result.type == "requirement"
        assert result.status == "draft"
        assert result.priority == "75.0"
        assert result.updated == "2026-04-29T10:23:00Z"
        assert result.created == "2026-04-01T09:00:00Z"
        assert result.outline_number == "1.2.3"
        assert result.space_id == "Design"
        assert result.document_name == "SRS"
        assert result.assignee_ids == ["alice"]
        assert result.author_id == "bob"
        # Entry without uri is skipped.
        assert len(result.hyperlinks) == 1
        assert result.hyperlinks[0].role == "ref_ext"
        assert result.hyperlinks[0].uri == "https://example.com/spec"
        # Raw HTML passthrough — <p>/<strong> survive verbatim, no markdownify.
        assert result.description_html == (
            "<p>User must be able to <strong>log in</strong>.</p>"
        )
        assert result.project_id == "proj1"
        assert result.custom_fields == {}

    async def test_include_description_html_false_blanks_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_description_html=False → description_html blanked.

        ``@all`` is the only sparse-fieldset that surfaces custom fields,
        so the body still travels over the wire; the tool layer is
        responsible for stripping it from the response to save LLM
        context tokens. The default at the FastMCP layer is False; here
        we pass it explicitly because direct-call tests bypass the
        FastMCP Field default unwrap.
        """
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-007",
                "attributes": {
                    "title": "WI",
                    "type": "task",
                    "status": "draft",
                    "description": {
                        "type": "text/html",
                        "value": "<p>should be hidden</p>",
                    },
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-007",
            include_description_html=False,
        )

        assert result.description_html == ""
        # Other metadata still populated.
        assert result.title == "WI"

    async def test_polarion_specific_markup_round_trips(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Polarion-specific spans / data-* attrs must survive on read.

        Core round-trip guarantee for update_work_item(description_html=).
        """
        raw = (
            '<p>Refs <span class="polarion-rte-link" '
            'data-item-id="MCPT-9" data-scope="proj1">MCPT-9</span></p>'
        )
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-008",
                "attributes": {
                    "title": "RT",
                    "type": "task",
                    "status": "draft",
                    "description": {"type": "text/html", "value": raw},
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-008",
            include_description_html=True,
        )

        assert result.description_html == raw

    async def test_defect_severity_and_open_resolution(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-500",
                "attributes": {
                    "title": "Login crashes",
                    "type": "defect",
                    "status": "open",
                    "severity": "blocker",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-500",
        )

        assert result.severity == "blocker"
        assert result.resolution == ""

    async def test_no_description(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-002",
                "attributes": {
                    "title": "Minimal",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-002",
            include_description_html=True,
        )

        assert result.description_html == ""
        # Default values for new detail-only fields.
        assert result.author_id == ""
        assert result.created == ""
        assert result.severity == ""
        assert result.resolution == ""
        assert result.outline_number == ""
        assert result.hyperlinks == []

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_work_item(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
            )

    async def test_api_path_includes_work_item_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-010",
                "attributes": {
                    "title": "Test",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-010",
        )

        call_path = mock_client.get.call_args[0][0]
        expected = "/projects/proj1/workitems/MCPT-010"
        assert call_path == expected

    async def test_custom_fields_populated_from_response(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Inline non-standard attributes flow through as ``custom_fields``."""
        rich_value = {"type": "text/html", "value": "<p>note</p>"}
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-999",
                "attributes": {
                    # Standard attrs — present but excluded from custom_fields.
                    "title": "WI with customs",
                    "type": "softwarerequirement",
                    "status": "approved",
                    "priority": "50.0",
                    # Inline custom attributes — top-level keys, not nested.
                    "riskLevel": "high",
                    "category": "user",
                    "effortHours": 12.0,
                    "reviewerNote": rich_value,
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-999",
        )

        # Raw passthrough: rich-text values stay as the original
        # {type, value} dict — they are NOT converted to Markdown.
        assert result.custom_fields == {
            "riskLevel": "high",
            "category": "user",
            "effortHours": 12.0,
            "reviewerNote": rich_value,
        }

    async def test_custom_fields_empty_when_only_standard_attrs(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """All-standard attributes → empty custom_fields dict."""
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-100",
                "attributes": {
                    "title": "No customs",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-100",
        )

        assert result.custom_fields == {}

    async def test_sparse_fieldset_uses_all_token(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``fields[workitems]=@all`` is the only token that surfaces customs."""
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-1",
                "attributes": {"title": "x", "type": "task", "status": "open"},
            },
        }

        await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["fields[workitems]"] == "@all"


class TestGetLinkedWorkItems:
    """Tests for the ``get_linked_work_items`` tool.

    Each call returns a single page in a single direction (``forward`` or
    ``back``). Pagination matches the convention used by other list tools.
    """

    async def test_forward_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001/parent/proj1/MCPT-010",
                    "attributes": {"role": "parent", "suspect": False},
                    "relationships": {
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": "proj1/MCPT-010",
                            }
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-010",
                    "attributes": {
                        "title": "Parent Item",
                        "type": "heading",
                        "status": "open",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Design/SRS",
                            }
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 1
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        assert len(result.items) == 1

        fwd = result.items[0]
        assert isinstance(fwd, LinkedWorkItemSummary)
        assert fwd.direction == "forward"
        assert fwd.id == "MCPT-010"
        assert fwd.role == "parent"
        assert fwd.title == "Parent Item"
        assert fwd.suspect is False
        assert fwd.type == "heading"
        assert fwd.status == "open"
        assert fwd.space_id == "Design"
        assert fwd.document_name == "SRS"

    async def test_forward_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Simulate page 1 of a 5-item set with page_size=2 — server returns
        # the first 2 items, meta.totalCount reports the full collection
        # size, and has_more should be True (2 * 1 < 5).
        mock_client.get.return_value = {
            "data": [
                {
                    "id": f"proj1/MCPT-001/parent/proj1/MCPT-{i:03d}",
                    "attributes": {"role": "parent", "suspect": False},
                    "relationships": {
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": f"proj1/MCPT-{i:03d}",
                            }
                        },
                    },
                }
                for i in range(2)
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": f"proj1/MCPT-{i:03d}",
                    "attributes": {
                        "title": f"WI {i}",
                        "type": "requirement",
                        "status": "open",
                    },
                }
                for i in range(2)
            ],
            "meta": {"totalCount": 5},
        }

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=2,
            page_number=1,
        )

        assert result.total_count == 5
        assert result.page == 1
        assert result.page_size == 2
        assert result.has_more is True
        assert len(result.items) == 2

    async def test_forward_passes_pagination_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=25,
            page_number=3,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems/MCPT-001/linkedworkitems"
        params = calls[0][1]["params"]
        assert params["fields[linkedworkitems]"] == "@all"
        assert params["include"] == "workItem"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_back_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-020",
                    "attributes": {
                        "title": "Related Item",
                        "type": "requirement",
                        "status": "draft",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Requirements/SysRS",
                            }
                        },
                    },
                },
                {
                    "id": "proj1/MCPT-030",
                    "attributes": {
                        "title": "Verifier Item",
                        "type": "testCase",
                        "status": "approved",
                    },
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="back",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 2
        assert result.has_more is False
        assert len(result.items) == 2

        back_by_id = {item.id: item for item in result.items}
        assert set(back_by_id) == {"MCPT-020", "MCPT-030"}
        for item in result.items:
            assert item.direction == "back"
            assert item.role is None
            assert item.suspect is False
        assert back_by_id["MCPT-020"].type == "requirement"
        assert back_by_id["MCPT-020"].space_id == "Requirements"
        assert back_by_id["MCPT-020"].document_name == "SysRS"
        assert back_by_id["MCPT-030"].space_id == ""

    async def test_back_passes_pagination_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="back",
            page_size=10,
            page_number=2,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems"
        params = calls[0][1]["params"]
        assert params["query"] == "linkedWorkItems:MCPT-001"
        assert params["page[size]"] == 10
        assert params["page[number]"] == 2

    async def test_no_links_returns_empty_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0
        assert result.has_more is False

    async def test_forward_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_linked_work_items(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
                direction="forward",
                page_size=100,
                page_number=1,
            )

    async def test_back_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_linked_work_items(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
                direction="back",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Forbidden",
            status_code=403,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await get_linked_work_items(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
                direction="forward",
                page_size=100,
                page_number=1,
            )

    async def test_back_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Boom",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Backlink query failed"):
            await get_linked_work_items(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
                direction="back",
                page_size=100,
                page_number=1,
            )

    async def test_direction_default_is_forward_in_registered_schema(
        self,
    ) -> None:
        """Guard the JSON-Schema default for ``direction``.

        Direct invocation cannot exercise FastMCP's default-injection
        (passing the ``Field(...)`` sentinel through), so the registered
        tool schema is the authoritative place to verify the default
        the LLM will see. Regressions here would silently break the
        zero-arg call path.
        """
        tools = await mcp.list_tools()
        tool = next(t for t in tools if t.name == "get_linked_work_items")
        direction_schema = tool.parameters["properties"]["direction"]
        assert direction_schema["default"] == "forward"
        assert direction_schema["enum"] == ["forward", "back"]
