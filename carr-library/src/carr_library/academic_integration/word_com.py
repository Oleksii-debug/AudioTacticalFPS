"""Optional, running-instance-only Word Automation on the existing worker.

COM imports are lazy. Every proxy belongs to the calling STA worker; no Word
process, document, account, macro, external field or save operation is started.
A deadline revokes further calls. A mutating RPC is never detached or declared
cancelled before it returns: Word may continue a call after COM cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import os
import threading
import time
from uuid import uuid4

from .formatting import CitationError
from .word_fields import (MAX_DOCUMENT_CHARS, MAX_FIELDS, MAX_FIELD_BYTES, MAX_TOTAL_BYTES,
                          WordField, WordSnapshot, WordReceipt, WordPlan, FieldChange, code_identity,
                          decode_field, digest, encoded, field_code)

WD_MAIN_TEXT = 1
WD_FIELD_ADDIN = 81
WD_COLLAPSE_START = 1
MAX_NATIVE_FIELDS = 2000
MAX_NATIVE_NOTES = 500


class WordUnavailable(CitationError):
    pass


class WordUpdateIncomplete(CitationError):
    """A reviewed update stopped after its first possible native mutation."""


def _bounded_text(value, maximum, label):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise CitationError(label + " exceeds the supported text limit.")
    return value


def _integer(value, maximum, label, *, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CitationError(label + " is outside the supported native range.")
    return value


class _NativeCancellation:
    """Balanced COM cancellation for read-only calls on this exact thread."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.ole = ctypes.WinDLL("ole32", winmode=0x00000800)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x00000800)
        self.kernel.GetCurrentThreadId.argtypes = ()
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.ole.CoEnableCallCancellation.argtypes = (ctypes.c_void_p,)
        self.ole.CoEnableCallCancellation.restype = ctypes.c_long
        self.ole.CoDisableCallCancellation.argtypes = (ctypes.c_void_p,)
        self.ole.CoDisableCallCancellation.restype = ctypes.c_long
        self.ole.CoCancelCall.argtypes = (wintypes.DWORD, wintypes.ULONG)
        self.ole.CoCancelCall.restype = ctypes.c_long
        self.thread = self.kernel.GetCurrentThreadId()

    @staticmethod
    def _result(value, *, disabling=False):
        code = int(value) & 0xffffffff
        # Microsoft's documented nested-disable result means another balanced
        # enable remains active; it does not invalidate this owner's release.
        if code & 0x80000000 and not (disabling and code == 0x80010140):
            raise OSError("Word COM cancellation HRESULT 0x%08x" % code)

    def enable(self):
        self._result(self.ole.CoEnableCallCancellation(None))

    def disable(self):
        self._result(self.ole.CoDisableCallCancellation(None), disabling=True)

    def request(self):
        # HRESULT is diagnostic; requesting cancellation is not proof that
        # Word stopped. The worker still owns the outstanding call.
        try:
            self.ole.CoCancelCall(self.thread, 0)
        except OSError:
            pass


class _Calls:
    def __init__(self, cancelled, *, timeout=30.0, native=None, manage_native=True):
        if (not callable(cancelled) or type(timeout) not in (int, float)
                or not 0.1 <= timeout <= 60):
            raise ValueError("Word calls need a cancellation callback and a bounded deadline.")
        self.cancelled, self.deadline, self.native = cancelled, time.monotonic() + timeout, native
        self.manage_native = manage_native
        self.done = threading.Event()
        self.guard = threading.Lock()
        self.active = self.mutating = self.expired = False
        self.reader = None

    def check(self):
        if self.cancelled():
            raise InterruptedError("Word cancellation requested; no additional native call was started.")
        if self.expired or time.monotonic() >= self.deadline:
            raise TimeoutError("Word reached its operation deadline; no additional native call was started.")

    def _watch(self):
        while not self.done.wait(.05):
            expired = self.cancelled() or time.monotonic() >= self.deadline
            if not expired:
                continue
            with self.guard:
                self.expired = True
                # Never request cancellation of a write. Microsoft's COM
                # contract permits the server to continue after cancellation.
                # Holding this lock also prevents a late read cancellation
                # request from racing into the following mutating RPC.
                if self.active and not self.mutating and self.native is not None:
                    self.native.request()

    def __enter__(self):
        if self.native is not None and self.manage_native:
            self.native.enable()
        try:
            self.reader = threading.Thread(target=self._watch, name="Word call deadline", daemon=True)
            self.reader.start()
        except BaseException:
            if self.native is not None and self.manage_native:
                self.native.disable()
            raise
        return self

    def call(self, function, *, mutation=False, finishing=False):
        if not finishing:
            self.check()
        with self.guard:
            self.active, self.mutating = True, mutation
        try:
            return function()
        except Exception as exc:
            if not mutation:
                try:
                    self.check()
                except (InterruptedError, TimeoutError) as cancelled:
                    raise cancelled from exc
            raise
        finally:
            with self.guard:
                self.active = self.mutating = False

    def __exit__(self, kind, value, trace):
        self.done.set()
        if self.reader is not None:
            self.reader.join()
            if self.reader.is_alive():
                raise RuntimeError("The exact Word deadline observer has not exited.")
        if self.native is not None and self.manage_native:
            self.native.disable()


