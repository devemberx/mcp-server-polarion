# Comments

Document and work item comments — one rule set for both. Read before touching `list_document_comments`, `list_work_item_comments`, `create_document_comments`, `create_work_item_comments`, `update_document_comment`, or `update_work_item_comment`. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET, POST | `projects/{p}/spaces/{s}/documents/{d}/comments` | REST-created top-level comments do not surface in the portal document sidebar |
| PATCH | `projects/{p}/spaces/{s}/documents/{d}/comments/{comment_id}` | |
| GET, POST | `projects/{p}/workitems/{wi}/comments` | |
| PATCH | `projects/{p}/workitems/{wi}/comments/{comment_id}` | |

## Read contract

- `text/plain` is never served back — every comment reads as `text/html`.
- Document comments follow the document-attachment `meta.totalCount` rule — see [attachments.md](attachments.md).

## Write contract

- A `text/plain` POST is HTML-escaped by the server, then stored and served as `text/html`. Markup written as plain text never renders.
- PATCH covers `resolved` only — the text is not editable.

## Server does not validate

- `attachment:` and `workitemimg:` refs in a comment body — accepted verbatim (201) and persisted; a ghost `workitemimg:` ref resolves in the work item comment portal render, so it shows as a visible broken image rather than being ignored. Guard: `guard/_attachment_refs.py`, wired into the create-comment tools via `guard_document_comment_attachment_refs` and its work item twin.

## Cross-domain

- The id formats that make those refs easy to get wrong: [attachments.md](attachments.md).
- A test record carries its comment as an attribute of the record, not as a comment resource: [test-runs.md](test-runs.md).

## Verified

| Date | Scope |
|---|---|
| 2026-07-21 | Plain-text storage, ghost ref rendering, portal sidebar gap |
