"""Tests for the 7 read-only MCP tools.

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
list_documents = _read_mod.list_documents.fn
list_projects = _read_mod.list_projects.fn
list_work_items = _read_mod.list_work_items.fn

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------


class TestListDocuments:
    """Tests for the ``list_documents`` tool."""

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

    async def test_binary_search_skips_pages(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Binary search discovers document boundaries without scanning every page.

        Layout (5 pages total, sorted by module):
          Page 1: DocA x 100   (page 1 always fetched)
          Page 2: DocA x 100
          Page 3: DocA x 50 + DocB x 50  (transition)
          Page 4: DocB x 100
          Page 5: DocB x 30   (partial -> end)

        Binary search from page 1 (last_module=DocA):
          lo=2, hi=5 → mid=3 → DocA+DocB (has_new) → transition_page=3, hi=2
          lo=2, hi=2 → mid=2 → DocA only → lo=3
          lo>hi → done, transition_page=3
        Then from page 3 (last_module=DocB):
          lo=4, hi=5 → mid=4 → DocB only → lo=5
          lo=5, hi=5 → mid=5 → DocB only → lo=6
          lo>hi → done, no transition
        Total fetches: page 1 + page 3 + page 2 + page 4 + page 5 = 5
        (but in a larger dataset, most pages would be skipped)
        """

        def _make_item(doc: str) -> dict:
            return {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": f"p/S/{doc}"}}
                }
            }

        page_data = {
            1: [_make_item("DocA")] * 100,
            2: [_make_item("DocA")] * 100,
            3: [_make_item("DocA")] * 50 + [_make_item("DocB")] * 50,
            4: [_make_item("DocB")] * 100,
            5: [_make_item("DocB")] * 30,
        }

        async def _mock_get(path, *, params=None):
            page_num = params["page[number]"]
            return {
                "data": page_data.get(page_num, []),
                "meta": {"totalCount": 430},  # 5 pages
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

    async def test_binary_search_many_documents_on_few_pages(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """When multiple small documents fit on one page, all are discovered."""

        def _make_item(doc: str) -> dict:
            return {
                "relationships": {
                    "module": {"data": {"type": "documents", "id": f"p/S/{doc}"}}
                }
            }

        # Single page with 4 different documents (sorted).
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
        # Only 1 API call needed (single page).
        assert mock_client.get.call_count == 1

    async def test_api_params_include_query_and_sort(
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
        assert params["sort"] == "module"

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
        )

        assert isinstance(result, DocumentDetail)
        assert result.id == "SRS"
        assert result.title == "Software Requirement Spec"
        assert "SRS" in result.content
        assert "<p>" not in result.content
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
        )

        assert result.content == ""

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
        )

        assert result.content == ""


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
        assert heading.id == "proj1/_default/SRS/heading_MCPT-001"
        assert heading.type == "heading"
        assert heading.level == 1
        assert heading.title == "Introduction"
        assert heading.next_part_id == "proj1/_default/SRS/workitem_MCPT-002"
        assert heading.previous_part_id == ""

        wi_part = result.items[1]
        assert wi_part.id == "proj1/_default/SRS/workitem_MCPT-002"
        assert wi_part.type == "workitem"
        assert wi_part.level == 0
        assert wi_part.title == "Login Feature"
        assert "login" in wi_part.description.lower()
        assert wi_part.previous_part_id == "proj1/_default/SRS/heading_MCPT-001"
        assert wi_part.next_part_id == "proj1/_default/SRS/polarion_1"

        normal_part = result.items[2]
        assert normal_part.type == "normal"
        assert normal_part.level == 0
        assert normal_part.title == ""
        assert normal_part.previous_part_id == "proj1/_default/SRS/workitem_MCPT-002"
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
                    "id": "proj1/_default/Doc/heading_MCPT-003",
                    "attributes": {
                        "id": "heading_MCPT-003",
                        "content": "<h2>Plain string content.</h2>",
                        "type": "heading",
                    },
                    "relationships": {
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": "proj1/MCPT-003",
                            }
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-003",
                    "attributes": {
                        "type": "heading",
                        "title": "Plain string content.",
                        "status": "open",
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
        assert result.items[0].type == "heading"
        assert result.items[0].level == 2
        assert "Plain string content" in result.items[0].content
        assert result.items[0].title == "Plain string content."

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
            query=None,
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
# get_linked_work_items
# ---------------------------------------------------------------------------


class TestGetLinkedWorkItems:
    """Tests for the ``get_linked_work_items`` tool."""

    async def test_merges_forward_and_back_links(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Forward call: MCPT-001 -> MCPT-010 (parent) via linkedworkitems
        # Back call: MCPT-020, MCPT-030 -> MCPT-001 via Lucene query
        mock_client.get.side_effect = [
            {
                "data": [
                    {
                        "id": "proj1/MCPT-001/parent/proj1/MCPT-010",
                        "attributes": {
                            "role": "parent",
                            "suspect": False,
                        },
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
                    },
                ],
            },
            {
                "data": [
                    {
                        "id": "proj1/MCPT-020",
                        "attributes": {
                            "title": "Related Item",
                            "type": "requirement",
                            "status": "draft",
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
        assert result.total_count == 3
        assert len(result.items) == 3

        fwd = [i for i in result.items if i.direction == "forward"]
        back = [i for i in result.items if i.direction == "back"]
        assert len(fwd) == 1
        assert len(back) == 2

        # Forward: target from relationships.workItem, role from attributes
        assert fwd[0].id == "MCPT-010"
        assert fwd[0].role == "parent"
        assert fwd[0].title == "Parent Item"
        assert fwd[0].suspect is False

        # Back: parsed from workitems query, role = "backlink"
        back_ids = {i.id for i in back}
        assert back_ids == {"MCPT-020", "MCPT-030"}
        for b in back:
            assert b.role == "backlink"
            assert b.suspect is False

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
        assert result.total_count == 0
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

    async def test_api_calls_forward_and_backlink_query(
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
        # Forward call includes fields and include params
        fwd_params = calls[0][1]["params"]
        assert fwd_params["fields[linkedworkitems]"] == "@all"
        assert fwd_params["include"] == "workItem"
        # Back links use camelCase Lucene query
        assert calls[1][0][0] == "projects/proj1/workitems"
        back_params = calls[1][1]["params"]
        assert back_params["query"] == "linkedWorkItems:MCPT-001"

    async def test_parses_polarion_link_id_format(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Forward: Polarion ID format, Back: workitems query."""
        mock_client.get.side_effect = [
            {
                "data": [
                    {
                        # Forward link: MCPT-001 --[parent]--> MCPT-010
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
                            "title": "Parent Target",
                            "type": "heading",
                            "status": "open",
                        },
                    },
                ],
            },
            {
                "data": [
                    {
                        "id": "proj1/MCPT-099",
                        "attributes": {
                            "title": "Back Item",
                            "type": "requirement",
                            "status": "open",
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

        fwd = result.items[0]  # forward
        back = result.items[1]  # back

        # Forward: target from relationships.workItem
        assert fwd.direction == "forward"
        assert fwd.id == "MCPT-010"
        assert fwd.role == "parent"
        assert fwd.title == "Parent Target"

        # Back: parsed from workitems query
        assert back.direction == "back"
        assert back.id == "MCPT-099"
        assert back.role == "backlink"
        assert back.title == "Back Item"