class WordComBridge:
    """Retained native identity; create and close it on one existing worker."""

    def __init__(self, *, connect=None, identity=None, native_calls=None):
        # Injection is used by strict modeled object-model tests. Production
        # always selects the installed comtypes running-object attachment.
        self._connect = connect
        self._injected = connect is not None
        self._identity = identity
        self._native_calls = native_calls
        self._thread = None
        self._com = self._app = self._doc = self._doc_identity = None
        self._snapshot = None
        self._fields = {}
        self._busy = self._closed = False
        self._coinit = False
        self._lock = threading.RLock()

    def _same_thread(self):
        if self._thread is not None and self._thread != threading.get_ident():
            raise RuntimeError("Word proxies must be released on their existing STA worker.")

    def _initialize(self):
        self._same_thread()
        if self._closed:
            raise WordUnavailable("This Word bridge is closed.")
        if self._thread is None:
            self._thread = threading.get_ident()
        if self._injected:
            if self._identity is None:
                raise ValueError("A modeled Word connection needs an explicit identity function.")
            return
        if os.name != "nt":
            raise WordUnavailable("Live Word integration is optional and requires desktop Word on Windows.")
        if self._com is None:
            import comtypes
            from comtypes.client import GetActiveObject
            import ctypes
            # No generated Office wrapper or new dependency is required.
            comtypes.CoInitializeEx(2)
            self._coinit = True
            self._com = comtypes
            self._connect = lambda: GetActiveObject("Word.Application", dynamic=True)
            self._identity = lambda value: value._comobj.QueryInterface(comtypes.IUnknown)
            self._native_calls = _NativeCancellation
            self._pointer = lambda value: ctypes.cast(value, ctypes.c_void_p).value

    def _id(self, value):
        result = self._identity(value)
        return self._pointer(result) if hasattr(self, "_pointer") else result

    @contextmanager
    def _operation(self, cancelled, *, timeout=30):
        with self._lock:
            if self._busy:
                raise WordUnavailable("The previous Word call still owns this session.")
            self._busy = True
        try:
            self._initialize()
            native = self._native_calls() if self._native_calls is not None else None
            with _Calls(cancelled, timeout=timeout, native=native) as calls:
                if self._app is None:
                    try:
                        self._app = calls.call(self._connect)
                    except (InterruptedError, TimeoutError):
                        raise
                    except Exception as exc:
                        raise WordUnavailable("Open Word and the intended document yourself, then inspect it again. Word was not launched.") from exc
                yield calls
        finally:
            with self._lock:
                self._busy = False

    def _active(self, calls, *, capture=False):
        doc = calls.call(lambda: self._app.ActiveDocument)
        identity = self._identity(doc)
        key = self._pointer(identity) if hasattr(self, "_pointer") else identity
        if capture:
            self._doc, self._doc_identity = doc, identity
        else:
            original = self._pointer(self._doc_identity) if hasattr(self, "_pointer") else self._doc_identity
            if key != original:
                raise CitationError("Word's active document changed. Capture and review the intended document again.")
        if calls.call(lambda: bool(doc.ReadOnly)):
            raise CitationError("The active Word document is read-only.")
        if calls.call(lambda: int(doc.ProtectionType)) != -1:
            raise CitationError("The active Word document is protected; its protection was not changed.")
        if calls.call(lambda: bool(doc.TrackRevisions)):
            raise CitationError("Review this operation in a document without active Track Changes; Word's setting was not changed.")
        return doc

    def _read(self, calls, *, capture=False):
        doc = self._active(calls, capture=capture)
        name = _bounded_text(calls.call(lambda: str(doc.FullName)), 4096, "The Word document name")
        window = _integer(calls.call(lambda: int(self._app.ActiveWindow.Hwnd)), 2**64-1,
                          "The active Word window", minimum=-(2**31))
        if window == 0:
            raise CitationError("Word did not expose an active document window.")
        selection = calls.call(lambda: self._app.Selection.Range.Duplicate)
        if self._id(calls.call(lambda: selection.Document)) != self._id(doc):
            raise CitationError("Word's selection belongs to a different document.")
        if calls.call(lambda: int(selection.StoryType)) != WD_MAIN_TEXT:
            raise CitationError("Place the insertion cursor in the main Word document body.")
        content = calls.call(lambda: doc.Content)
        content_start = _integer(calls.call(lambda: int(content.Start)), 2**31-1, "Document start")
        content_end = _integer(calls.call(lambda: int(content.End)), 2**31-1, "Document end")
        if content_end - content_start > MAX_DOCUMENT_CHARS:
            raise CitationError("The open Word document exceeds the two-million-character allowance.")
        selected = (_integer(calls.call(lambda: int(selection.Start)), content_end, "Selection start"),
                    _integer(calls.call(lambda: int(selection.End)), content_end, "Selection end"))
        if not content_start <= selected[0] <= selected[1] <= content_end:
            raise CitationError("Word's selection is outside the captured document.")
        full_text = _bounded_text(calls.call(lambda: content.Text), MAX_DOCUMENT_CHARS, "Word document text")
        fields, objects, excluded, all_ranges, note_rows, extra_notes = [], {}, [], [], [], []
        total_metadata = 0
        native_count = 0
        story_characters = content_end - content_start

        def scan(container, *, placement, position=None, note_index=None):
            nonlocal total_metadata, native_count
            collection = calls.call(lambda: container.Fields)
            count = _integer(calls.call(lambda: int(collection.Count)), MAX_NATIVE_FIELDS, "Native field count")
            native_count += count
            if native_count > MAX_NATIVE_FIELDS:
                raise CitationError("The Word document exceeds its total field allowance.")
            owned_ranges = []
            for number in range(1, count + 1):
                calls.check()
                native = calls.call(lambda number=number: collection.Item(number))
                code_range = calls.call(lambda: native.Code)
                result_range = calls.call(lambda: native.Result)
                begin = _integer(calls.call(lambda: int(code_range.Start)) - 1, 2**31-1, "Field start")
                end = _integer(calls.call(lambda: int(result_range.End)) + 1, 2**31-1, "Field end")
                if end <= begin:
                    raise CitationError("Word returned an invalid field range.")
                if placement == "inline":
                    all_ranges.append((begin, end))
                code_end = _integer(calls.call(lambda: int(code_range.End)), 2**31-1, "Field code end")
                if not begin + 1 <= code_end <= end or code_end - (begin + 1) > 4096:
                    raise CitationError("The Word field code exceeds its bounded allowance.")
                code = _bounded_text(calls.call(lambda: code_range.Text), 4096, "Word field code")
                owned = code_identity(code)
                if owned is None:
                    continue
                if calls.call(lambda: int(native.Type)) != WD_FIELD_ADDIN:
                    raise CitationError("A CARR marker is attached to an unsupported Word field type.")
                kind, identity = owned
                if identity in objects:
                    raise CitationError("The document contains duplicate CARR field IDs.")
                data = _bounded_text(calls.call(lambda: native.Data), MAX_FIELD_BYTES, "Word field metadata")
                total_metadata += len(data.encode("utf-8"))
                if total_metadata > MAX_TOTAL_BYTES:
                    raise CitationError("The Word metadata exceeds its aggregate byte allowance.")
                text = _bounded_text(calls.call(lambda: result_range.Text), MAX_DOCUMENT_CHARS, "Word field result")
                if calls.call(lambda: int(result_range.Fields.Count)):
                    raise CitationError("Nested fields inside a CARR result cannot be replaced.")
                row = WordField(identity, kind, placement,
                    begin if position is None else position, note_index, code, data, text)
                decode_field(row)
                objects[identity] = native
                fields.append(row)
                owned_ranges.append((begin, end))
            return owned_ranges

        excluded.extend(scan(content, placement="inline"))
        notes = calls.call(lambda: doc.Endnotes)
        note_count = _integer(calls.call(lambda: int(notes.Count)), MAX_NATIVE_NOTES, "Native endnote count")
        numbering = (calls.call(lambda: int(notes.NumberingRule)) == 0
                     and calls.call(lambda: int(notes.StartingNumber)) == 1
                     and calls.call(lambda: int(notes.NumberStyle)) == 0)
        for number in range(1, note_count + 1):
            note = calls.call(lambda number=number: notes.Item(number))
            reference = calls.call(lambda: note.Reference)
            begin = _integer(calls.call(lambda: int(reference.Start)), content_end, "Endnote reference")
            end = _integer(calls.call(lambda: int(reference.End)), content_end, "Endnote reference end")
            note_index = _integer(calls.call(lambda: int(note.Index)), MAX_NATIVE_NOTES, "Endnote index", minimum=1)
            note_rows.append((begin, note_index))
            body = calls.call(lambda: note.Range)
            body_start = _integer(calls.call(lambda: int(body.Start)), 2**31-1, "Endnote story start")
            body_end = _integer(calls.call(lambda: int(body.End)), 2**31-1, "Endnote story end")
            if body_end < body_start:
                raise CitationError("The endnote story has an invalid native range.")
            story_characters += body_end - body_start
            if story_characters > MAX_DOCUMENT_CHARS:
                raise CitationError("The Word body and endnotes exceed their aggregate text allowance.")
            before_ids = set(objects)
            ranges = scan(body, placement="endnote", position=begin, note_index=note_index)
            owned_ids = set(objects) - before_ids
            if len(owned_ids) > 1:
                raise CitationError("A native endnote may contain only one CARR citation field.")
            if ranges:
                identity = next(iter(owned_ids))
                if objects[identity] is None:
                    raise CitationError("The captured note field is unavailable.")
                excluded.append((begin, end))
                outside = self._outside(calls, body, ranges)
                if outside not in ("", "\r"):
                    extra_notes.append(("owned", identity, outside))
            else:
                extra_notes.append(("foreign", _bounded_text(
                    calls.call(lambda: body.Text), MAX_DOCUMENT_CHARS, "Foreign endnote text")))
        if len(fields) > MAX_FIELDS:
            raise CitationError("The CARR field allowance is five hundred per document.")
        unmanaged = self._outside(calls, content, excluded)
        unmanaged_parts = (unmanaged,
            [row[1] for row in extra_notes if row[0] == "foreign"],
            sorted((row[1], row[2]) for row in extra_notes if row[0] == "owned"))
        unmanaged_sha = digest(encoded(unmanaged_parts))
        fields.sort(key=lambda row: (row.position, row.id))
        state = {"name": name, "window": window, "selection": selected,
                 "text": full_text, "fields": [asdict(row) for row in fields],
                 "endnotes": note_rows, "extra_notes": extra_notes, "numbering": numbering,
                 "all_ranges": all_ranges, "unmanaged_sha256": unmanaged_sha,
                 "unmanaged_parts": unmanaged_parts}
        if len(encoded(state).encode("utf-8")) > MAX_TOTAL_BYTES + 4 * MAX_DOCUMENT_CHARS:
            raise CitationError("The Word inspection exceeds its total allowance.")
        self._active(calls)
        return state, objects

    def _outside(self, calls, content, excluded):
        start, end = calls.call(lambda: int(content.Start)), calls.call(lambda: int(content.End))
        fragments, cursor, size = [], start, 0
        for first, last in sorted(excluded):
            if not cursor <= first < last <= end:
                raise CitationError("CARR field ranges overlap or leave their native story.")
            if first > cursor:
                selected = calls.call(lambda: content.Duplicate)
                calls.call(lambda: selected.SetRange(cursor, first))
                text = _bounded_text(calls.call(lambda: selected.Text), MAX_DOCUMENT_CHARS, "Unmanaged Word text")
                fragments.append(text)
                size += len(text)
            cursor = last
        if cursor < end:
            selected = calls.call(lambda: content.Duplicate)
            calls.call(lambda: selected.SetRange(cursor, end))
            text = _bounded_text(calls.call(lambda: selected.Text), MAX_DOCUMENT_CHARS, "Unmanaged Word text")
            fragments.append(text)
            size += len(text)
        if size > MAX_DOCUMENT_CHARS:
            raise CitationError("The unmanaged Word text exceeds its allowance.")
        return "".join(fragments)

    def capture(self, *, cancelled=lambda: False):
        self._snapshot = None
        with self._operation(cancelled) as calls:
            first, objects = self._read(calls, capture=True)
            second, objects = self._read(calls)
            if first != second:
                raise CitationError("Word changed while it was being inspected. Capture it again.")
            self._fields = objects
            self._snapshot = WordSnapshot(uuid4().hex, first["name"], digest(encoded(first)),
                first["unmanaged_sha256"], tuple(first["selection"]),
                tuple(WordField(**row) for row in first["fields"]),
                tuple(tuple(row) for row in first["endnotes"]), first["numbering"])
            return self._snapshot

    def _verify(self, snapshot, calls):
        if snapshot is not self._snapshot:
            raise CitationError("The native Word capture has expired.")
        state, objects = self._read(calls)
        if digest(encoded(state)) != snapshot.document_sha256:
            raise CitationError("Word's document, cursor or fields changed after the preview.")
        self._fields = objects
        return state

    def verify(self, snapshot, *, cancelled=lambda: False):
        with self._operation(cancelled) as calls:
            self._verify(snapshot, calls)

    def apply(self, plan, *, guard, cancelled=lambda: False):
        if not isinstance(plan, WordPlan) or not callable(guard):
            raise CitationError("A typed reviewed Word plan and live source guard are required.")
        snapshot = plan.context.document
        if snapshot is not self._snapshot or not 1 <= len(plan.changes) <= MAX_FIELDS:
            raise CitationError("The exact Word preview or its field allowance is invalid.")
        before = {row.id: row for row in snapshot.fields}
        expected = set(before) | ({plan.insert_id} if plan.insert_id is not None else set())
        if len({row.id for row in plan.changes}) != len(plan.changes) or {row.id for row in plan.changes} != expected:
            raise CitationError("The Word plan cannot delete, duplicate or retarget captured fields.")
        for row in plan.changes:
            if not isinstance(row, FieldChange) or row.before != before.get(row.id):
                raise CitationError("A Word field preimage changed.")
            number = None
            if row.placement == "endnote":
                import json
                try:
                    number = json.loads(row.data)["cluster"]["note_index"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise CitationError("The planned native note metadata is invalid.") from exc
            decode_field(WordField(row.id, row.kind, row.placement, 0, number,
                field_code(row.kind, row.id), row.data, row.text))
            if row.before is not None and (row.before.kind, row.before.placement) != (row.kind, row.placement):
                raise CitationError("An existing Word field cannot change its owned kind or story.")
        if sum(len(row.data.encode("utf-8")) + len(row.text.encode("utf-8")) for row in plan.changes) > MAX_TOTAL_BYTES:
            raise CitationError("The Word update exceeds its total native byte allowance.")
        with self._operation(cancelled, timeout=60) as calls:
            state = self._verify(plan.context.document, calls)
            guard()
            # Scope validation can perform SQLite work; recapture the exact
            # native preimage again before the first possible write.
            state = self._verify(plan.context.document, calls)
            insertion = next((row for row in plan.changes if row.id == plan.insert_id), None)
            if plan.insert_id is not None:
                start, end = state["selection"]
                if start != end or any(first <= start < last for first, last in state["all_ranges"]):
                    raise CitationError("Place a collapsed Word cursor outside existing fields before inserting.")
            self._snapshot = None
            changed = False
            inserted_note_outside = None
            def authorize_write():
                guard()
                self._active(calls)
            try:
                if insertion is not None:
                    guard()
                    selected = calls.call(lambda: self._doc.Range(*state["selection"]))
                    if insertion.placement == "endnote":
                        authorize_write()
                        changed = True
                        note = calls.call(lambda: self._doc.Endnotes.Add(Range=selected, Text=""), mutation=True)
                        if calls.call(lambda: int(note.Index)) != plan.note_index:
                            raise CitationError("Word inserted a different native note index; inspect its new note.")
                        selected = calls.call(lambda: note.Range.Duplicate)
                        inserted_note_outside = _bounded_text(calls.call(lambda: selected.Text),
                            MAX_DOCUMENT_CHARS, "The newly created empty endnote")
                        calls.call(lambda: selected.Collapse(WD_COLLAPSE_START))
                    authorize_write()
                    changed = True
                    native = calls.call(lambda: self._doc.Fields.Add(
                        Range=selected, Type=WD_FIELD_ADDIN,
                        Text=field_code(insertion.kind, insertion.id).removeprefix("ADDIN "),
                        PreserveFormatting=False), mutation=True)
                    self._fields[insertion.id] = native
                # Right-to-left updates keep unprocessed native positions stable.
                ordered = sorted(plan.changes,
                    key=lambda row: row.before.position if row.before is not None else state["selection"][0],
                    reverse=True)
                for row in ordered:
                    guard()
                    self._active(calls)
                    native = self._fields.get(row.id)
                    if native is None:
                        raise CitationError("A reviewed Word field is no longer available.")
                    if row.before is not None:
                        current = (calls.call(lambda: native.Code.Text),
                                   calls.call(lambda: native.Data),
                                   calls.call(lambda: native.Result.Text))
                        if current != (row.before.code, row.before.data, row.before.text):
                            raise CitationError("A Word field changed while applying the reviewed update.")
                    authorize_write()
                    changed = True
                    calls.call(lambda: setattr(native.Result, "Text", row.text), mutation=True)
                    authorize_write()
                    calls.call(lambda: setattr(native, "Data", row.data), mutation=True)
                # The result must be observed before completion. A late Cancel
                # after the last acknowledged write does not erase that receipt.
                with _Calls(lambda: False, timeout=30,
                            native=calls.native, manage_native=False) as verify:
                    after, objects = self._read(verify)
                expected = {row.id: row for row in plan.changes}
                actual = {row["id"]: WordField(**row) for row in after["fields"]}
                expected_unmanaged = state["unmanaged_sha256"]
                if inserted_note_outside not in (None, "", "\r"):
                    original, foreign, owned = state["unmanaged_parts"]
                    expected_unmanaged = digest(encoded((original, foreign,
                        sorted((*owned, (plan.insert_id, inserted_note_outside))))))
                if (set(actual) != set(expected)
                        or after["unmanaged_sha256"] != expected_unmanaged
                        or any(actual[key].text != row.text or actual[key].data != row.data
                               for key, row in expected.items())
                        or any(actual[key].placement == "endnote" and
                               decode_field(actual[key])["cluster"]["note_index"] != actual[key].note_index
                               for key in actual)):
                    raise CitationError("Word's observed result differs from the reviewed fields or unmanaged content.")
                self._fields = objects
                return WordReceipt(plan.token, state["name"], tuple(row.id for row in plan.changes))
            except BaseException as exc:
                if changed:
                    raise WordUpdateIncomplete(
                        "Word may contain a partial update. No automatic replay, save or Undo was performed. "
                        "Inspect the named document and its CARR fields before capturing a new preview. "
                        + str(exc)) from exc
                raise

    def close(self):
        with self._lock:
            if self._busy:
                raise RuntimeError("The actual Word call still owns its worker and proxies; wait for Word to return.")
        self._same_thread()
        if self._closed:
            return True
        self._snapshot = None
        self._fields.clear()
        self._doc_identity = self._doc = self._app = None
        if self._coinit:
            self._com.CoUninitialize()
            self._coinit = False
        self._closed = True
        return True
