"""Tests for the 8 read-only MCP tools.

Each tool is tested by calling the async function directly with a mock
``PolarionClient`` injected via a mock ``Context``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    DocumentDetail,
    DocumentPart,
    LinkedWorkItemsList,
    PaginatedResult,
    ProjectSummary,
    WorkItemDetail,
)
from mcp_server_polarion.tools import read as _read_mod

# Extract the underlying async functions from FunctionTool wrappers
# so they can be called directly in tests.
get_document = _read_mod.get_document.fn
get_document_parts = _read_mod.get_document_parts.fn
get_linked_work_items = _read_mod.get_linked_work_items.fn
get_work_item = _read_mod.get_work_item.fn
list_projects = _read_mod.list_projects.fn
list_spaces = _read_mod.list_spaces.fn
list_work_items = _read_mod.list_work_items.fn
search_work_items = _read_mod.search_work_items.fn

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    client.get = AsyncMock()
    client.get_all_pages = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Return a mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


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
                    "attributes": {"name": "Project One"},
                },
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Project Two"},
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_projects(
            mock_ctx,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2
        assert result.page == 1
        assert result.page_size == 100
        p1 = ProjectSummary(id="proj1", name="Project One")
        assert result.items[0] == p1
        p2 = ProjectSummary(id="proj2", name="Project Two")
        assert result.items[1] == p2

    async def test_empty_projects(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        result = await list_projects(
            mock_ctx,
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0

    async def test_pagination_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            page_size=10,
            page_number=3,
        )

        mock_client.get.assert_called_once_with(
            "/projects",
            params={"page[size]": 10, "page[number]": 3},
        )

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
                page_size=100,
                page_number=1,
            )

    async def test_missing_meta_returns_zero_total(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await list_projects(
            mock_ctx,
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 0


# ---------------------------------------------------------------------------
# list_spaces
# ---------------------------------------------------------------------------


class TestListSpaces:
    """Tests for the ``list_spaces`` tool."""

    async def test_extracts_spaces_from_modules(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_all_pages.return_value = [
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

        result = await list_spaces(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 3
        ids = [s.id for s in result.items]
        assert sorted(ids) == ["Design", "Testing", "_default"]

    async def test_deduplicates_spaces(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_all_pages.return_value = [
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocA"}}
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocB"}}
                }
            },
            {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": "proj1/Space1/DocC"}}
                }
            },
        ]

        result = await list_spaces(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 1
        assert result.items[0].id == "Space1"

    async def test_pagination_slicing(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_all_pages.return_value = [
            {
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": f"proj1/Space{i}/Doc"}
                    }
                }
            }
            for i in range(5)
        ]

        result = await list_spaces(
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
        mock_client.get_all_pages.return_value = [
            {"relationships": {"module": {"data": None}}},
            {"relationships": {}},
            {"relationships": {"module": {}}},
        ]

        result = await list_spaces(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 0
        assert result.items == []

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_all_pages.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await list_spaces(
                mock_ctx,
                project_id="missing",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_all_pages.side_effect = PolarionAuthError(
            "Forbidden", status_code=403
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_spaces(
                mock_ctx,
                project_id="proj1",
                page_size=100,
                page_number=1,
            )


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


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
                    "description": {
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
        )

        assert isinstance(result, DocumentDetail)
        assert result.id == "SRS"
        assert result.title == "Software Requirement Spec"
        assert "SRS" in result.description
        assert "<p>" not in result.description
        assert result.space_id == "_default"
        assert result.project_id == "proj1"

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

    async def test_empty_description(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "id": "EmptyDoc",
                    "title": "Empty",
                    "description": {
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
        )

        assert result.description == ""

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

    async def test_no_description_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "id": "NoDesc",
                    "title": "No Description",
                },
            },
        }

        result = await get_document(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="NoDesc",
        )

        assert result.description == ""


# ---------------------------------------------------------------------------
# get_document_parts
# ---------------------------------------------------------------------------


class TestGetDocumentParts:
    """Tests for the ``get_document_parts`` tool."""

    async def test_returns_document_parts(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_parts",
                    "id": "heading_MCPT-001",
                    "attributes": {
                        "title": "Introduction",
                        "level": 1,
                        "content": {
                            "type": "text/html",
                            "value": "<p>This is the intro.</p>",
                        },
                    },
                },
                {
                    "type": "document_parts",
                    "id": "workitem_MCPT-002",
                    "attributes": {
                        "title": "Login Feature",
                        "level": 0,
                        "content": {
                            "type": "text/html",
                            "value": "<p>Login requirement.</p>",
                        },
                    },
                },
            ],
            "meta": {"totalCount": 2},
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
        assert len(result.items) == 2
        assert result.total_count == 2

        heading = result.items[0]
        assert isinstance(heading, DocumentPart)
        assert heading.id == "heading_MCPT-001"
        assert heading.title == "Introduction"
        assert heading.type == "heading"
        assert heading.level == 1
        assert "<p>" not in heading.content

        wi_part = result.items[1]
        assert wi_part.id == "workitem_MCPT-002"
        assert wi_part.type == "workitem"
        assert wi_part.level == 0

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
        assert kwargs["params"]["page[size]"] == 10
        assert kwargs["params"]["page[number]"] == 2

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
        """Plain string content (not dict) is handled."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "heading_MCPT-003",
                    "attributes": {
                        "title": "Heading",
                        "level": 2,
                        "content": "<p>Plain string content.</p>",
                    },
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
        assert "Plain string content" in result.items[0].content


