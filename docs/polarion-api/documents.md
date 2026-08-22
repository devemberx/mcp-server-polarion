# Documents

Document contracts, `renderingLayouts`, and the copy action. Read before touching `list_documents`, `get_document`, `read_document`, `read_document_parts`, `create_document`, `update_document`, or `copy_document`. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `projects/{p}/documents` | absent on some builds |
| GET, POST, PATCH | `projects/{p}/spaces/{s}/documents` | DELETE 405 — documents are not REST-deletable |
| POST | `projects/{p}/spaces/{s}/documents/{d}/actions/copy` | flat body, not `{"data": [...]}` |
| GET | `projects/{p}/spaces` | 404 — there is no template space, so templates are not API-readable |

## Read contract

- Default GET attrs are `moduleFolder`, `status`, `title`.
- `renderingLayouts` is absent from that default and served under `@all`.
- No value normalization on `renderingLayouts` — `default` is served back verbatim.
- `properties` order is not round-tripped: the server serves its own order, content preserved.

## Write contract

- Every `renderingLayouts` entry needs `layouter`. `type` alone returns 400 `"Required member 'uri' was not found."` — the pointer index in that error is misleading.
- Known-valid layouters: `paragraph`, `section`, `title`, `default`, `titleTestSteps`. The set is not enumerable via the API; live documents use all five.
- `layouter` is server-validated — a bad value is 400.
- `label` and `properties` are optional.
- Multi-entry is accepted and order-preserved. Duplicate `type` is accepted, with undefined UI precedence.
- PATCH REPLACES the whole `renderingLayouts` array; `[]` clears it.
- A missing layout breaks the work item property panel in the UI.
- The copy action takes a flat body, not the `{"data": [...]}` wrapper, and its 201 `data` is a single dict.

Layout shape of a UI-made document:

- A freshly UI-made document carries, for every work item type: `layouter: paragraph`, a `label`, `fieldsAtStart=id`, `fieldsAtEnd=status` — confirmed on both an SRS and a test spec document.
- `label` is the work item type enum `name`, which `getAvailableOptions` serves beside `id`.
- `section` and `titleTestSteps` come from template-seeded documents, which the API cannot read back.

## Server does not validate

- Body `<img src="attachment:{id}">` refs — a nonexistent id persists verbatim (204) and renders identically to a real one in `read_document`. Guard: `guard/documents.py` `guard_document_attachment_refs`.
- `renderingLayouts[].type` — accepted unvalidated and ghosts. Guard: `guard/documents.py` `guard_document_rendering_layout_types`.
- `renderingLayouts[].properties` keys — accepted unvalidated, `unguarded`.
- Copy `linkOriginalItemsWithRole` — a bad role creates a ghost link per copied item. Guard: `guard/links.py` `guard_work_item_link_roles`, called against the **target** project's `workitem-link-role` enum.

## Cross-domain

- Document attachments and `attachment:` body refs: [attachments.md](attachments.md).
- Document comments and the portal sidebar gap: [comments.md](comments.md).

## Verified

| Date | Scope |
|---|---|
| 2026-08-07 | `renderingLayouts` required members, layouter validation |
| 2026-08-12 | Layout shape of UI-made documents, template readability |
| 2026-08-13 | `properties` order round-trip |
