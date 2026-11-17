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
- `structureLinkRole` is `parent` on UI-made documents and `relates_to` on REST-made ones.

Body and parts:

- `homePageContent` is the whole document. `/parts` is a 1:1 split of its top-level blocks, in body order — the body is the source of truth.
- A member work item sits in the body as an empty macro placeholder whose `id` is `polarion_wiki macro name=module-workitem;params=id={wi}`.
- A heading placeholder is `<hN id="...">` with no text — the heading text is the work item `title`. A body item placeholder is a `<div id="...">`; a move writes the item id as its text, the copy action leaves it empty.
- Part `id` is `heading_{wi}` for a heading, `workitem_{wi}` for a body item, and the block's own `id` verbatim for a normal block.
- Part attrs are `id`, `content`, and `type` (`heading`, `workitem`, `normal`).
- Heading parts add `headingText` and `level`, where `level` is N-1 for `<hN>`. Body item parts add `level` `0` and `layout` `0`.
- Part relationships are `workItem`, `previousPart`, and `nextPart`. `previousPart` is absent on the first part, and `nextPart` on the last.
- `/parts` honors `page[size]` and `page[number]`. `meta.totalCount` is served on every page once the body spans more than one page, and on an overshoot page. A single-page read serves no `meta`.
- Any top-level element without an `id` makes `/parts` return 500, on every page. This holds for every element, not just block tags: `p`, `table`, `hr`, `img`, `br`, and `span` are all verified.

## Write contract

- Every `renderingLayouts` entry needs `layouter`. `type` alone returns 400 `"Required member 'uri' was not found."` — the pointer index in that error is misleading.
- Known-valid layouters: `paragraph`, `section`, `title`, `default`, `titleTestSteps`. The set is not enumerable via the API; live documents use all five.
- `layouter` is server-validated — a bad value is 400.
- `label` and `properties` are optional.
- Multi-entry is accepted and order-preserved. Duplicate `type` is accepted, with undefined UI precedence.
- PATCH REPLACES the whole `renderingLayouts` array; `[]` clears it.
- A missing layout breaks the work item property panel in the UI.
- The copy action takes a flat body, not the `{"data": [...]}` wrapper, and its 201 `data` is a single dict.

Body PATCH:

- Block `id` values are stored verbatim. The `polarion_*` prefix is UI convention only.
- A duplicate block `id` fails the whole PATCH with 400.
- Non-blank text at the top level fails the whole PATCH with 400 `"Invalid Home Page HTML. Unexpected top-level text nodes"`. Whitespace between blocks is accepted.
- A plain `<hN>` with no placeholder `id` spawns a heading work item (next project-global id, `title` = the heading text) and is rewritten to a heading placeholder on save.
- Re-sending the same heading text as plain `<hN>` spawns another heading work item.
- A PATCH that drops a placeholder removes the item from the body and `/parts`, but the item keeps `module`. It becomes an orphan that no read of the document shows.

Copy:

- With only `targetDocumentName` in the body, the copy lands in the source space.
- Every member work item is duplicated under a new project-global id, and the placeholders are rewritten to the new ids.
- Normal blocks are copied verbatim, `id` included.
- The copy keeps `type`, `structureLinkRole`, and `usesOutlineNumbering`. `status` resets to `draft`, and `title` is absent.
- Structure links are re-created among the copies.
- `linkOriginalItemsWithRole` adds one link from each copy to its original. The originals gain no outgoing link.

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
- Moving items into or out of the body, and the structure link a move creates: [work-items.md](work-items.md).

## Verified

| Date | Scope |
|---|---|
| 2026-05-20 | Block `id` storage, duplicate-id PATCH, heading spawn on re-send |
| 2026-08-07 | `renderingLayouts` required members, layouter validation |
| 2026-08-12 | Layout shape of UI-made documents, template readability |
| 2026-08-13 | `properties` order round-trip |
| 2026-11-15 | Body/parts split, part shape, placeholder forms, orphan PATCH, copy semantics |
| 2026-11-16 | `/parts` paging and `meta`, anchorless 500 per element kind, top-level text 400 |