# ---------------------------------------------------------------------------
# list_work_items
# ---------------------------------------------------------------------------


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
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2
        assert result.items[0].id == "MCPT-001"
        assert result.items[0].title == "Login Feature"
        assert result.items[1].id == "MCPT-002"

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
            page_size=100,
            page_number=1,
        )

        assert result.items[0].id == "WI-100"


# ---------------------------------------------------------------------------
# get_work_item
# ---------------------------------------------------------------------------


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
                    "description": {
                        "type": "text/html",
                        "value": (
                            "<p>User must be able to <strong>log in</strong>.</p>"
                        ),
                    },
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        assert isinstance(result, WorkItemDetail)
        assert result.id == "MCPT-001"
        assert result.title == "Login Feature"
        assert result.type == "requirement"
        assert result.status == "draft"
        assert "log in" in result.description
        assert "<p>" not in result.description
        assert result.project_id == "proj1"

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
        )

        assert result.description == ""

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


# ---------------------------------------------------------------------------
# search_work_items
# ---------------------------------------------------------------------------


class TestSearchWorkItems:
    """Tests for the ``search_work_items`` tool."""

    async def test_returns_matching_items(
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

        result = await search_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:requirement AND status:approved",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1
        assert result.items[0].id == "MCPT-001"

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await search_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:testCase",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "type:testCase"

    async def test_empty_results(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        result = await search_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:nonexistent",
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0

    async def test_project_not_found(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await search_work_items(
                mock_ctx,
                project_id="missing",
                query="type:requirement",
                page_size=100,
                page_number=1,
            )

    async def test_generic_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Bad query syntax",
            status_code=400,
        )

        with pytest.raises(RuntimeError, match="Failed to search"):
            await search_work_items(
                mock_ctx,
                project_id="proj1",
                query="invalid:::query",
                page_size=100,
                page_number=1,
            )


# ---------------------------------------------------------------------------
# get_linked_work_items
# ---------------------------------------------------------------------------


class TestGetLinkedWorkItems:
    """Tests for the ``get_linked_work_items`` tool."""

    async def test_merges_forward_and_back_links(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # First call = forward, second = back.
        mock_client.get.side_effect = [
            {
                "data": [
                    {
                        "id": "proj1/MCPT-010",
                        "attributes": {
                            "title": "Parent Item",
                            "role": "parent",
                            "suspect": False,
                        },
                    },
                ],
            },
            {
                "data": [
                    {
                        "id": "proj1/MCPT-020",
                        "attributes": {
                            "title": "Child Item",
                            "role": "relates_to",
                            "suspect": True,
                        },
                    },
                    {
                        "id": "proj1/MCPT-030",
                        "attributes": {
                            "title": "Verifier",
                            "role": "verifies",
                            "suspect": False,
                        },
                    },
                ],
            },
        ]

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        assert isinstance(result, LinkedWorkItemsList)
        assert result.forward_count == 1
        assert result.back_count == 2
        assert len(result.items) == 3

        fwd = [i for i in result.items if i.direction == "forward"]
        back = [i for i in result.items if i.direction == "back"]
        assert len(fwd) == 1
        assert len(back) == 2

        assert fwd[0].id == "MCPT-010"
        assert fwd[0].role == "parent"
        assert fwd[0].suspect is False

        suspects = [i for i in result.items if i.suspect]
        assert len(suspects) == 1
        assert suspects[0].id == "MCPT-020"

    async def test_no_links(self, mock_ctx: MagicMock, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = [
            {"data": []},
            {"data": []},
        ]

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        assert result.forward_count == 0
        assert result.back_count == 0
        assert result.items == []

    async def test_not_found_raises_value_error(
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
            )

    async def test_api_calls_both_endpoints(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            {"data": []},
            {"data": []},
        ]

        await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 2
        base = "/projects/proj1/workitems/MCPT-001"
        assert calls[0][0][0] == f"{base}/linkedworkitems"
        assert calls[1][0][0] == f"{base}/backlinkedworkitems"

    async def test_strips_project_prefix_from_linked_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            {
                "data": [
                    {
                        "id": "proj1/MCPT-010",
                        "attributes": {
                            "title": "Linked",
                            "role": "parent",
                            "suspect": False,
                        },
                    },
                ],
            },
            {"data": []},
        ]

        result = await get_linked_work_items(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        assert result.items[0].id == "MCPT-010"
