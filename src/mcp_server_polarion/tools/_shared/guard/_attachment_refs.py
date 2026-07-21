"""Attachment-ref guard core: extract scheme refs from HTML bodies; create =
reject outright, update = verify vs live attachment id set. No caching --
attachment set churn on UI upload; stale cache false-reject fresh upload or
false-accept deletion.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import unquote

from bs4 import BeautifulSoup

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared.guard._http import guarded_pages
from mcp_server_polarion.tools._shared.helpers import format_option_list

# Single source for body schemes -- domain wrappers import, no per-module
# literals. Prefix match case-insensitive (hand-written "Attachment:" real).
DOCUMENT_ATTACHMENT_SCHEME = "attachment"
WORK_ITEM_ATTACHMENT_SCHEME = "workitemimg"
_SCHEMES: tuple[str, ...] = (DOCUMENT_ATTACHMENT_SCHEME, WORK_ITEM_ATTACHMENT_SCHEME)


def extract_scheme_refs(html: str) -> list[tuple[str, str]]:
    """``(scheme_lower, token)`` pairs from ``img[src]``/``a[href]``, document
    order. Token = rest after colon, may be empty. No extension filter --
    portal embed ``.pptx``/``.txt`` via ``img`` too.
    """
    if not html or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    refs: list[tuple[str, str]] = []
    for img in soup.find_all("img"):
        _append_ref(img.attrs.get("src", ""), refs)
    for anchor in soup.find_all("a", href=True):
        _append_ref(anchor.get("href", ""), refs)
    return refs


def _append_ref(value: object, refs: list[tuple[str, str]]) -> None:
    """Append ``(scheme, token)`` when *value* carries a known scheme prefix."""
    if not isinstance(value, str):
        return
    lowered = value.lower()
    for scheme in _SCHEMES:
        prefix = f"{scheme}:"
        if lowered.startswith(prefix):
            refs.append((scheme, value[len(prefix) :]))
            return


def reject_any_scheme_refs(htmls: Iterable[str], what: str) -> None:
    """Create path: any ref of either scheme in any *htmls* -> ``ValueError``.
    Resource not exist yet = no attachment can exist. Run on converted
    Markdown->HTML bodies.
    """
    for html in htmls:
        if extract_scheme_refs(html):
            raise ValueError(
                f"attachments cannot exist before the {what} is created -- "
                "create it first, upload the attachment, then embed via the "
                "matching update tool."
            )


def _reject_wrong_scheme(
    refs: list[tuple[str, str]], *, expected_scheme: str, what: str
) -> None:
    """First ref with ``scheme != expected_scheme`` -> ``ValueError``."""
    for scheme, token in refs:
        if scheme != expected_scheme:
            raise ValueError(
                f"{what} references '{scheme}:{token}', but only "
                f"'{expected_scheme}:' refs resolve here -- wrong-scheme "
                "refs never resolve, remove it or use the matching scheme."
            )


def check_refs_against_ids(
    refs: list[tuple[str, str]],
    valid_ids: frozenset[str],
    *,
    expected_scheme: str,
    list_tool: str,
    what: str,
) -> None:
    """Update path: wrong-scheme refs first (never resolve in this body type,
    must not masquerade as dangling id), then dangling tokens. Token match =
    raw or ``unquote(token)`` in *valid_ids* -- portal store URL-encoded
    token, list API serve raw id.
    """
    _reject_wrong_scheme(refs, expected_scheme=expected_scheme, what=what)

    unmatched = sorted(
        {
            token
            for _scheme, token in refs
            if token not in valid_ids and unquote(token) not in valid_ids
        }
    )
    if unmatched:
        raise ValueError(
            f"{what} references attachment id(s) {format_option_list(unmatched)} "
            f"that do not exist -- resolve via {list_tool} first."
        )


async def fetch_attachment_ids(
    client: PolarionClient,
    path: str,
    resource_type: str,
    *,
    what: str,
    project_id: str,
) -> frozenset[str]:
    """Live short-id set for one resource's attachments via ``guarded_pages``
    (fail closed); ``@basic`` fieldset.
    """
    base_params: dict[str, str | int] = {f"fields[{resource_type}]": "@basic"}
    ids: set[str] = set()
    async for data, _response in guarded_pages(
        client, path, base_params, what=what, project_id=project_id
    ):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            attributes = entry.get("attributes")
            if not isinstance(attributes, dict):
                continue
            attachment_id = attributes.get("id")
            if isinstance(attachment_id, str) and attachment_id:
                ids.add(attachment_id)
    return frozenset(ids)


async def guard_attachment_refs_many(  # noqa: PLR0913
    client: PolarionClient,
    htmls: Iterable[str],
    *,
    path: str,
    resource_type: str,
    expected_scheme: str,
    list_tool: str,
    what: str,
    project_id: str,
) -> None:
    """Block refs across all *htmls* to nonexistent attachments or other
    resource's scheme. One GET total, not per html -- single update call
    pass ``[html]``; comment batch passes the whole set, guarded as one
    union. Both domain axes share this -- only
    path/resource_type/expected_scheme/list_tool differ.
    """
    refs = [ref for html in htmls for ref in extract_scheme_refs(html)]
    if not refs:
        return
    # Wrong scheme never resolve regardless of real attachments -- reject
    # before GET spend.
    _reject_wrong_scheme(refs, expected_scheme=expected_scheme, what=what)

    valid_ids = await fetch_attachment_ids(
        client, path, resource_type, what=what, project_id=project_id
    )
    check_refs_against_ids(
        refs,
        valid_ids,
        expected_scheme=expected_scheme,
        list_tool=list_tool,
        what=what,
    )
