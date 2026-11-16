"""Mutable Polarion state + write semantics, no HTTP. One ``World`` per
``FakePolarion`` (= per eval run), deep-copied from seeds so writes never
leak across runs. Ops raise ``Fault``; routes render it as JSON:API error.

Semantics mirror live testdrive (v2506, probed 2026-11-15) unless comment
say "not live-verified".
"""

from __future__ import annotations

import copy
import html
from collections import Counter
from dataclasses import dataclass, field, replace

from ..fixtures import PROJECT, SPACE, TESTCASE_ID, TS, Attachment, Comment, Seeds
from . import body
from .body import Block

Json = dict[str, object]


class Fault(Exception):
    """Polarion error answer: HTTP ``status`` + ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Link:
    role: str
    target: str  # short id; world single-project
    suspect: bool = False
    revision: str = ""


@dataclass
class WorkItemState:
    id: str
    attributes: Json
    # Full module id = document membership. Set while absent from body =
    # orphan (live: PATCH dropping placeholder keep module).
    module: str = ""
    assignees: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)  # stored only; see structure_link
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class DocumentState:
    space: str
    name: str
    attributes: Json
    blocks: list[Block] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def module_id(self) -> str:
        return f"{PROJECT}/{self.space}/{self.name}"

    def index_of(self, part_ref: str) -> int:
        """Block index of full part id (``{module_id}/{part_id}``)."""
        prefix = f"{self.module_id}/"
        if part_ref.startswith(prefix):
            part_id = part_ref[len(prefix) :]
            for i, block in enumerate(self.blocks):
                if block.part_id == part_id:
                    return i
        # Not live-verified: status + wording of unknown anchor part.
        raise Fault(400, f"Document part '{part_ref}' was not found.")


@dataclass
class TestRecord:
    test_case: str  # short id
    iteration: int
    attributes: Json = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class TestRunState:
    id: str
    attributes: Json
    records: list[TestRecord] = field(default_factory=list)

    @property
    def is_template(self) -> bool:
        return bool(self.attributes.get("isTemplate"))


def record_id(run_id: str, record: TestRecord) -> str:
    """Server-composed 5-segment id: run / test case project + id / iteration."""
    return f"{PROJECT}/{run_id}/{PROJECT}/{record.test_case}/{record.iteration}"


def work_item_defaults() -> Json:
    """Attributes every work item serve until written."""
    return {
        "status": "open",
        "priority": "50.0",
        "severity": "should_have",
        "resolution": "",
        "outlineNumber": "",
        "created": TS,
        "updated": TS,
        "description": {"type": "text/html", "value": ""},
        "hyperlinks": [],
    }


# Live copy keep these; title dropped, status reset to draft.
_COPIED_DOCUMENT_ATTRIBUTES = ("type", "structureLinkRole", "usesOutlineNumbering")


@dataclass
class World:
    work_items: dict[str, WorkItemState]
    documents: dict[tuple[str, str], DocumentState]
    test_runs: dict[str, TestRunState]
    enums: dict[tuple[str, str], list[tuple[str, bool]]]
    project_enums: dict[str, list[str]]
    # Server-assigned ids. Live work item ids = project-global sequence;
    # 9001+ keep clear of seed ids.
    next_work_item: int = 9001
    next_comment: int = 100

    @classmethod
    def from_seeds(cls, seeds: Seeds) -> World:
        seeds = copy.deepcopy(seeds)
        work_items = {
            wi.short_id: WorkItemState(
                wi.short_id,
                {
                    **work_item_defaults(),
                    "title": wi.title,
                    "type": wi.type,
                    "status": wi.status,
                    "priority": wi.priority,
                    "severity": wi.severity,
                    "outlineNumber": wi.outline_number,
                    "hyperlinks": wi.hyperlinks,
                    **wi.custom_fields,
                },
                module=wi.module_id,
                links=[
                    Link(role, target)
                    for role, target in seeds.links.get(wi.short_id, [])
                ],
                comments=wi.comments,
                attachments=wi.attachments,
            )
            for wi in seeds.work_items.values()
        }
        world = cls(work_items, {}, {}, seeds.enums, seeds.project_enums)
        for doc in seeds.documents.values():
            state = DocumentState(
                SPACE,
                doc.name,
                {
                    "title": doc.title,
                    "type": doc.type,
                    "status": doc.status,
                    "structureLinkRole": doc.structure_link_role,
                },
                body.parse(doc.body_html),
                doc.comments,
                doc.attachments,
            )
            for block in state.blocks:
                if block.kind == "normal":
                    continue
                member = work_items.get(block.ref)
                if member is None or member.module != state.module_id:
                    msg = f"seed {doc.name}: placeholder {block.ref!r} is no member"
                    raise ValueError(msg)
            world.documents[(SPACE, doc.name)] = state
        for tr in seeds.test_runs.values():
            run = TestRunState(
                tr.short_id,
                {
                    "title": tr.title,
                    "type": tr.type,
                    "status": tr.status,
                    "finishedOn": tr.finished_on,
                    "updated": TS,
                    "isTemplate": tr.is_template,
                },
            )
            # Templates never executed. Instances: failed TESTCASE_ID record per
            # iteration, seed attachments on iteration 0 only.
            if not tr.is_template:
                run.records = [
                    TestRecord(
                        TESTCASE_ID,
                        i,
                        {
                            "executed": TS,
                            "duration": 1.5,
                            "result": "failed",
                            "comment": {
                                "type": "text/html",
                                "value": "<p>Fake execution comment.</p>",
                            },
                            "testCaseRevision": "3",
                        },
                        tr.record_attachments if i == 0 else [],
                    )
                    for i in range(tr.iterations)
                ]
            world.test_runs[tr.short_id] = run
        return world

    def work_item(self, short_id: str) -> WorkItemState:
        wi = self.work_items.get(short_id)
        if wi is None:
            raise Fault(404, f"Work Item '{PROJECT}/{short_id}' was not found.")
        return wi

    def document(self, space: str, name: str) -> DocumentState:
        doc = self.documents.get((space, name))
        if doc is None:
            raise Fault(404, f"Document '{PROJECT}/{space}/{name}' was not found.")
        return doc

    def document_by_module(self, module_id: str) -> DocumentState:
        return self.document(*_space_name(module_id))

    def find_document(self, module_id: str) -> DocumentState | None:
        return self.documents.get(_space_name(module_id))

    def test_run(self, run_id: str) -> TestRunState:
        run = self.test_runs.get(run_id)
        if run is None:
            raise Fault(404, f"Test Run '{PROJECT}/{run_id}' was not found.")
        return run

    def record(
        self, run_id: str, case_project: str, case_id: str, iteration: str
    ) -> TestRecord:
        run = self.test_runs.get(run_id)
        records = run.records if run is not None and case_project == PROJECT else []
        for rec in records:
            if rec.test_case == case_id and str(rec.iteration) == iteration:
                return rec
        ref = f"{PROJECT}/{run_id}/{case_project}/{case_id}/{iteration}"
        raise Fault(404, f"Test Record '{ref}' was not found.")

    def _new_work_item(self, attributes: Json, module: str = "") -> WorkItemState:
        wi = WorkItemState(
            f"MCPT-{self.next_work_item}",
            {**work_item_defaults(), **attributes},
            module=module,
        )
        self.next_work_item += 1
        self.work_items[wi.id] = wi
        return wi

    def create_work_items(self, entries: list[Json]) -> list[str]:
        created = []
        for entry in entries:
            wi = self._new_work_item(_dict(entry.get("attributes")))
            wi.assignees = _assignee_ids(entry)
            created.append(wi.id)
        return created

    def update_work_items(self, entries: list[Json], change_type_to: str = "") -> None:
        """Atomic (live): unknown id fail batch before any write."""
        targets = [
            (self.work_item(_short(entry.get("id"))), entry) for entry in entries
        ]
        for wi, entry in targets:
            wi.attributes.update(_dict(entry.get("attributes")))
            if "assignee" in _dict(entry.get("relationships")):
                wi.assignees = _assignee_ids(entry)
            if change_type_to:
                wi.attributes["type"] = change_type_to

    def move_to_document(
        self, short_id: str, target: str, previous: str = "", next_: str = ""
    ) -> None:
        """No anchor = append (live). Any membership, orphan included, = 409."""
        wi = self.work_item(short_id)
        doc = self.document_by_module(target)
        if wi.module:
            raise Fault(409, "Work Item already in Document.")
        if previous:
            position = doc.index_of(previous) + 1
        elif next_:
            position = doc.index_of(next_)
        else:
            position = len(doc.blocks)
        # Not live-verified: heading-type item land as level-1 heading.
        is_heading = wi.attributes.get("type") == "heading"
        block = (
            Block("heading", wi.id, level=1) if is_heading else Block("workitem", wi.id)
        )
        doc.blocks.insert(position, block)
        wi.module = doc.module_id

    def move_from_document(self, short_id: str) -> None:
        wi = self.work_item(short_id)
        if not wi.module:
            raise Fault(400, "Work Item is not in Document.")
        doc = self.document_by_module(wi.module)
        doc.blocks = [b for b in doc.blocks if b.kind == "normal" or b.ref != wi.id]
        wi.module = ""

    def structure_link(self, wi: WorkItemState) -> Link | None:
        """Auto link item -> nearest heading above it, role = document
        ``structureLinkRole`` (live: created by move under heading, removed
        when item leave body). Derived from body, so always consistent.
        Heading -> parent heading not live-verified: headings get none.
        """
        doc = self.find_document(wi.module) if wi.module else None
        if doc is None:
            return None
        heading = ""
        for block in doc.blocks:
            if block.kind == "heading":
                heading = block.ref
            elif block.kind == "workitem" and block.ref == wi.id:
                if not heading:
                    return None
                role = str(doc.attributes.get("structureLinkRole", "relates_to"))
                return Link(role, heading)
        return None

    def links_of(self, short_id: str) -> list[Link]:
        wi = self.work_item(short_id)
        auto = self.structure_link(wi)
        if auto is None or any(
            (link.role, link.target) == (auto.role, auto.target) for link in wi.links
        ):
            return list(wi.links)
        return [*wi.links, auto]

    def linking_to(self, target: str) -> list[WorkItemState]:
        """Back-link sources (``query=linkedWorkItems:{target}``)."""
        return [
            wi
            for wi in self.work_items.values()
            if any(link.target == target for link in self.links_of(wi.id))
        ]

    def create_links(self, source_id: str, entries: list[Json]) -> list[str]:
        """Server validate neither role nor target (ghost link) -- guard/ job.
        Not live-verified: same role + target twice stored once.
        """
        wi = self.work_item(source_id)
        ids = []
        for entry in entries:
            attributes = _dict(entry.get("attributes"))
            role = str(attributes.get("role", ""))
            target = _short(_relationship_id(entry, "workItem"))
            if not any((x.role, x.target) == (role, target) for x in wi.links):
                wi.links.append(
                    Link(
                        role,
                        target,
                        bool(attributes.get("suspect", False)),
                        str(attributes.get("revision", "")),
                    )
                )
            ids.append(f"{PROJECT}/{source_id}/{role}/{PROJECT}/{target}")
        return ids

    def delete_links(self, source_id: str, link_ids: list[str]) -> None:
        wi = self.work_item(source_id)
        doomed = set()
        for link_id in link_ids:
            segments = link_id.split("/")
            if len(segments) == 5:
                doomed.add((segments[2], segments[4]))
        wi.links = [x for x in wi.links if (x.role, x.target) not in doomed]

    def update_link(
        self, source_id: str, role: str, target: str, attributes: Json
    ) -> None:
        wi = self.work_item(source_id)
        link = next((x for x in wi.links if (x.role, x.target) == (role, target)), None)
        if link is None:
            link_ref = f"{PROJECT}/{source_id}/{role}/{PROJECT}/{target}"
            raise Fault(404, f"Linked Work Item '{link_ref}' was not found.")
        if "suspect" in attributes:
            link.suspect = bool(attributes["suspect"])
        if "revision" in attributes:
            link.revision = str(attributes["revision"])

    def create_comments(self, thread: list[Comment], entries: list[Json]) -> list[str]:
        """``text/plain`` stored HTML-escaped as ``text/html`` (live). Not
        live-verified: unknown parent = 400.
        """
        known = {c.comment_id for c in thread}
        planned = []
        for entry in entries:
            parent_ref = _relationship_id(entry, "parentComment")
            parent = parent_ref.rsplit("/", 1)[-1] if parent_ref else None
            if parent is not None and parent not in known:
                raise Fault(400, f"Comment '{parent_ref}' was not found.")
            planned.append((entry, parent))
        ids = []
        for entry, parent in planned:
            attributes = _dict(entry.get("attributes"))
            text = _dict(attributes.get("text"))
            value = str(text.get("value", ""))
            if text.get("type") == "text/plain":
                value = html.escape(value)
            comment = Comment(
                str(self.next_comment),
                value,
                bool(attributes.get("resolved", False)),
                parent,
                str(attributes.get("title", "")),
            )
            self.next_comment += 1
            thread.append(comment)
            ids.append(comment.comment_id)
        return ids

    def update_comment(
        self, thread: list[Comment], comment_id: str, attributes: Json
    ) -> None:
        """Only ``resolved`` patchable (live); resolving root resolve thread."""
        target = next((c for c in thread if c.comment_id == comment_id), None)
        if target is None:
            raise Fault(404, f"Comment '{comment_id}' was not found.")
        if "resolved" not in attributes:
            return
        flag = bool(attributes["resolved"])
        pending = [target]
        while pending:
            comment = pending.pop()
            comment.resolved = flag
            pending.extend(c for c in thread if c.parent_id == comment.comment_id)

    def create_document(self, space: str, entry: Json) -> DocumentState:
        attributes = dict(_dict(entry.get("attributes")))
        name = str(attributes.pop("moduleName", "") or "")
        if not name:
            raise Fault(400, "Required member 'moduleName' was not found.")
        if (space, name) in self.documents:
            # Not live-verified: duplicate name status + wording.
            raise Fault(409, "A resource with the same ID already exists.")
        content = _dict(attributes.pop("homePageContent", None))
        # REST-made document default structureLinkRole = relates_to (live).
        doc = DocumentState(
            space,
            name,
            {"status": "draft", "structureLinkRole": "relates_to", **attributes},
        )
        self.set_body(doc, str(content.get("value", "")))
        self.documents[(space, name)] = doc
        return doc

    def update_document(self, doc: DocumentState, attributes: Json) -> None:
        attributes = dict(attributes)
        attributes.pop("moduleName", None)
        content = attributes.pop("homePageContent", None)
        if content is not None:
            self.set_body(doc, str(_dict(content).get("value", "")))
        doc.attributes.update(attributes)

    def set_body(self, doc: DocumentState, markup: str) -> None:
        """Replace body (live): duplicate block id = 400 whole write; plain
        ``<hN>`` spawn heading work item + become placeholder; member dropped
        from body = orphan (module kept). Not live-verified: placeholder of
        non-member dropped, no implicit move.
        """
        blocks = body.parse(markup)
        ids = [b.ref for b in blocks if b.kind == "normal" and b.ref]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise Fault(400, f"Duplicate element id: {', '.join(duplicates)}")
        kept: list[Block] = []
        placed: set[str] = set()
        for block in blocks:
            if block.kind == "normal":
                kept.append(block)
                continue
            if not block.ref:
                title = body.text_of(block.html)
                spawned = self._new_work_item(
                    {"title": title, "type": "heading"}, module=doc.module_id
                )
                kept.append(Block("heading", spawned.id, level=block.level))
                continue
            member = self.work_items.get(block.ref)
            if member is None or member.module != doc.module_id or block.ref in placed:
                continue
            placed.add(block.ref)
            kept.append(Block(block.kind, block.ref, level=block.level))
        doc.blocks = kept

    def copy_document(
        self,
        source: DocumentState,
        target_space: str,
        target_name: str,
        link_role: str = "",
        remove_outgoing_links: bool = False,
    ) -> DocumentState:
        """Live: every member duplicated under new id, placeholders rewritten,
        normal blocks verbatim; ``link_role`` = copy -> original link;
        structure links re-derive among copies. Not live-verified: copies keep
        original's stored outgoing links unless ``remove_outgoing_links``.
        """
        if (target_space, target_name) in self.documents:
            raise Fault(409, "A resource with the same ID already exists.")
        target = DocumentState(
            target_space,
            target_name,
            {
                "status": "draft",
                **{
                    key: source.attributes[key]
                    for key in _COPIED_DOCUMENT_ATTRIBUTES
                    if key in source.attributes
                },
            },
        )
        for block in source.blocks:
            if block.kind == "normal":
                target.blocks.append(replace(block))
                continue
            original = self.work_items[block.ref]
            duplicate = self._new_work_item(
                copy.deepcopy(original.attributes), module=target.module_id
            )
            duplicate.assignees = list(original.assignees)
            if not remove_outgoing_links:
                duplicate.links = [replace(link) for link in original.links]
            if link_role:
                duplicate.links.append(Link(link_role, original.id))
            target.blocks.append(Block(block.kind, duplicate.id, level=block.level))
        self.documents[(target_space, target_name)] = target
        return target

    def add_document_attachments(
        self, doc: DocumentState, uploads: list[Attachment]
    ) -> list[str]:
        """Id = fileName; clash with existing = 409 whole batch (live)."""
        names = {u.attachment_id for u in uploads}
        if names & {a.attachment_id for a in doc.attachments}:
            raise Fault(409, "A resource with the same ID already exists.")
        doc.attachments.extend(uploads)
        return [u.attachment_id for u in uploads]

    def add_work_item_attachments(
        self, wi: WorkItemState, uploads: list[Attachment]
    ) -> list[str]:
        """Server prefix counter (``{n}-{fileName}``), never conflict (live)."""
        base = len(wi.attachments)
        stored = [
            replace(u, attachment_id=f"{base + i + 1}-{u.attachment_id}")
            for i, u in enumerate(uploads)
        ]
        wi.attachments.extend(stored)
        return [a.attachment_id for a in stored]

    def add_record_attachments(
        self, record: TestRecord, uploads: list[Attachment]
    ) -> list[str]:
        """Server rewrite id to ``{testCaseId}_{fileName}``; clash within batch
        or with existing = 409 whole batch (live).
        """
        stored = [
            replace(u, attachment_id=f"{record.test_case}_{u.attachment_id}")
            for u in uploads
        ]
        ids = [a.attachment_id for a in stored]
        if len(set(ids)) != len(ids) or set(ids) & {
            a.attachment_id for a in record.attachments
        }:
            raise Fault(409, "A resource with the same ID already exists.")
        record.attachments.extend(stored)
        return ids

    def create_test_runs(self, entries: list[Json]) -> list[str]:
        """Client id required (live 400 without). Template relationship pass
        its type on. Not live-verified: duplicate id = 409.
        """
        planned: list[TestRunState] = []
        for entry in entries:
            attributes = dict(_dict(entry.get("attributes")))
            run_id = str(attributes.pop("id", "") or "")
            if not run_id:
                raise Fault(400, "Required member 'id' was not found.")
            if run_id in self.test_runs or any(p.id == run_id for p in planned):
                raise Fault(409, "A resource with the same ID already exists.")
            template_id = _short(_relationship_id(entry, "template"))
            if template_id:
                template_type = self.test_run(template_id).attributes.get("type")
                attributes = {"type": template_type, **attributes}
            planned.append(
                TestRunState(
                    run_id,
                    {
                        "status": "open",
                        "updated": TS,
                        "isTemplate": False,
                        **attributes,
                    },
                )
            )
        for run in planned:
            self.test_runs[run.id] = run
        return [run.id for run in planned]

    def update_test_runs(self, entries: list[Json]) -> None:
        targets = [(self.test_run(_short(entry.get("id"))), entry) for entry in entries]
        for run, entry in targets:
            run.attributes.update(_dict(entry.get("attributes")))

    def create_test_records(self, run_id: str, entries: list[Json]) -> list[str]:
        """Repeat test case = next iteration (live). Unknown test case = 400
        whole batch. Live e-signature 403 (manual run + ``result``) is
        instance workflow config -- not modeled.
        """
        run = self.test_run(run_id)
        seen = Counter(rec.test_case for rec in run.records)
        planned = []
        for entry in entries:
            case = _short(_relationship_id(entry, "testCase"))
            if case not in self.work_items:
                raise Fault(
                    400, "Test Case is missing, or the one specified is invalid."
                )
            planned.append(
                TestRecord(case, seen[case], dict(_dict(entry.get("attributes"))))
            )
            seen[case] += 1
        run.records.extend(planned)
        return [record_id(run.id, rec) for rec in planned]

    def update_test_records(self, run_id: str, entries: list[Json]) -> None:
        """Atomic (live): id outside path run = 400 whole batch."""
        run = self.test_run(run_id)
        by_id = {record_id(run.id, rec): rec for rec in run.records}
        targets = []
        for entry in entries:
            rid = str(entry.get("id"))
            if rid not in by_id:
                raise Fault(400, f"Test Record '{rid}' was not found.")
            targets.append((by_id[rid], entry))
        for rec, entry in targets:
            rec.attributes.update(_dict(entry.get("attributes")))


def _dict(value: object) -> Json:
    return value if isinstance(value, dict) else {}


def _short(value: object) -> str:
    """Trailing segment of possibly project-qualified id."""
    return str(value or "").rsplit("/", 1)[-1]


def _space_name(module_id: str) -> tuple[str, str]:
    _, _, rest = module_id.partition("/")
    space, _, name = rest.partition("/")
    return space, name


def _relationship_id(entry: Json, name: str) -> str:
    relationship = _dict(_dict(entry.get("relationships")).get(name))
    return str(_dict(relationship.get("data")).get("id") or "")


def _assignee_ids(entry: Json) -> list[str]:
    data = _dict(_dict(entry.get("relationships")).get("assignee")).get("data")
    items = data if isinstance(data, list) else []
    return [str(_dict(item).get("id")) for item in items if _dict(item).get("id")]
