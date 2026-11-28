"""Reviewed citation updates for CARR-owned Word ADDIN fields.

Uses the existing scoped citation controller and pinned CSL processor. This is
a document adapter, not a second citation engine. Field metadata is provenance,
never a new source-verification or workspace authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from uuid import uuid4

from .formatting import (CitationCluster, CitationError, CitationRequest,
                         _clusters, _identifier, _manifest, _text)

MAX_FIELDS = 500
MAX_DOCUMENT_CHARS = 2_000_000
MAX_FIELD_BYTES = 256_000
MAX_TOTAL_BYTES = 8_000_000
SCHEMA = "carr-word-field/1"
PREFIX = "CARR_CITATION_"
BIB_PREFIX = "CARR_BIBLIOGRAPHY_"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise CitationError("A Word field contains duplicate metadata keys.")
        result[key] = value
    return result


def _check(cancelled):
    if cancelled():
        raise InterruptedError("Word citation operation cancelled before its next change.")


def field_code(kind, identity):
    if kind not in ("citation", "bibliography") or not _HEX32.fullmatch(identity):
        raise CitationError("A Word field needs an exact owned kind and stable ID.")
    return "ADDIN " + (PREFIX if kind == "citation" else BIB_PREFIX) + identity


def code_identity(code):
    if not isinstance(code, str) or len(code) > 4096:
        raise CitationError("The Word field code exceeds its bounded allowance.")
    match = re.fullmatch(r"\s*ADDIN\s+(CARR_CITATION_|CARR_BIBLIOGRAPHY_)([0-9a-f]{32})\s*", code)
    if match:
        return ("citation" if match[1] == PREFIX else "bibliography"), match[2]
    if PREFIX in code or BIB_PREFIX in code:
        raise CitationError("A CARR Word field has an incomplete or unsupported code.")
    return None


def restore_cluster(value, reference_ids):
    """Strict cluster recovery adapted from PR152 regeneration43758cf.

    Round-trip through the current formatter so stored data cannot introduce
    options which the existing citation/locator contract would discard.
    """
    if (not isinstance(value, dict)
            or set(value) != {"id", "items", "note_index", "locator_evidence"}):
        raise CitationError("The Word citation cluster has an unsupported shape.")
    items, evidence = value["items"], value["locator_evidence"]
    if (not isinstance(items, list) or not 1 <= len(items) <= 100
            or not isinstance(evidence, list) or len(items) != len(evidence)):
        raise CitationError("The Word citation item budget or locator evidence is invalid.")
    requests = []
    for item, checked in zip(items, evidence):
        if (not isinstance(item, dict) or "id" not in item
                or set(item) - {"id", "locator", "label", "prefix", "suffix", "suppress-author"}
                or not isinstance(checked, dict)
                or set(checked) != {"reference_id", "locator_verified"}
                or checked["reference_id"] != item["id"]
                or ("locator" in item and checked["locator_verified"] is not True)
                or ("locator" not in item and checked["locator_verified"] is not None)):
            raise CitationError("The saved locator evidence is inconsistent; review its source.")
        requests.append(CitationRequest(
            _identifier(item["id"], "Word reference ID"), item.get("locator"),
            item.get("label", "page"), checked["locator_verified"] is True,
            item.get("prefix", ""), item.get("suffix", ""),
            item.get("suppress-author", False)))
    result = CitationCluster(tuple(requests), value["id"], value["note_index"])
    if _clusters((result,), set(reference_ids)) != [value]:
        raise CitationError("The Word cluster is not in the current normalized citation format.")
    return result


@dataclass(frozen=True)
class WordField:
    id: str
    kind: str
    placement: str
    position: int
    note_index: int | None
    code: str
    data: str = field(repr=False)
    text: str


@dataclass(frozen=True)
class WordSnapshot:
    token: str
    document_name: str
    document_sha256: str
    unmanaged_sha256: str
    selection: tuple[int, int]
    fields: tuple[WordField, ...]
    # Native endnotes: (main-story reference position, current 1-based index).
    endnotes: tuple[tuple[int, int], ...] = ()
    note_numbering_supported: bool = True


@dataclass(frozen=True)
class WordContext:
    bibliography: object = field(repr=False)
    document: WordSnapshot


@dataclass(frozen=True)
class FieldChange:
    id: str
    kind: str
    placement: str
    text: str
    data: str = field(repr=False)
    before: WordField | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WordPlan:
    token: str
    context: WordContext = field(repr=False)
    operation: str
    changes: tuple[FieldChange, ...]
    insert_id: str | None
    note_index: int | None
    preview: str
    source_sha256: str


@dataclass(frozen=True)
class WordReceipt:
    token: str
    document_name: str
    field_ids: tuple[str, ...]
    notice: str = ("The reviewed CARR fields were applied to the open Word document. "
                   "Save the document explicitly in Word. Library records and source bytes were not changed.")


def decode_field(value):
    if not isinstance(value, WordField) or code_identity(value.code) != (value.kind, value.id):
        raise CitationError("The captured Word field identity is inconsistent.")
    if (value.placement not in ("inline", "endnote")
            or type(value.position) is not int or value.position < 0
            or (value.placement == "inline" and value.note_index is not None)
            or (value.placement == "endnote" and
                (type(value.note_index) is not int or not 1 <= value.note_index <= MAX_FIELDS))):
        raise CitationError("The captured Word field placement is invalid.")
    if not isinstance(value.data, str) or len(value.data.encode("utf-8")) > MAX_FIELD_BYTES:
        raise CitationError("The Word field metadata exceeds its byte limit.")
    try:
        record = json.loads(value.data, object_pairs_hook=_pairs,
                            parse_constant=lambda text: (_ for _ in ()).throw(ValueError(text)))
    except (ValueError, RecursionError) as exc:
        raise CitationError("The Word field metadata is not bounded strict JSON.") from exc
    keys = {"schema", "id", "kind", "placement", "workspace", "source_sha256",
            "style", "locale", "processor_sha256", "reference_ids", "cluster", "text_sha256"}
    if not isinstance(record, dict) or set(record) != keys or record["schema"] != SCHEMA:
        raise CitationError("The Word field uses an unsupported metadata schema.")
    if (record["id"] != value.id or record["kind"] != value.kind
            or record["placement"] != value.placement
            or not isinstance(record["workspace"], str) or not record["workspace"]
            or len(record["workspace"]) > 200
            or any(not isinstance(record[key], str) or not _HEX64.fullmatch(record[key])
                   for key in ("source_sha256", "processor_sha256", "text_sha256"))
            or record["text_sha256"] != digest(_text(value.text, "Word field result", MAX_DOCUMENT_CHARS))
            or not isinstance(record["style"], str) or not record["style"] or len(record["style"]) > 200
            or record["locale"] not in ("en-US", "uk-UA", "sk-SK")):
        raise CitationError("The Word field identity, result or provenance changed; inspect it in Word.")
    ids = record["reference_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or any(_identifier(item, "Word reference ID") != item for item in ids)
            or len(set(ids)) != len(ids)):
        raise CitationError("The Word field reference IDs are invalid.")
    if value.kind == "citation":
        cluster = restore_cluster(record["cluster"], ids)
        if cluster.id != value.id:
            raise CitationError("The Word field and stable citation IDs disagree.")
        if {item.reference_id for item in cluster.items} != set(ids):
            raise CitationError("The Word field references do not match its citation.")
        if value.placement == "inline" and cluster.note_index is not None:
            raise CitationError("An inline Word citation contains an unexpected note number.")
        # Word can renumber native endnotes after a user's ordinary edit. The
        # recorded index is historical, while the fresh native index is used
        # only after a new explicit preview.
    elif record["cluster"] is not None or value.placement != "inline":
        raise CitationError("A bibliography must be one owned inline field.")
    return record


class WordCitationService:
    """One preview token, one scoped library and one retained native session."""

    def __init__(self, controller, bridge, *, guard=lambda: None):
        self.controller, self.bridge, self.guard = controller, bridge, guard
        self._context = self._plan = None
        self._closed = False

    def _live(self, cancelled):
        if self._closed:
            raise CitationError("The Word citation session is closed.")
        _check(cancelled)
        self.guard()

    def capture(self, *, cancelled=lambda: False):
        self._live(cancelled)
        self._context = self._plan = None
        bibliography = self.controller.load(cancelled=cancelled)
        document = self.bridge.capture(cancelled=cancelled)
        self.controller._check(bibliography, cancelled)
        self._live(cancelled)
        if not isinstance(document, WordSnapshot) or len(document.fields) > MAX_FIELDS:
            raise CitationError("The Word document exceeds its owned-field allowance.")
        if len({row.id for row in document.fields}) != len(document.fields):
            raise CitationError("Duplicate CARR Word field IDs must be repaired before updating.")
        if sum(len(row.data.encode("utf-8")) for row in document.fields) > MAX_TOTAL_BYTES:
            raise CitationError("The Word field metadata exceeds its total allowance.")
        for row in document.fields:
            decode_field(row)
        self._context = WordContext(bibliography, document)
        return self._context

    def preview(self, context, *, operation, reference_ids=(), target_id=None,
                locator=None, label="page", locator_verified=False,
                review_existing_locators=False, style="apa", locale="en-US", cancelled=lambda: False):
        self._live(cancelled)
        self._plan = None
        if context is not self._context:
            raise CitationError("Reload the exact Word document and library before previewing.")
        if operation not in ("insert", "edit", "bibliography", "refresh"):
            raise CitationError("Choose Insert citation, Edit citation, Bibliography or Refresh.")
        self.controller._check(context.bibliography, cancelled)
        self.bridge.verify(context.document, cancelled=cancelled)
        old = {row.id: row for row in context.document.fields}
        records = {identity: decode_field(row) for identity, row in old.items()}
        if any(row["workspace"] != self.controller.workspace for row in records.values()):
            raise CitationError("This Word document contains CARR fields from another library.")
        processor = digest(encoded(_manifest()["processor"]))
        if any(row["processor_sha256"] != processor for row in records.values()):
            raise CitationError("The saved processor identity changed. Review the fields before migrating them.")
        references = {row["id"]: row for row in context.bibliography.references}
        if not isinstance(reference_ids, (tuple, list)) or len(reference_ids) > 1000:
            raise CitationError("Select a bounded reference list.")
        wanted = tuple(reference_ids)
        if len(set(wanted)) != len(wanted) or any(item not in references for item in wanted):
            raise CitationError("A selected reference is unavailable in the active library.")
        if target_id is not None and target_id not in old:
            raise CitationError("Select an existing CARR citation in this document.")
        if operation == "edit" and (target_id is None or old[target_id].kind != "citation"):
            raise CitationError("Choose one existing citation to edit.")
        if operation != "edit" and target_id is not None:
            raise CitationError("A target ID is only valid for Edit citation.")
        if operation in ("insert", "edit") and not wanted:
            raise CitationError("Select at least one academic reference.")
        if locator is not None:
            locator = _text(locator, "Verified citation locator", 2000)
            if not locator.strip() or len(wanted) != 1:
                raise CitationError("A pinpoint needs one selected reference and a nonblank locator.")
        if type(locator_verified) is not bool or type(review_existing_locators) is not bool:
            raise CitationError("The source-review confirmations must be booleans.")
        if operation not in ("insert", "edit") and (locator is not None or locator_verified):
            raise CitationError("Locator changes require an explicit citation edit.")
        insert_id = uuid4().hex if operation in ("insert", "bibliography") else None
        citations, placements = {}, {}
        bibliography_id = None
        bibliography_ids = ()
        for identity, value in records.items():
            row = old[identity]
            if row.kind == "bibliography":
                if bibliography_id is not None:
                    raise CitationError("Only one CARR bibliography field is supported per document.")
                bibliography_id, bibliography_ids = identity, tuple(value["reference_ids"])
                continue
            cluster = restore_cluster(value["cluster"], set(references))
            if (value["source_sha256"] != context.bibliography.sha256
                    and any(item.locator is not None for item in cluster.items)
                    and not (operation == "edit" and identity == target_id)):
                raise CitationError("A cited source changed. Explicitly review and edit its locators before refreshing Word.")
            if (any(item.locator is not None for item in cluster.items)
                    and not (operation == "edit" and identity == target_id)
                    and not review_existing_locators):
                raise CitationError("Stored Word locator flags are historical only. Explicitly review retained locators against the current sources.")
            citations[identity], placements[identity] = cluster, row.placement
        if operation == "bibliography":
            if bibliography_id is not None:
                raise CitationError("The CARR bibliography already exists; use Refresh to regenerate it.")
            if not wanted:
                raise CitationError("Select references for the new bibliography.")
            bibliography_id, bibliography_ids = insert_id, wanted
        if operation in ("insert", "edit"):
            identity = insert_id if operation == "insert" else target_id
            requests = tuple(CitationRequest(item, locator, label, locator_verified) for item in wanted)
            # Existing formatter enforces locator checks before any Word call.
            citations[identity] = CitationCluster(requests, identity)
        if not citations and bibliography_id is None:
            raise CitationError("There are no CARR citations or bibliography fields to regenerate.")
        from .formatting import available_styles
        choices = {item.id: item for item in available_styles()}
        if style not in choices:
            raise CitationError("Choose one installed, pinned CSL style.")
        note = choices[style].kind == "note"
        if note and citations and not context.document.note_numbering_supported:
            raise CitationError("Live note styles need continuous Arabic endnotes starting at 1; Word's policy was not changed.")
        if operation == "insert":
            placements[insert_id] = "endnote" if note else "inline"
        if any((placement == "endnote") != note for placement in placements.values()):
            raise CitationError("Changing between inline and native note placement requires an explicit new citation, not a style-only refresh.")
        start, end = context.document.selection
        if insert_id is not None and start != end:
            raise CitationError("Place a collapsed cursor in Word; existing selected prose is never replaced.")
        position = {identity: row.position for identity, row in old.items() if row.kind == "citation"}
        if operation == "insert":
            position[insert_id] = start
        ordered = sorted(citations, key=lambda key: (position[key], 0 if key == insert_id else 1))
        new_note_index = None
        if operation == "insert" and note:
            new_note_index = 1 + sum(at < start for at, index in context.document.endnotes)
        clusters = []
        for identity in ordered:
            cluster = citations[identity]
            number = None
            if note:
                if identity == insert_id:
                    number = new_note_index
                else:
                    row = old[identity]
                    number = row.note_index
                    if new_note_index is not None and row.position >= start:
                        number += 1
            clusters.append(CitationCluster(cluster.items, identity, number))
        selected = set(bibliography_ids)
        selected.update(item.reference_id for cluster in clusters for item in cluster.items)
        if not selected <= references.keys():
            raise CitationError("A saved Word reference no longer exists in this library.")
        result = self.controller.render(context.bibliography,
            reference_ids=tuple(row["id"] for row in context.bibliography.references if row["id"] in selected),
            style=style, locale=locale, citations=tuple(clusters), cancelled=cancelled)
        normalized = {value["id"]: value for value in result.interchange["clusters"]}
        changes = []
        def append(identity, kind, placement, text, ids, cluster):
            record = {"schema": SCHEMA, "id": identity, "kind": kind, "placement": placement,
                "workspace": self.controller.workspace, "source_sha256": context.bibliography.sha256,
                "style": style, "locale": locale, "processor_sha256": processor,
                "reference_ids": list(ids), "cluster": cluster, "text_sha256": digest(text)}
            text = text.replace("\r\n", "\r").replace("\n", "\r")
            record["text_sha256"] = digest(text)
            data = encoded(record)
            if len(data.encode("utf-8")) > MAX_FIELD_BYTES:
                raise CitationError("The prepared Word field exceeds its metadata allowance.")
            changes.append(FieldChange(identity, kind, placement, text, data, old.get(identity)))
        for citation in result.citations:
            append(citation.cluster_id, "citation", placements[citation.cluster_id],
                   citation.text, citation.reference_ids, normalized[citation.cluster_id])
        if bibliography_id is not None:
            append(bibliography_id, "bibliography", "inline",
                   "\r\r".join(entry.text for entry in result.bibliography),
                   tuple(row["id"] for row in result.references), None)
        if (len(changes) > MAX_FIELDS or sum(len(row.data.encode("utf-8")) for row in changes) > MAX_TOTAL_BYTES
                or sum(len(row.text) for row in changes) > MAX_DOCUMENT_CHARS):
            raise CitationError("The Word update exceeds its aggregate field or text allowance.")
        self.bridge.verify(context.document, cancelled=cancelled)
        self.controller._check(context.bibliography, cancelled)
        self._live(cancelled)
        preview = ("Open Word document: " + context.document.document_name
            + "\nAction: " + operation + "\nStyle: " + result.style.title
            + "\nOnly the listed CARR fields will change. Other fields and prose remain outside this operation."
            + "\nNo document is saved automatically. Locator checks are explicit caller source reviews.\n\n"
            + "\n\n".join(("Bibliography" if row.kind == "bibliography" else
                ("Native endnote" if row.placement == "endnote" else "Citation"))
                + " [" + row.id + "]\n" + row.text for row in changes))
        self._plan = WordPlan(uuid4().hex, context, operation, tuple(changes),
                              insert_id, new_note_index, preview, context.bibliography.sha256)
        return self._plan

    def apply(self, plan, *, cancelled=lambda: False):
        self._live(cancelled)
        if plan is not self._plan or plan.context is not self._context:
            raise CitationError("This Word preview expired or was already consumed.")
        self.controller._check(plan.context.bibliography, cancelled)
        self.bridge.verify(plan.context.document, cancelled=cancelled)
        self._live(cancelled)
        # Consumption precedes the first possible remote mutation. An uncertain
        # response must never make this same operation eligible for replay.
        self._plan = self._context = None
        def guard():
            self._live(cancelled)
            self.controller._check(plan.context.bibliography, cancelled)
        result = self.bridge.apply(plan, guard=guard, cancelled=cancelled)
        if not isinstance(result, WordReceipt) or result.token != plan.token:
            raise CitationError("Word did not return this operation's verified receipt.")
        # A complete verified remote outcome survives a late Cancel.
        return result

    def close(self):
        self._closed = True
        self._context = self._plan = None
        return self.bridge.close()
