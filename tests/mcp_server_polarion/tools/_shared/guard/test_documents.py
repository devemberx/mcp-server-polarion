"""Document guard tests: enum args and custom-field keys/values."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.cache import (
    store_document_type_custom_keys,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_document_attachment_refs,
    guard_document_comment_attachment_refs,
    guard_document_custom_fields,
    guard_document_enums,
    guard_document_rendering_layout_types,
)
from mcp_server_polarion.tools._shared.guard.documents import (
    _check_document_custom_keys,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    attachments_response,
    enum_response,
)


class TestGuardDocumentEnums:
    """Validation of document type / status."""

    async def test_listed_value_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["systemRequirementSpecification"])

        await guard_document_enums(
            mock_client,
            "P",
            "systemRequirementSpecification",
            type="systemRequirementSpecification",
        )

    async def test_unlisted_value_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["systemRequirementSpecification"])

        with pytest.raises(ValueError) as exc:
            await guard_document_enums(
                mock_client,
                "P",
                "systemRequirementSpecification",
                type="productRequirementSpecification",
            )

        assert "productRequirementSpecification" in str(exc.value)


def _docs_list(*docs: tuple[str, dict[str, object]]) -> dict[str, object]:
    """Heading + ``include=module`` sample: one module per document in ``included``.

    ``data`` rows = bare heading placeholders, only drive pagination;
    document type + customs ride in ``included`` module resources.
    """
    return {
        "data": [{"type": "workitems"} for _ in docs],
        "included": [
            {
                "type": "documents",
                "id": f"P/_default/D{i}",
                "attributes": {"title": "t", "type": dtype, **customs},
            }
            for i, (dtype, customs) in enumerate(docs)
        ],
        "meta": {"totalCount": len(docs)},
    }


class TestGuardDocumentCustomFieldKeys:
    """Validation of ``custom_fields`` keys via the project-wide document sample."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_document_custom_fields(mock_client, "P", "generic", {})

        mock_client.get.assert_not_awaited()

    async def test_cached_schema_passes_without_sample(
        self, mock_client: AsyncMock
    ) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"doc_risk"}))

        await _check_document_custom_keys(mock_client, "P", "generic", {"doc_risk": 3})

        mock_client.get.assert_not_awaited()

    async def test_sample_primes_schema_and_passes(
        self, mock_client: AsyncMock
    ) -> None:
        # Customs grouped per type across whole project in one GET.
        mock_client.get.return_value = _docs_list(
            ("generic", {"doc_risk": 3}),
            ("generic", {"owner": "x"}),
            ("systemReqSpecification", {"version": "1.0"}),
        )

        await _check_document_custom_keys(
            mock_client, "P", "generic", {"doc_risk": 9, "owner": "y"}
        )

        mock_client.get.assert_awaited_once()
        params = mock_client.get.call_args.kwargs["params"]
        path = mock_client.get.call_args.args[0]
        assert path == "/projects/P/workitems"
        # Heading-discovery SQL + include=module surface each doc type+customs.
        assert params["query"].startswith("SQL:(")
        assert params["include"] == "module"
        assert params["fields[documents]"] == "@all"
        # Every type schema stored from the one fetch.
        assert cache_mod._document_type_custom_key_cache.get(
            ("P", "systemReqSpecification")
        ) == frozenset({"version"})

    async def test_non_list_included_is_skipped_and_fails_closed(
        self, mock_client: AsyncMock
    ) -> None:
        # Malformed page: ``included`` not a list -> no keys sampled ->
        # empty schema refuse the write.
        mock_client.get.return_value = {
            "data": [{"type": "workitems"}],
            "included": {"type": "documents"},
        }

        with pytest.raises(RuntimeError, match="Cannot verify custom_fields"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"doc_risk": 1}
            )

    async def test_non_document_included_entries_are_skipped(
        self, mock_client: AsyncMock
    ) -> None:
        # Stray entries in ``included`` (non-dict, non-document type,
        # non-dict attributes, missing document type) must not break
        # sampling of well-formed entries.
        response = _docs_list(("generic", {"doc_risk": 3}))
        included = response["included"]
        assert isinstance(included, list)
        included[:0] = [
            "stray",
            {"type": "workitems", "id": "P/W-1"},
            {"type": "documents", "attributes": "stray"},
            {"type": "documents", "attributes": {"title": "untyped", "ghost": 1}},
        ]
        mock_client.get.return_value = response

        await _check_document_custom_keys(mock_client, "P", "generic", {"doc_risk": 9})

    async def test_unknown_key_against_fresh_sample_rejects_without_retry(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _docs_list(("generic", {"doc_risk": 3}))

        with pytest.raises(ValueError) as exc:
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )

        msg = str(exc.value)
        assert "ghost_key" in msg
        assert "doc_risk" in msg
        mock_client.get.assert_awaited_once()

    async def test_cached_schema_unknown_key_refetches_then_passes(
        self, mock_client: AsyncMock
    ) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"doc_risk"}))
        mock_client.get.return_value = _docs_list(
            ("generic", {"doc_risk": 3, "new_field": 1})
        )

        await _check_document_custom_keys(mock_client, "P", "generic", {"new_field": 1})

        mock_client.get.assert_awaited_once()

    async def test_empty_sample_fails_closed(self, mock_client: AsyncMock) -> None:
        # No document of this type has any custom -> schema empty -> block.
        mock_client.get.return_value = _docs_list(("systemReqSpecification", {"v": 1}))

        with pytest.raises(RuntimeError, match="Refusing the write") as exc:
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"doc_risk": 3}
            )

        msg = str(exc.value)
        assert "doc_risk" in msg
        assert "ask the user" in msg.lower()
        assert "save one" not in msg.lower()
        assert "retry" not in msg.lower()

    async def test_sample_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )

    async def test_sample_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="Refusing the write"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )


