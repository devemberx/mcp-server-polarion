"""Attachment model tests (``mcp_server_polarion.models.attachments``)."""

from __future__ import annotations

from mcp_server_polarion.models import Attachment


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
