"""Custom-field policy: the standard-attribute allowlists plus the read
(extract) and write (merge) helpers that split inline customs from standard
Polarion attributes.
"""

from __future__ import annotations

from typing import Final, cast

from mcp_server_polarion.models import JsonValue

# Standard-attribute allowlist (REST OpenAPI schema); anything outside is
# treated as a custom field, so new standard attrs misclassify until added.
STANDARD_WORK_ITEM_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "type",
        "title",
        "description",
        "status",
        "priority",
        "severity",
        "resolution",
        "resolvedOn",
        "created",
        "updated",
        "outlineNumber",
        "dueDate",
        "plannedStart",
        "plannedEnd",
        "initialEstimate",
        "remainingEstimate",
        "timeSpent",
        "hyperlinks",
    }
)

# Document-side mirror of ``STANDARD_WORK_ITEM_ATTRIBUTES``.
STANDARD_DOCUMENT_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "title",
        "type",
        "status",
        "homePageContent",
        "moduleFolder",
        "moduleName",
        "outlineNumbering",
        "renderingLayouts",
        "structureLinkRole",
        "usesOutlineNumbering",
        "autoSuspect",
        "branchedWithInitializedFields",
        "branchedWithQuery",
        "derivedFields",
        "derivedFromLinkRole",
        "created",
        "updated",
    }
)


def extract_custom_fields(
    attributes: dict[str, object],
    standard: frozenset[str],
) -> dict[str, object]:
    """Inline custom-field subset of ``attributes`` (keys outside *standard*),
    returned verbatim so rich-text values round-trip unchanged.
    """
    return {k: v for k, v in attributes.items() if k not in standard}


def merge_custom_fields(
    attributes: dict[str, JsonValue],
    customs: dict[str, object] | None,
    standard: frozenset[str],
) -> None:
    """Merge custom-field key/values into *attributes* in place; a key in
    *standard* raises ``ValueError`` (would shadow a tool parameter), ``None``
    values skipped. Values stored by reference — callers must NOT mutate
    *customs* before serialisation.
    """
    if not customs:
        return
    collisions = sorted(set(customs) & standard)
    if collisions:
        msg = (
            "custom_fields keys collide with standard Polarion attributes: "
            f"{collisions}. Use the matching standard tool parameter "
            "(e.g. ``title=``, ``status=``) instead of overriding via "
            "custom_fields."
        )
        raise ValueError(msg)
    for key, value in customs.items():
        if value is None:
            continue
        attributes[key] = cast(JsonValue, value)


__all__: list[str] = [
    "STANDARD_DOCUMENT_ATTRIBUTES",
    "STANDARD_WORK_ITEM_ATTRIBUTES",
    "extract_custom_fields",
    "merge_custom_fields",
]
