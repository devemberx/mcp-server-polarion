"""Real MCP tools against stateful fake: write -> read round trips eval
agents lean on (verify after write, look before act). Same wiring as
``runner`` (in-memory fastmcp + respx) minus agent / LLM, so drift between
server parsers and fake wire shapes fail here, not mid-gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

import mcp_server_polarion.core.client as client_mod
import mcp_server_polarion.tools._shared.cache as cache_mod
from evals.harness.fake_polarion import FakePolarion
from evals.harness.fixtures import (
    CHILD_REQ_ID,
    DOC,
    DOC_HEADING_ID,
    FLOATING_TASK_HYPERLINK_URI,
    FLOATING_TASK_ID,
    POLARION_HOST,
    PROJECT,
    SECTION_A_PART_ID,
    SPACE,
    TEST_RUN_ID,
    TESTCASE_ID,
)
from mcp_server_polarion.server import mcp

Tool = Callable[..., Awaitable[Any]]


@pytest.fixture
async def tool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Tool]:
    monkeypatch.setenv("POLARION_URL", POLARION_HOST)
    monkeypatch.setenv("POLARION_TOKEN", "fake-token")
    monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "0")
    monkeypatch.setattr(client_mod, "_WRITE_DELAY_SECONDS", 0.0)
    # Process-wide caches outlive fake worlds (runner clear them per case).
    for value in vars(cache_mod).values():
        if isinstance(value, cache_mod.TTLCache):
            value.clear()
    fake = FakePolarion()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        fake.install(router)
        async with Client(mcp) as client:

            async def call(name: str, **args: Any) -> Any:
                result = await client.call_tool(name, {"project_id": PROJECT, **args})
                return result.structured_content

            yield call


def _doc(**args: Any) -> dict[str, Any]:
    return {"space_id": SPACE, "document_name": DOC, **args}


async def _new_requirement(tool: Tool, title: str) -> str:
    created = await tool(
        "create_work_items", items=[{"title": title, "type": "systemrequirement"}]
    )
    (new_id,) = created["work_item_ids"]
    return str(new_id)


async def test_created_item_moved_under_heading_shows_everywhere(tool: Tool) -> None:
    new_id = await _new_requirement(tool, "Cache size limit")
    await tool(
        "move_work_item_to_document",
        work_item_id=new_id,
        target_space_id=SPACE,
        target_document_name=DOC,
        previous_part_id=SECTION_A_PART_ID,
    )
    parts = await tool("read_document_parts", **_doc())
    ids = [p["id"] for p in parts["items"]]
    assert ids[ids.index(SECTION_A_PART_ID) + 1] == f"workitem_{new_id}"
    links = await tool("list_work_item_links", work_item_id=new_id)
    assert [(x["id"], x["role"]) for x in links["items"]] == [
        (DOC_HEADING_ID, "relates_to")
    ]
    found = await tool("list_work_items", query='title:"Cache size limit"')
    assert [i["id"] for i in found["items"]] == [new_id]


async def test_heading_from_update_document_anchors_later_move(tool: Tool) -> None:
    doc = await tool("get_document", **_doc(include_home_page_content_html=True))
    await tool(
        "update_document",
        **_doc(home_page_content_html=doc["content_html"] + "\n<h2>Performance</h2>"),
    )
    parts = await tool("read_document_parts", **_doc())
    heading = parts["items"][-1]
    assert (heading["type"], heading["title"]) == ("heading", "Performance")
    new_id = await _new_requirement(tool, "p95 latency")
    await tool(
        "move_work_item_to_document",
        work_item_id=new_id,
        target_space_id=SPACE,
        target_document_name=DOC,
        previous_part_id=heading["id"],
    )
    rendered = await tool("read_document", **_doc())
    assert rendered["content"].endswith(
        f"# Performance\n\n**p95 latency** (`{new_id}`)"
    )


async def test_placeholder_dropped_by_update_orphans_item(tool: Tool) -> None:
    await tool(
        "update_document", **_doc(home_page_content_html='<p id="p-1">Intro.</p>')
    )
    parts = await tool("read_document_parts", **_doc())
    assert [p["id"] for p in parts["items"]] == ["p-1"]
    orphan = await tool("get_work_item", work_item_id=CHILD_REQ_ID)
    assert orphan["document_name"] == DOC  # membership survive body edit
    with pytest.raises(ToolError, match="already in Document"):
        await tool(
            "move_work_item_to_document",
            work_item_id=CHILD_REQ_ID,
            target_space_id=SPACE,
            target_document_name=DOC,
        )


async def test_created_record_listed_with_next_iteration(tool: Tool) -> None:
    created = await tool(
        "create_test_records",
        test_run_id=TEST_RUN_ID,
        items=[{"test_case_id": TESTCASE_ID, "result": "passed"}],
    )
    assert created["record_ids"][0].endswith(f"/{TESTCASE_ID}/1")
    passed = await tool("list_test_records", test_run_id=TEST_RUN_ID, result="passed")
    assert [r["id"] for r in passed["items"]] == created["record_ids"]


async def test_work_item_update_visible_and_partial(tool: Tool) -> None:
    await tool(
        "update_work_items",
        items=[{"work_item_id": FLOATING_TASK_ID, "priority": "90.0"}],
    )
    wi = await tool("get_work_item", work_item_id=FLOATING_TASK_ID)
    assert wi["priority"] == "90.0"
    assert wi["hyperlinks"][0]["uri"] == FLOATING_TASK_HYPERLINK_URI


async def test_copy_discoverable_with_duplicated_items(tool: Tool) -> None:
    await tool("copy_document", **_doc(target_document_name="FakeDocCopy"))
    docs = await tool("list_documents")
    assert {d["document_name"] for d in docs["items"]} == {DOC, "FakeDocCopy"}
    copy_parts = await tool(
        "read_document_parts", space_id=SPACE, document_name="FakeDocCopy"
    )
    assert [p["title"] for p in copy_parts["items"]] == [
        "",
        "Section A",
        "Child requirement",
        "Uncovered requirement",
    ]
    assert CHILD_REQ_ID not in {p["work_item_id"] for p in copy_parts["items"]}


async def test_comment_and_link_writes_visible(tool: Tool) -> None:
    await tool("create_document_comments", **_doc(comments=[{"text": "Needs review"}]))
    comments = await tool("list_document_comments", **_doc())
    assert comments["items"][-1]["text"] == "Needs review"
    link = {"role": "relates_to", "target_work_item_id": CHILD_REQ_ID}
    await tool("create_work_item_links", work_item_id=FLOATING_TASK_ID, links=[link])
    back = await tool(
        "list_work_item_links", work_item_id=CHILD_REQ_ID, direction="back"
    )
    assert FLOATING_TASK_ID in [x["id"] for x in back["items"]]
    await tool("delete_work_item_links", work_item_id=FLOATING_TASK_ID, links=[link])
    forward = await tool("list_work_item_links", work_item_id=FLOATING_TASK_ID)
    assert forward["items"] == []
