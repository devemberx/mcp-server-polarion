# Work items and links

Work item query, delete, and link contracts. Read before touching `list_work_items`, `get_work_item`, `read_work_item`, `create_work_items`, `update_work_items`, or any `*_work_item_links` tool. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `projects/{p}/workitems` | `query=linkedWorkItems:{wi}` is the only back-link direction |
| DELETE | `projects/{p}/workitems` | 204 with a `{"data": [...]}` body |
| DELETE | `workitems/{wi}` | 405 — the single-resource form does not exist |
| GET | `workitems/{wi}/backlinkedworkitems` | unsupported |

## Read contract

- Back-links come from `query=linkedWorkItems:{wi}`, and those results carry `role=None` — the role is not recoverable in that direction.

## Write contract

- The collection-body DELETE returns 204. This is API capability only: the repo ships no work item delete tool by policy, and documents stay 405 either way.

## Server does not validate

- `workitemimg:` refs in a description — accepted verbatim (204) and persisted. Guard: `guard/work_items.py` `guard_work_item_attachment_refs`, built on `guard/_attachment_refs.py`.
- Link targets and roles — accepted unvalidated. Guard: `guard/links.py` against the project's `workitem-link-role` enum, `guard/_targets.py` for target existence.

## Cross-domain

- Work item attachment ids, which make `workitemimg:` tokens unpredictable before upload: [attachments.md](attachments.md).
- Ghost refs in comment bodies: [comments.md](comments.md).

## Verified

| Date | Scope |
|---|---|
| — | No dated probe; observed across v2506 development |
