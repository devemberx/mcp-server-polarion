# Test runs and test records

Test run and test record contracts, including the e-signature block. Read before touching `list_test_runs`, `get_test_run`, `create_test_runs`, `update_test_runs`, `list_test_records`, `get_test_record`, `create_test_records`, or `update_test_records`. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET, POST, PATCH | `projects/{p}/testruns` | POST requires an explicit `id` |
| GET, PATCH | `testruns/{r}/testrecords` | PATCH batches are atomic |
| GET | `projects/{p}/enumerations/testing/{enumName}/~` | the `~` context returns 404 for test run enums |

## Read contract

- Test run default GET (no `fields`) ships only `id`, `title`, `status`.
- Test run enums resolve only under the `testing` context — the `~` context returns 404.
- There is no `getAvailableOptions` for test runs.
- `isTemplate` is served only on templates.
- `homePageContent` is served only on explicit request. Under `useReportFromTemplate` the GET serves the linked template's content instead of the run's own.
- Test record default GET ships attrs `result` and `iteration` plus the `testCase` relationship.
- The `defect` relationship is absent from that default — serialize it only when named in `fields[testrecords]` or pulled via `include=defect` into `included`.
- The first record iteration is `0`.

## Write contract

- Test run POST requires an explicit `id`; without one the server returns 400. The UI autofill is UI-only.
- `isTemplate` is settable at POST via `attributes.isTemplate`.
- `homePageContent` is settable at POST and PATCH.
- Test record PATCH batches are atomic — one bad id fails the whole batch with 400, and `"was not found"` arrives as 400, not 404.
- Partial PATCH is safe: omitted attributes are preserved.
- REST auto-fills nothing. The server never populates `executed`, `duration`, or `testCaseRevision`; all three are settable explicitly via PATCH (204, preserved).
- A run's type may require an e-signature. Record PATCH then fails 403 and the remedy is portal-only — REST cannot supply the signature. Surface the Polarion detail in the error; a token hint alone misleads the caller.
- The e-signature block covers record PATCH only.

## Server does not validate

- Test record `result` — a bad value returns 204 and ghosts. Guard: `guard/test_records.py` `guard_test_record_results` against the `testing/test-result` enum.
- Test record `defect` target — a nonexistent work item returns 204 and ghosts. Guard: `guard/test_records.py` `guard_test_record_defect_targets`; the target work item *type* stays unchecked.
- Test run custom-field enum *values* — there is no `getAvailableOptions` for test runs, so values are `unguarded`; keys are guarded.

## Cross-domain

- Record attachments, including why the e-signature block does not reach them: [attachments.md](attachments.md).
- A `text/plain` record comment is stored as `text/html`: [comments.md](comments.md).

## Verified

| Date | Scope |
|---|---|
| 2026-07-21 | E-signature scope on manual-type runs |
