# <Domain>

> Copy this file to `<domain>.md` and delete this quote block.
>
> **Section order is fixed. Drop a section only when it has no facts — never reorder, never invent a new heading.**
>
> Writing rules:
> - One fact per line. A line carrying two facts gets split.
> - Identifiers, paths, params, and enum values in backticks, spelled exactly as the API spells them.
> - HTTP status codes as bare numbers (`400`, not `HTTP 400`). Quote the server's own message verbatim when the text is the diagnostic.
> - Say what the server does, not what our code does. Code behavior belongs in the module it lives in.
> - No PR or issue numbers, no narrative of how the fact was found, no private deployment names.
> - Every entry under *Server does not validate* names the guard that covers it, or says `unguarded`.
> - One fact, one owner. A ghost body ref belongs to the resource whose body holds it, not to the resource being referenced. Anything a sibling doc already states gets a *Cross-domain* link, never a restatement.
> - Dates live only in *Verified* — never inline in a fact line.

<One line: what this file covers.> Read before touching <tool names>. Baseline Polarion REST API v2506.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| | | |

## Read contract

<Default GET shape, `@basic`, mandatory `fields[]`, `include`, `sort`, `meta.totalCount`, empty-vs-404.>

## Write contract

<Body shape, batch atomicity, server-side rewrites, status codes, reversibility.>

## Server does not validate

<Every surface that accepts bad input and persists it — the ghost list. Each line names its guard.>

## Cross-domain

<Links to sibling docs for rules that live there.>

## Verified

| Date | Scope |
|---|---|
| | |
