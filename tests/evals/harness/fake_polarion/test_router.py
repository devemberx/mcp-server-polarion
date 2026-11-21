"""Route table: full-path template match, per-segment decode, body decoding."""

from __future__ import annotations

import httpx
import pytest

from evals.harness.fake_polarion.router import Call, Router
from evals.harness.fake_polarion.world import Fault


def _ok(_world: object, _call: Call) -> httpx.Response:
    return httpx.Response(204)


def _call(body: object) -> Call:
    return Call(httpx.Request("POST", "https://h/x"), {}, body)


class TestResolve:
    def test_template_match_full_path_only(self) -> None:
        router = Router()
        router.get("/projects/{project}/workitems/{work_item}")(_ok)
        assert router.resolve("GET", "/projects/P/workitems/W-1") is not None
        assert router.resolve("GET", "/projects/P/workitems/W-1/comments") is None
        assert router.resolve("GET", "/projects/P/workitems") is None

    def test_method_must_match(self) -> None:
        router = Router()
        router.post("/a")(_ok)
        assert router.resolve("GET", "/a") is None

    def test_segments_percent_decoded_after_match(self) -> None:
        # Encoded "/" stay inside one segment (document names may carry "/").
        router = Router()
        router.patch("/docs/{document}")(_ok)
        resolved = router.resolve("PATCH", "/docs/A%2FB%20C")
        assert resolved is not None
        assert resolved[1] == {"document": "A/B C"}

    def test_literal_segments_are_not_regex(self) -> None:
        router = Router()
        router.delete("/a.b/~")(_ok)
        assert router.resolve("DELETE", "/a.b/~") is not None
        assert router.resolve("DELETE", "/aXb/~") is None


class TestCallBody:
    def test_entries_need_data_list(self) -> None:
        assert _call({"data": [{"a": 1}, "junk"]}).entries() == [{"a": 1}]
        with pytest.raises(Fault) as excinfo:
            _call({"data": {}}).entries()
        assert excinfo.value.status == 400

    def test_resource_needs_data_object(self) -> None:
        assert _call({"data": {"id": "x"}}).resource() == {"id": "x"}
        with pytest.raises(Fault):
            _call(None).resource()

    def test_flat_defaults_to_empty(self) -> None:
        assert _call({"targetDocument": "d"}).flat() == {"targetDocument": "d"}
        assert _call(None).flat() == {}
