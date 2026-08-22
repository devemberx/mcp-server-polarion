# Attachments

Document, work item, and test record attachments — three resources with three rule sets. Read before touching `list_*_attachments`, `create_*_attachments`, or `get_*_attachment_content`. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET, POST | `projects/{p}/spaces/{s}/documents/{d}/attachments` | DELETE 405 — uploads REST-irreversible |
| GET | `projects/{p}/spaces/{s}/documents/{d}/attachments/{id}/content` | |
| GET, POST | `projects/{p}/workitems/{wi}/attachments` | DELETE 204 |
| GET | `projects/{p}/workitems/{wi}/attachments/{id}/content` | |
| GET, POST | `projects/{p}/testruns/{r}/testrecords/{tcProj}/{tcId}/{iter}/attachments` | DELETE 204 |
| GET | `projects/{p}/testruns/{r}/testrecords/{tcProj}/{tcId}/{iter}/attachments/{id}` | `data` is a dict, default attrs `@basic`; `/content` serves the bytes |

## Read contract

| Aspect | Document | Work item | Test record |
|---|---|---|---|
| Default GET attrs | not probed — the tools always send `fields[]` | not probed — the tools always send `fields[]` | none — `type`/`id`/`links` only |
| `@basic` | `id,fileName,title` | `id,fileName` | `id,fileName,title` |
| `meta.totalCount` | only when a page overshoots a non-empty collection; absent on an empty one | every page of a multi-page collection; absent single-page, empty collection not probed | every page of a multi-page collection **and** on overshoot; absent single-page or empty |
| Body ref scheme | `attachment:{id}` | `workitemimg:{id}` | n/a |

- Attributes are `id`, `fileName`, `title`, `updated`, `length` — no `created`, no mime anywhere.
- `content` is served as `application/octet-stream`; test record content is byte-exact.
- `sort` is rejected with 400 on all three — order is server-defined.
- A zero-attachment resource returns 200 empty; 404 means the parent resource is missing.
- Test record attachments require an explicit `fields[testrecord_attachments]` — the default GET ships no `attributes` block at all.
- Document `@basic` ships no relationships.
- Work item `title` is settable at POST and served on explicit fields.
- An unseeded document or the wrong space returns 404.
- A bad run, test case, or iteration returns 404 `"Test Record ... was not found"`.
- The test record `revision` query param addresses the record revision, not the attachment.
- In a body the ref token is URL-encoded against the raw `attributes.id`.
- Non-image attachments are embeddable via `img`.

## Write contract

POST is multipart on all three, with one shared shape:

- `resource` is a plain form field holding a JSON string — a part with an explicit `application/json` content-type returns 500.
- File parts are all named `files`, order-matched to `data[]`. `lid` works only as a part *name*, not as a field.
- `attributes.fileName` overrides the multipart filename.
- 201 `data` is a list in input order; entries carry `type`/`id`/`links` only.

Server-side id rewriting differs per resource:

| Aspect | Document | Work item | Test record |
|---|---|---|---|
| Duplicate `fileName` | 409, whole batch atomic | never 409 | 409, whole batch atomic — in-batch **and** against existing |
| Stored id | `attributes.fileName` verbatim | `{counter}-{fileName}` (`3-a.txt`) | `{testCaseId}_{fileName}` |
| Served `fileName` | as uploaded | as uploaded | rewritten to the prefixed token |
| DELETE | 405 | 204 | 204 |

- The two 204 DELETEs are API capability only: the repo ships no attachment delete tool by policy.
- The work item counter is monotonic per work item and is not reset by delete, so `workitemimg:{id}` is unpredictable before upload — read it from the 201 echo.
- Test record 201 echo ids never match the uploaded names.
- A nonexistent test record coordinate returns 404 and creates no ghost.
- Record attachment POST and DELETE are not blocked by the manual-run e-signature — see [test-runs.md](test-runs.md) for its scope.

## Server does not validate

This resource has no unvalidated surface of its own: a body ref is validated by the resource whose body holds it, and each guard is named in the doc linked below.

## Cross-domain

- Ghost `attachment:` refs in a document body: [documents.md](documents.md).
- Ghost `workitemimg:` refs in a work item description: [work-items.md](work-items.md).
- Ghost refs in comment bodies, and the document-comment `meta.totalCount` rule: [comments.md](comments.md).
- E-signature scope and record iteration numbering: [test-runs.md](test-runs.md).

## Verified

| Date | Scope |
|---|---|
| 2026-07-18 | Document attachment + document comment `meta.totalCount`, unseeded-document 404 |
| 2026-07-20 | Document attachment POST multipart contract |
| 2026-07-21 | Work item and test record attachment POST, e-signature scope |
