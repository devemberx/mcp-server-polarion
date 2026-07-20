"""Attachment model tests (``mcp_server_polarion.models.attachments``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    Attachment,
    AttachmentsCreateResult,
    DocumentAttachmentSpec,
)


class TestAttachment:
    def test_defaults(self):
        a = Attachment(id="1-screenshot-20260512-142738-1.png")
        assert a.file_name == ""
        assert a.title == ""
        assert a.length == 0
        assert a.updated == ""
        assert a.author_name == ""

    def test_full_attributes(self):
        a = Attachment(
            id="1-screenshot-20260512-142738-1.png",
            file_name="screenshot.png",
            title="screenshot",
            length=1024,
            updated="2026-05-12T14:27:38Z",
            author_name="Jane Doe",
        )
        assert a.id == "1-screenshot-20260512-142738-1.png"
        assert a.file_name == "screenshot.png"
        assert a.title == "screenshot"
        assert a.length == 1024
        assert a.updated == "2026-05-12T14:27:38Z"
        assert a.author_name == "Jane Doe"


class TestDocumentAttachmentSpec:
    def test_minimal(self):
        spec = DocumentAttachmentSpec(file_path="/home/user/screenshot.png")
        assert spec.file_path == "/home/user/screenshot.png"
        assert spec.file_name is None
        assert spec.title is None

    def test_full(self):
        spec = DocumentAttachmentSpec(
            file_path="/home/user/screenshot.png",
            file_name="renamed.png",
            title="Screenshot",
        )
        assert spec.file_name == "renamed.png"
        assert spec.title == "Screenshot"

    def test_typo_key_rejected(self):
        # Typo key must fail at parse -- silent drop = ghost field in Polarion.
        with pytest.raises(ValidationError) as exc:
            DocumentAttachmentSpec(file_path="/home/user/x.png", ttile="Oops")  # type: ignore[call-arg]
        assert exc.value.errors()[0]["type"] == "extra_forbidden"


class TestAttachmentsCreateResult:
    def test_dry_run(self):
        result = AttachmentsCreateResult(
            created=False,
            dry_run=True,
            attachment_ids=[],
            payload_preview={"data": [{"type": "document_attachments"}]},
        )
        assert result.dry_run is True
        assert result.attachment_ids == []
        assert result.payload_preview is not None

    def test_real_run(self):
        result = AttachmentsCreateResult(
            created=True,
            dry_run=False,
            attachment_ids=["proj/space/doc/screenshot.png"],
            payload_preview=None,
        )
        assert result.created is True
        assert result.attachment_ids == ["proj/space/doc/screenshot.png"]
        assert result.payload_preview is None
