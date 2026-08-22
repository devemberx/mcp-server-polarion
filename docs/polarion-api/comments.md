# Comments

Document, work item, and test record comments — one rule set across all three. Read before touching `list_*_comments`, `create_*_comments`, or `update_*_comment`. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET, POST, PATCH | `documents/{d}/comments` | REST-created top-level comments do not surface in the portal document sidebar |
| GET, POST, PATCH | `workitems/{wi}/comments` | |
| GET, POST, PATCH | `testruns/{r}/testrecords/{tcProj}/{tc}/{iter}/comments` | |

## Read contract

- `text/plain` is never served back — every comment reads as `text/html`.
- Document comments follow the document-attachment `meta.totalCount` rule — see [attachments.md](attachments.md).

## Write contract

- A `text/plain` POST is HTML-escaped by the server, then stored and served as `text/html`. Markup written as plain text never renders.
- PATCH covers `resolved` only — the text is not editable.

## Server does not validate

- `attachment:` and `workitemimg:` refs in a comment body — accepted verbatim (201) and persisted. Guard: `guard/_attachment_refs.py`, wired into the create-comment tools via `guard_document_comment_attachment_refs` and its work item twin.
- A ghost `workitemimg:` ref does resolve in the work item comment portal render, so it shows as a visible broken image rather than being ignored.

## Cross-domain

- The id formats that make those refs easy to get wrong: [attachments.md](attachments.md).
- Test record comment storage is the same rule, listed with the record contract: [test-runs.md](test-runs.md).

## Verified

| Date | Scope |
|---|---|
| 2026-07-21 | Plain-text storage, ghost ref rendering, portal sidebar gap |
