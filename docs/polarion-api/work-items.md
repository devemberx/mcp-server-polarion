# Work items and links

Work item query, delete, document-membership, and link contracts. Read before touching `list_work_items`, `get_work_item`, `read_work_item`, `create_work_items`, `update_work_items`, `move_work_item_to_document`, `move_work_item_from_document`, or any `*_work_item_links` tool. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `projects/{p}/workitems` | `query=linkedWorkItems:{wi}` is the only back-link direction |
| DELETE | `projects/{p}/workitems` | 204 with a `{"data": [...]}` body |
| DELETE | `projects/{p}/workitems/{wi}` | 405 — the single-resource form does not exist |
| GET | `projects/{p}/workitems/{wi}/backlinkedworkitems` | unsupported |
| POST | `projects/{p}/workitems/{wi}/actions/moveToDocument` | flat body, not `{"data": [...]}` |
| POST | `projects/{p}/workitems/{wi}/actions/moveFromDocument` | takes no body |
| GET | `projects/{p}/workitems/{wi}/actions/getWorkflowActions` | per action: `nativeActionId`, `targetStatus`, `requiredFields`, and `available` or an `unavailableReason` |
| GET, POST, DELETE | `projects/{p}/workitems/{wi}/linkedworkitems` | DELETE takes a `{"data": [...]}` body |
| PATCH | `projects/{p}/workitems/{wi}/linkedworkitems/{role}/{targetProject}/{targetId}` | the link coordinate — the same 5 segments as the link `id` |

## Read contract

- Back-links come from `query=linkedWorkItems:{wi}`, and those results carry `role=None` — the role is not recoverable in that direction.

## Write contract

- The collection-body DELETE returns 204. This is API capability only: the repo ships no work item delete tool by policy, and documents stay 405 either way.
- An unknown `workflowAction` on the bulk PATCH fails the whole batch with 400 `"The workflow action '{action}' was not found."`. Actions, their target status, and their conditions are instance workflow config.

Document-membership moves:

- `moveToDocument` with neither `previousPart` nor `nextPart` appends the item at the end of the document.
- `previousPart` inserts the item right after that part.
- `moveToDocument` on an item whose `module` already names the target document returns 409 `"Work Item already in Document."`. This holds for a placed item and for an orphan alike.
- `moveToDocument` on an item that belongs to a different document moves it: 204, `module` switches, and the source body loses the placeholder.
- A heading work item never moves. `moveToDocument` and `moveFromDocument` both return 400 `"Cannot move headings."`, for a placed heading and an orphan alike. This check runs before the 409.
- A move under a heading auto-creates a link from the item to that heading. Its role is the document's `structureLinkRole`.
- `moveFromDocument` removes that structure link.
- `moveFromDocument` on a non-heading orphan returns 204 and clears `module`.

Links of the document's `structureLinkRole` are server-owned on a member:

- A move into a document drops every link of that role the item already has, whether or not a heading sits above it. Links of other roles survive. This loses data: a dropped link does not come back on `moveFromDocument`.
- Creating a link of that role on a member returns 201 with a link id but stores nothing (phantom success). This holds with or without a heading above the item. Verified for both `parent` and `relates_to`.
- The structure link ignores PATCH and DELETE: both return 204 and the link is unchanged.

Links:

- A repeated role + target fails the whole batch with 409 `"Work Item '{project}/{wi}' is already linked with role '{role}'."`. The repeat may be an existing link or an earlier entry in the same batch; either way nothing in the batch is written.

## Server does not validate

- `workitemimg:` refs in a description — accepted verbatim (204) and persisted. Guard: `guard/work_items.py` `guard_work_item_attachment_refs`, built on `guard/_attachment_refs.py`.
- Link targets and roles — accepted unvalidated. Guard: `guard/links.py` against the project's `workitem-link-role` enum, `guard/_targets.py` for target existence.

## Cross-domain

- Work item attachment ids, which make `workitemimg:` tokens unpredictable before upload: [attachments.md](attachments.md).
- Ghost refs in comment bodies: [comments.md](comments.md).
- Body placeholders, part ids, `structureLinkRole` values, and how a body PATCH orphans an item: [documents.md](documents.md).

## Verified

| Date | Scope |
|---|---|
| — | No dated probe; observed across v2506 development |
| 2026-11-15 | Move anchors, re-move 409, orphan release, auto structure link |
| 2026-11-16 | Cross-document move, heading move 400, structure-role ownership, duplicate link 409, unknown `workflowAction` |
