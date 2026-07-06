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
    guard_document_custom_fields,
    guard_document_enums,
)
from mcp_server_polarion.tools._shared.guard.documents import (
    _check_document_custom_keys,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
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

    ``data`` rows are bare heading placeholders that only drive pagination; the
    document type + customs ride in the ``included`` module resources.
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
        # Customs are grouped per type across the whole project in one GET.
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
        # Heading-discovery SQL + include=module surfaces each doc's type+customs.
        assert params["query"].startswith("SQL:(")
        assert params["include"] == "module"
        assert params["fields[documents]"] == "@all"
        # Every type's schema is stored from the one fetch.
        assert cache_mod._document_type_custom_key_cache.get(
            ("P", "systemReqSpecification")
        ) == frozenset({"version"})

    async def test_non_list_included_is_skipped_and_fails_closed(
        self, mock_client: AsyncMock
    ) -> None:
        # Malformed page: ``included`` not a list -> no keys sampled -> empty
        # schema refuses the write.
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
        # Stray entries in ``included`` (non-dict, non-document type) must not
        # break sampling of the well-formed document entries.
        response = _docs_list(("generic", {"doc_risk": 3}))
        included = response["included"]
        assert isinstance(included, list)
        included[:0] = ["stray", {"type": "workitems", "id": "P/W-1"}]
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

        with pytest.raises(PermissionError, match="lacks permission"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )


class TestGuardDocumentCustomFieldEnums:
    """Document-axis mirror; the shared enum core is exercised above."""

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
