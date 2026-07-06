"""Shared custom-field key helpers: unknown-key rejection and key
extraction from JSON:API data lists.
"""

from __future__ import annotations

import pytest

from mcp_server_polarion.tools._shared.guard._common import (
    custom_keys_from_data_list,
    reject_unknown_custom_keys,
)


class TestRejectUnknownCustomKeys:
    def test_all_known_keys_pass(self) -> None:
        reject_unknown_custom_keys(
            {"risk": "high", "freeText": "x"},
            frozenset({"risk", "freeText"}),
            scope="project 'P' type 'task'",
            discovery_tool="get_work_item",
        )

    def test_empty_custom_fields_pass(self) -> None:
        reject_unknown_custom_keys(
            {},
            frozenset(),
            scope="project 'P' type 'task'",
            discovery_tool="get_work_item",
        )

    def test_unknown_keys_raise_sorted_with_scope_and_tool(self) -> None:
        with pytest.raises(ValueError, match=r"\['aaa', 'zzz'\]") as exc_info:
            reject_unknown_custom_keys(
                {"zzz": 1, "risk": "high", "aaa": 2},
                frozenset({"risk"}),
                scope="project 'P' type 'task'",
                discovery_tool="get_work_item",
            )

        message = str(exc_info.value)
        assert "get_work_item" in message
        assert "project 'P' type 'task'" in message
        assert "ghost" in message


class TestCustomKeysFromDataList:
    def test_collects_non_allowlisted_attribute_keys(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"attributes": {"id": "P/W-1", "risk": "high"}},
                {"attributes": {"id": "P/W-2", "freeText": "x"}},
            ]
        }

        keys = custom_keys_from_data_list(response, frozenset({"id"}))

        assert keys == frozenset({"risk", "freeText"})

    def test_missing_data_returns_empty(self) -> None:
        assert custom_keys_from_data_list({}, frozenset()) == frozenset()

    def test_non_list_data_returns_empty(self) -> None:
        response: dict[str, object] = {"data": {"attributes": {"risk": "high"}}}

        assert custom_keys_from_data_list(response, frozenset()) == frozenset()

    def test_non_dict_entries_and_attributes_skipped(self) -> None:
        response: dict[str, object] = {
            "data": ["stray", {"attributes": "stray"}, {"attributes": {"risk": 1}}]
        }

        assert custom_keys_from_data_list(response, frozenset()) == frozenset({"risk"})
