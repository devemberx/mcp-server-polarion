"""Attachment-ref guard core: extract ``attachment:``/``workitemimg:`` scheme
refs from HTML bodies, reject on create (resource can't own attachments
yet), verify against a live attachment id set on update. No caching --
attachment sets churn on every UI upload, a stale cache would false-reject
fresh uploads or false-accept deletions.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import unquote

from bs4 import BeautifulSoup

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared.guard._http import guarded_pages
from mcp_server_polarion.tools._shared.helpers import format_option_list

# Body-embedded schemes; prefix match case-insensitive (hand-written HTML may
# write "Attachment:").
_SCHEMES: tuple[str, ...] = ("attachment", "workitemimg")


def extract_scheme_refs(html: str) -> list[tuple[str, str]]:
    """``(scheme_lower, token)`` pairs from ``img[src]`` and ``a[href]``
    values, in document order. Token = remainder after the colon, may be
    empty (dangling). No extension filtering -- portal embeds ``.pptx``/
    ``.txt`` via ``img`` too.
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

    Resource does not exist yet, so no attachment can exist. Runs on
    converted (Markdown->HTML) bodies -- catches both Markdown image syntax
    and hand-written HTML.
    """
    for html in htmls:
        if extract_scheme_refs(html):
            raise ValueError(
                f"attachments cannot exist before the {what} is created -- "
                "create it first, upload the attachment, then embed via the "
                "matching update tool."
            )


def check_refs_against_ids(
    refs: list[tuple[str, str]],
    valid_ids: frozenset[str],
    *,
    expected_scheme: str,
    list_tool: str,
    what: str,
) -> None:
    """Update path: reject wrong-scheme refs, then dangling tokens.

    Wrong-scheme ref (``scheme != expected_scheme``) never resolves in this
    body type, checked first so it can't masquerade as a dangling id. Token
    matches when raw or ``urllib.parse.unquote(token)`` is in *valid_ids* --
    portal stores URL-encoded tokens for non-ASCII names, the list API
    serves raw ids.
    """
    for scheme, token in refs:
        if scheme != expected_scheme:
            raise ValueError(
                f"{what} references '{scheme}:{token}', but only "
                f"'{expected_scheme}:' refs resolve here -- wrong-scheme "
                "refs never resolve, remove it or use the matching scheme."
            )

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
    """Live short-id set for one document/work item's attachments, paged via
    ``guarded_pages`` (fail closed). ``@basic`` fieldset -- id is all the
    caller needs.
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
