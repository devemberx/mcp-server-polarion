# Polarion HTML recipes — raw-HTML edits via update_work_item / update_document

Templates for hand-writing Polarion-native HTML when editing a body fetched
with `get_work_item(include_description_html=True)` /
`get_document(include_home_page_content_html=True)`. Splice new blocks in;
never rewrite existing markup (round-trip bodies are sent verbatim).

Markdown given to `create_work_items` / `create_document` gets tables and
`Table:` captions converted automatically — these recipes are for the
raw-HTML update path only.

## Styled table

A bare `<table>` renders without borders or header shading in the Polarion UI.
Use the house style:

```html
<table class="polarion-Document-table" style="width: 100%;max-width: 1280px;margin-left: 0px;margin-right: auto;border: 1px solid #CCCCCC;empty-cells: show;border-collapse: collapse;">
  <tbody>
    <tr>
      <th style="font-weight: bold;background-color: #F0F0F0;text-align: left;vertical-align: top;border: 1px solid #CCCCCC;padding: 5px;">Header</th>
    </tr>
    <tr>
      <td style="text-align: left;vertical-align: top;border: 1px solid #CCCCCC;padding: 5px;">Cell</td>
    </tr>
  </tbody>
</table>
```

Every `<th>` / `<td>` needs its inline style — Polarion has no stylesheet for
plain cells.

## Numbered caption (Table / Figure)

Place directly after the table (or image). The `#` span is replaced by an
auto-number and feeds the Table of Tables / Table of Figures widgets:

```html
<p class="polarion-rte-caption-paragraph" style="text-align: left;">Table <span data-sequence="Table" class="polarion-rte-caption">#</span> Caption text</p>
```

For figures use `Figure` in both the leading text and `data-sequence`:

```html
<p class="polarion-rte-caption-paragraph" style="text-align: left;">Figure <span data-sequence="Figure" class="polarion-rte-caption">#</span> Caption text</p>
```

## Image from uploaded attachments (update tools only)

Embed an already-uploaded work item attachment as an inline image:

```html
<img src="workitemimg:{id}" style="max-width: 650px;"/>
```

`{id}` is the `id` field from `list_work_item_attachments` (short form, e.g.
`1-diagram.svg` — Polarion may prefix it with a number). Non-image files
embed the same way; the portal renders them with a generic icon.

Non-ASCII or space-containing file names: the stored body token is
URL-encoded (`workitemimg:1-test%20file.txt` for id
`1-test file.txt`) — either the encoded token or the raw id is accepted here.

Polarion never validates this reference; a dangling id would otherwise
persist and render like a real image. This server blocks the write instead
— call list_work_item_attachments first to confirm the id exists.

Document bodies (update_document) embed the same way with the `attachment:`
scheme — `<img src="attachment:{id}"/>`, id from `list_document_attachments`;
every rule above applies unchanged. Schemes never cross: `attachment:` never
resolves in a work item description, `workitemimg:` never resolves in a
document body — use the scheme matching the body you're editing.

Sizing: inline style only — never `width=`/`height=` HTML attributes.
Default embed: `style="max-width: 650px;"`. To resize, mirror the portal's
own resize output: `style="width: 600px;height: 399px;"` — both values in
px, keeping the image's aspect ratio.

## Links to work items, cross references, wiki pages

Rendered links are empty `polarion-rte-link` spans — the target lives in
`data-*` attributes, the text is generated at render time (`data-option-id`:
`short` = id only, `long` = id + title, `default`):

```html
<span class="polarion-rte-link" data-type="workItem" id="fake" data-item-id="MCPT-555" data-option-id="long"></span>
<span class="polarion-rte-link" data-type="crossReference" id="fake" data-item-id="MCPT-555" data-option-id="long"></span>
<span class="polarion-rte-link" data-type="richPage" id="fake" data-item-name="Page Name" data-space-name="Space Name" data-option-id="default"></span>
```

`id="fake"` is the literal stored value — keep it. `crossReference` renders as
a section/outline reference to the target; `workItem` renders as an item link;
`richPage` links a wiki page by space + name. External URLs stay ordinary
`<a href="https://...">` anchors.

## Document-body widgets (update_document only)

Table of Contents, and Table of Tables / Figures (`tof` macro, one per
`data-sequence`):

```html
<div id="polarion_wiki macro name=toc"></div>
<div data-sequence="Table" id="polarion_wiki macro name=tof;params=uid=16"></div>
<div data-sequence="Figure" id="polarion_wiki macro name=tof;params=uid=17"></div>
```

Page break:

```html
<div id="polarion_wiki macro name=page_break;params=uid=23" contentEditable="false" data-is-landscape="false"></div>
```

`uid` must be unique within the document (duplicated ids are rejected with
HTTP 400) — pick values not present in the body. These widgets are
homePageContent-only; they do nothing inside a work item description.

## Macro ids on tables — do NOT add

UI-created tables carry `id="polarion_wiki macro name=table;params=..."`.
Never fabricate one: REST-rendered tables work without it, and a duplicated id
is rejected (HTTP 400). When editing an existing table, keep whatever `id` it
already has.

## Scope — bodies only, never metadata

These templates are for work item `description` and document
`homePageContent`. Rich-text custom fields (`{"type": "text/html", "value":
...}`) are metadata: their values pass to Polarion verbatim, and caption
widgets / rte-links / macro ids must NOT be used there — they only resolve in
a body render context. A plain unstyled table is fine inside a custom field
value.