class TestGuardDocumentCustomFieldEnums:
    """Document-axis mirror; shared enum core exercised above."""

    @pytest.fixture(autouse=True)
    def _prime_key_schemas(self, _reset_guard_caches: None) -> None:
        store_document_type_custom_keys(
            "P", "generic", frozenset({"docRisk", "freeText"})
        )

    async def test_valid_option_id_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["high", "moderate", "low"])

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"docRisk": "low"}
        )  # must not raise

    async def test_unknown_option_id_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["high", "moderate", "low"])

        with pytest.raises(ValueError, match=r"'docRisk'.*'severe'"):
            await guard_document_custom_fields(
                mock_client, "P", "generic", {"docRisk": "severe"}
            )

    async def test_queries_documents_fields_endpoint(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["high"])

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"docRisk": "high"}
        )

        path = mock_client.get.call_args.args[0]
        expected = "/projects/P/documents/fields/docRisk/actions/getAvailableOptions"
        assert path == expected
        assert mock_client.get.call_args.kwargs["params"]["type"] == "generic"

    async def test_non_enum_field_defers_on_404(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("not enum", status_code=404)

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"freeText": "anything"}
        )  # must not raise


class TestGuardDocumentAttachmentRefs:
    """Update-path guard on ``home_page_content_html`` attachment refs."""

    async def test_no_refs_returns_without_get(self, mock_client: AsyncMock) -> None:
        await guard_document_attachment_refs(
            mock_client, "P", "S", "D", "<p>no refs here</p>"
        )

        mock_client.get.assert_not_awaited()

    async def test_matching_raw_ref_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_document_attachment_refs(
            mock_client, "P", "S", "D", '<img src="attachment:1-x.png"/>'
        )  # must not raise

    async def test_url_encoded_token_matches_raw_id(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(
            ["1-test file.txt"], meta=False
        )

        await guard_document_attachment_refs(
            mock_client,
            "P",
            "S",
            "D",
            '<img src="attachment:1-test%20file.txt"/>',
        )  # must not raise

    async def test_dangling_ref_rejects_naming_list_tool(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-real.png"], meta=False)

        with pytest.raises(ValueError, match="list_document_attachments") as exc:
            await guard_document_attachment_refs(
                mock_client, "P", "S", "D", '<img src="attachment:1-ghost.png"/>'
            )

        assert "1-ghost.png" in str(exc.value)

    async def test_wrong_scheme_rejects_before_any_get(
        self, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="attachment") as exc:
            await guard_document_attachment_refs(
                mock_client, "P", "S", "D", '<img src="workitemimg:1-x.png"/>'
            )

        assert "workitemimg" in str(exc.value)
        mock_client.get.assert_not_awaited()

    async def test_get_failure_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_document_attachment_refs(
                mock_client, "P", "S", "D", '<img src="attachment:1-x.png"/>'
            )

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="Refusing the write"):
            await guard_document_attachment_refs(
                mock_client, "P", "S", "D", '<img src="attachment:1-x.png"/>'
            )

    async def test_get_uses_encoded_path_and_basic_fieldset(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_document_attachment_refs(
            mock_client, "P", "My Space", "D", '<img src="attachment:1-x.png"/>'
        )

        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/spaces/My%20Space/documents/D/attachments"
        assert params["fields[document_attachments]"] == "@basic"


class TestGuardDocumentCommentAttachmentRefs:
    """Create-path guard on document comment ``text`` attachment refs."""

    async def test_matching_ref_passes_via_attachments_path(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_document_comment_attachment_refs(
            mock_client, "P", "S", "D", ['<img src="attachment:1-x.png"/>']
        )  # must not raise

        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/spaces/S/documents/D/attachments"
        assert params["fields[document_attachments]"] == "@basic"

    async def test_dangling_ref_rejects_naming_list_tool(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-real.png"], meta=False)

        with pytest.raises(ValueError, match="list_document_attachments") as exc:
            await guard_document_comment_attachment_refs(
                mock_client, "P", "S", "D", ['<img src="attachment:1-ghost.png"/>']
            )

        assert "1-ghost.png" in str(exc.value)
        assert "Comment(s) on" in str(exc.value)


class TestGuardDocumentRenderingLayoutTypes:
    """Validation of the work item type ids behind ``renderingLayouts``."""

    async def test_listed_types_pass(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(
            ["softwarerequirement", "softwaretestcase"]
        )

        await guard_document_rendering_layout_types(
            mock_client, "P", ["softwarerequirement", "softwaretestcase"]
        )

    async def test_unknown_type_rejects_naming_discovery_tool(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["softwarerequirement"])

        with pytest.raises(ValueError, match="list_work_item_enum_options") as exc:
            await guard_document_rendering_layout_types(
                mock_client, "P", ["nosuchtype_zz"]
            )

        assert "nosuchtype_zz" in str(exc.value)

    async def test_message_names_the_parameter_not_bare_type(
        self, mock_client: AsyncMock
    ) -> None:
        # Both write tools carry own ``type`` parameter — colliding name
        # misroute model to wrong argument.
        mock_client.get.return_value = enum_response(["task"])

        with pytest.raises(ValueError) as exc:
            await guard_document_rendering_layout_types(mock_client, "P", ["Task"])

        assert "rendering_layout_types" in str(exc.value)
        assert "type='Task'" not in str(exc.value)

    async def test_every_unknown_id_reported_in_one_error(
        self, mock_client: AsyncMock
    ) -> None:
        # Per-id loop cost one failed write per bad id at 3 req/s.
        mock_client.get.return_value = enum_response(["task"])

        with pytest.raises(ValueError) as exc:
            await guard_document_rendering_layout_types(
                mock_client, "P", ["task", "ghost_a", "ghost_b"]
            )

        assert "ghost_a" in str(exc.value)
        assert "ghost_b" in str(exc.value)

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_type_rejected_without_http(
        self, mock_client: AsyncMock, blank: str
    ) -> None:
        # Blank entry persist verbatim + read path filter it out = ghost
        # invisible to every later round trip.
        with pytest.raises(ValueError, match="blank") as exc:
            await guard_document_rendering_layout_types(
                mock_client, "P", [blank, "task"]
            )

        assert "rendering_layout_types" in str(exc.value)
        mock_client.get.assert_not_called()

    async def test_validates_against_type_agnostic_work_item_enum(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["softwarerequirement"])

        await guard_document_rendering_layout_types(
            mock_client, "P", ["softwarerequirement"]
        )

        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/workitems/fields/type/actions/getAvailableOptions"
        assert params["type"] == "~"

    async def test_duplicate_types_reject_without_http(
        self, mock_client: AsyncMock
    ) -> None:
        # Server store both, UI precedence undefined -- refuse rather than dedupe.
        with pytest.raises(ValueError, match="duplicate") as exc:
            await guard_document_rendering_layout_types(
                mock_client, "P", ["task", "task"]
            )

        assert "task" in str(exc.value)
        mock_client.get.assert_not_called()

    async def test_empty_list_skips_probe(self, mock_client: AsyncMock) -> None:
        await guard_document_rendering_layout_types(mock_client, "P", [])

        mock_client.get.assert_not_called()

    async def test_missing_enum_endpoint_defers(self, mock_client: AsyncMock) -> None:
        # 404 = endpoint absent; defer to Polarion rather than block.
        mock_client.get.side_effect = PolarionNotFoundError("no options")

        await guard_document_rendering_layout_types(mock_client, "P", ["task"])

    async def test_probe_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("boom")

        with pytest.raises(RuntimeError):
            await guard_document_rendering_layout_types(mock_client, "P", ["task"])

    async def test_auth_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionAuthError("denied")

        with pytest.raises(PermissionError):
            await guard_document_rendering_layout_types(mock_client, "P", ["task"])
