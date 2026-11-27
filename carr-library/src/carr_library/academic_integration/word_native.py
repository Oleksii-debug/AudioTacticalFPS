"""Supported Word COM operations on one already-open, held document.

Only the standalone owned worker constructs this adapter. No call opens, saves,
closes, quits, activates, or changes security settings of the user's Word.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from hashlib import sha256
import os

from .word_records import (
    NAMESPACE, LEGACY_NAMESPACE, MAX_TEXT, MAX_BYTES, MAX_CONTROLS,
    TAG_CITATION, owned_tag, raw_text, integer, text, require, utf16_length,
    metadata_xml, parse_metadata_xml, canonical, fingerprint, validate_capture, validate_proposal,
)


def _sha(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _count(collection, maximum, label):
    return integer(collection.Count, label, 0, maximum)


def outside_main(capture):
    """Native-segment digest; Word offsets never index Python display strings."""
    return capture['document']['outside_main_sha256']


class WordNative:
    def __init__(self, session):
        require(os.name == 'nt', 'Native Word automation is available only on Windows.')
        import comtypes
        from comtypes.automation import IDispatch
        from comtypes.client import GetActiveObject
        from comtypes.client.dynamic import Dispatch
        self._comtypes = comtypes
        self.session = session
        self._process = None
        self.application = self.document = self.window = None
        self._document_iunknown = self._window_iunknown = None
        self._controls, self._notes, self._fields = {}, [], []
        self._initialized = False
        try:
            comtypes.CoInitializeEx(2)  # One worker-thread STA; no GUI COM calls.
            self._initialized = True
            self.application = Dispatch(GetActiveObject('Word.Application', interface=IDispatch))
            require(self.application.Documents.Count > 0, 'Open the intended document in Word first.')
            self.document = self.application.ActiveDocument
            self.window = self.application.ActiveWindow
            self._document_iunknown = self._unknown(self.document)
            self._window_iunknown = self._unknown(self.window)
            self._open_process()
            self._same_document()
        except BaseException:
            self.close()
            raise

    def _unknown(self, value):
        pointer = value._comobj.QueryInterface(self._comtypes.IUnknown)
        return pointer

    @staticmethod
    def _address(pointer):
        return ctypes.cast(pointer, ctypes.c_void_p).value

    def _open_process(self):
        self._user = ctypes.WinDLL('user32', use_last_error=True)
        self._kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self._user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user.IsWindow.argtypes, self._user.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        self._kernel.GetProcessTimes.restype = wintypes.BOOL
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.CloseHandle.argtypes, self._kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        self.hwnd = integer(self.window.Hwnd, 'Word window handle', 1)
        pid = wintypes.DWORD()
        require(self._user.IsWindow(self.hwnd)
                and self._user.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid)),
                'The selected Word window is unavailable.')
        self.pid = pid.value
        self._process = self._kernel.OpenProcess(0x1000 | 0x00100000, False, self.pid)
        if not self._process:
            raise ctypes.WinError(ctypes.get_last_error())
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self._kernel.GetProcessTimes(self._process, *(ctypes.byref(item) for item in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.creation_time = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)

    def _same_document(self):
        require(self._process is not None and self._kernel.WaitForSingleObject(self._process, 0) == 0x102,
                'The captured Word process is no longer alive.')
        pid = wintypes.DWORD()
        require(self._user.IsWindow(self.hwnd)
                and self._user.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
                and pid.value == self.pid, 'The captured Word window changed.')
        require(self._address(self._unknown(self.application.ActiveDocument))
                == self._address(self._document_iunknown)
                and self._address(self._unknown(self.application.ActiveWindow))
                == self._address(self._window_iunknown)
                and int(self.window.Hwnd) == self.hwnd,
                'Word changed its active document or window; inspect again.')
        require(self._address(self._unknown(self.window.Document)) == self._address(self._document_iunknown),
                'The held Word window belongs to another document.')

    def _range_text(self, value):
        first, last = int(value.Start), int(value.End)
        integer(first, 'Word range start', 0, MAX_TEXT)
        integer(last, 'Word range end', first, MAX_TEXT)
        # Range-local retrieval options do not change Word's screen or document.
        # Native offsets can count field internals absent from displayed text.
        selected = value.Duplicate
        selected.TextRetrievalMode.IncludeHiddenText = True
        selected.TextRetrievalMode.IncludeFieldCodes = True
        return raw_text(selected.Text, 'Word range text')

    def _outside_hash(self, value, spans):
        first, last = int(value.Start), int(value.End)
        integer(first, 'outside range start', 0, MAX_TEXT)
        integer(last, 'outside range end', first, MAX_TEXT)
        cursor, result = first, sha256()
        for begin, end in sorted(spans):
            require(cursor <= begin <= end <= last, 'Managed Word ranges overlap or exceed their story.')
            segment = value.Duplicate
            segment.SetRange(cursor, begin)
            result.update(self._range_text(segment).encode('utf-8'))
            cursor = end
        segment = value.Duplicate
        segment.SetRange(cursor, last)
        result.update(self._range_text(segment).encode('utf-8'))
        return result.hexdigest()

    def _parts(self, namespace, *, maximum=8, retain=lambda amount: None):
        parts = self.document.CustomXMLParts.SelectByNamespace(namespace)
        count = _count(parts, maximum, 'selected metadata count')
        values = []
        for index in range(1, count + 1):
            part = parts.Item(index)
            xml = text(part.XML, 'selected Word metadata XML', MAX_BYTES, multiline=True)
            amount = len(xml.encode('utf-8'))
            require(amount <= MAX_BYTES, 'Word metadata exceeds its byte bound.')
            retain(amount)
            values.append((part, xml))
        return values

    def capture(self):
        self._same_document()
        main = self.document.Content
        require(int(main.Start) == 0 and int(main.End) <= MAX_TEXT,
                'Select a Word document within the bounded main-story allowance.')
        remaining = MAX_BYTES - 2 * 1024**2  # bounded object/field/JSON overhead
        def retain(amount):
            nonlocal remaining
            require(type(amount) is int and 0 <= amount <= remaining,
                    'The combined Word capture exceeds its retained-data budget.')
            remaining -= amount
        def retained_text(value):
            units = int(value.End) - int(value.Start)
            require(0 <= units <= MAX_TEXT, 'Invalid retained Word range extent.')
            # A UTF-16 unit needs at most six bytes in ASCII JSON. Reserve
            # before asking COM for another possibly overlapping large string.
            allowance = units * 6 + 128
            require(allowance <= remaining, 'The combined Word capture exceeds its retained-data budget.')
            raw = self._range_text(value)
            actual = len(canonical(raw)) + 128
            retain(max(allowance, actual))
            return raw
        main_text = retained_text(main)
        selection = self.application.Selection.Range
        require(self._address(self._unknown(selection.Document)) == self._address(self._document_iunknown),
                'Word selection belongs to another document.')
        note_rows, note_objects = [], []
        for index in range(1, _count(self.document.Endnotes, 1000, 'endnote count') + 1):
            note = self.document.Endnotes.Item(index)
            reference = note.Reference
            require(int(reference.End) - int(reference.Start) == 1,
                    'This Word endnote reference has an unsupported extent.')
            self._range_text(note.Range)
            note_rows.append({'index': index, 'anchor': int(reference.Start),
                              'start': int(note.Range.Start), 'end': int(note.Range.End)})
            note_objects.append(note)
        controls, objects = [], {}
        collections = [self.document.ContentControls, *(note.Range.ContentControls for note in note_objects)]
        for collection in collections:
            for index in range(1, _count(collection, MAX_CONTROLS, 'content control count') + 1):
                control = collection.Item(index)
                native_id = str(control.ID)
                if native_id in objects:
                    require(self._address(self._unknown(objects[native_id]))
                            == self._address(self._unknown(control)), 'Duplicate native control identity.')
                    continue
                require(len(objects) < MAX_CONTROLS, 'Too many Word content controls.')
                value = control.Range
                story, first, last = int(value.StoryType), int(value.Start), int(value.End)
                note_index, anchor = None, first
                if story == 3:
                    matching = [row for row in note_rows if row['start'] <= first <= last <= row['end']]
                    require(len(matching) == 1, 'Endnote content control is not uniquely located.')
                    note_index, anchor = matching[0]['index'], matching[0]['anchor']
                controls.append({'native_id': native_id, 'tag': str(control.Tag), 'story': story,
                    'start': first, 'end': last, 'anchor': anchor, 'text': retained_text(value),
                    'locked': bool(control.LockContents) or bool(control.LockContentControl),
                    'note_index': note_index})
                objects[native_id] = control
        field_rows, field_objects, field_ids = [], [], set()
        for collection in (self.document.Fields, *(note.Range.Fields for note in note_objects)):
            for index in range(1, _count(collection, 4096, 'field count') + 1):
                field = collection.Item(index)
                field_id = self._address(self._unknown(field))
                if field_id in field_ids:
                    continue
                require(len(field_ids) < 4096, 'Too many Word fields.')
                field_ids.add(field_id)
                code, result = field.Code, field.Result
                code_text, result_text = self._range_text(code), self._range_text(result)
                field_rows.append({'start': max(0, int(code.Start) - 1), 'end': int(result.End) + 1,
                                   'story': int(code.StoryType), 'code_sha256': _sha(code_text),
                                   'result_sha256': _sha(result_text)})
                field_objects.append(field)
        selected = self._parts(NAMESPACE, maximum=1, retain=retain)
        require(len(selected) <= 1, 'The document has conflicting Resonance citation metadata.')
        metadata = parse_metadata_xml(selected[0][1]) if selected else None
        legacy = self._parts(LEGACY_NAMESPACE, retain=retain)
        self._controls, self._notes, self._fields = objects, note_objects, field_objects
        self._metadata_part = selected[0][0] if selected else None
        main_spans = [(row['start'], row['end']) for row in controls
                      if owned_tag(row['tag']) and row['story'] == 1]
        managed_notes = {row['note_index'] for row in controls
                         if owned_tag(row['tag']) and row['story'] == 3}
        main_spans.extend((row['anchor'], row['anchor'] + 1) for row in note_rows
                          if row['index'] in managed_notes)
        outside_digest = self._outside_hash(main, main_spans)
        result = {
            'schema': 'resonance-word-capture/1', 'session': self.session,
            'document': {'hwnd': self.hwnd, 'pid': self.pid, 'creation_time': self.creation_time,
                'word_version': str(self.application.Version),
                'name': str(self.document.Name), 'full_name': str(self.document.FullName),
                'text': main_text, 'outside_main_sha256': outside_digest,
                'readonly': bool(self.document.ReadOnly),
                'protection': int(self.document.ProtectionType),
                'track_revisions': bool(self.document.TrackRevisions),
                'undo_level': int(self.application.UndoRecord.CustomRecordLevel)},
            'selection': {'start': int(selection.Start), 'end': int(selection.End), 'story': int(selection.StoryType)},
            'controls': sorted(controls, key=lambda row: (row['story'], row['start'], row['native_id'])),
            'fields': field_rows, 'endnotes': note_rows, 'metadata': metadata,
            'metadata_xml_sha256': _sha(selected[0][1]) if selected else None,
            'legacy_parts': sorted(_sha(xml) for _, xml in legacy),
        }
        self._same_document()
        return validate_capture(result, self.session)

    def _typography(self, control, operation, check):
        check(); self._same_document()
        value = control.Range
        require(self._range_text(value) == operation['text'], 'Word changed the inserted citation text.')
        start, offset = int(value.Start), 0
        for run in operation['runs']:
            units = utf16_length(run['text'])
            if units:
                check(); self._same_document()
                part = value.Duplicate
                part.SetRange(start + offset, start + offset + units)
                require(self._range_text(part) == run['text'], 'Word typography range changed.')
                styles = set(run['styles'])
                font = part.Font
                check()
                font.Bold = -1 if 'bold' in styles else 0
                check()
                font.Italic = -1 if 'italic' in styles else 0
                check()
                font.Underline = 1 if 'underline' in styles else 0
                check()
                font.Superscript = -1 if 'superscript' in styles else 0
                check()
                font.Subscript = -1 if 'subscript' in styles else 0
                check()
                font.SmallCaps = -1 if 'small_caps' in styles else 0
            offset += units
        require(offset == utf16_length(operation['text']), 'Incomplete Word typography coverage.')

    def _outside_note(self, note, controls):
        value = note.Range
        first, last = int(value.Start), int(value.End)
        spans = []
        for control in controls:
            area = control.Range
            if (owned_tag(str(control.Tag)) and int(area.StoryType) == 3
                    and first <= int(area.Start) <= int(area.End) <= last):
                spans.append((int(area.Start), int(area.End)))
        return self._outside_hash(value, spans)

    def apply(self, proposal, capture, *, check, first_write):
        validate_proposal(proposal, capture)
        require(fingerprint(self.capture()) == fingerprint(capture), 'Word changed since the approved preview.')
        # Retain the exact native objects rather than looking up new active ranges
        # after the first mutation. All foreign fields and content remain outside
        # the allowlisted target set.
        objects = dict(self._controls)
        fields = [(field, _sha(self._range_text(field.Code)), _sha(self._range_text(field.Result)))
                  for field in self._fields]
        foreign = {row['native_id']: row for row in capture['controls'] if not owned_tag(row['tag'])}
        original_outside = outside_main(capture)
        original_note_objects = list(self._notes)
        original_note_outside = [(note, self._outside_note(note, list(objects.values())))
                                 for note in original_note_objects]
        selection = self.application.Selection.Range.Duplicate
        metadata_part = self._metadata_part
        undo = self.application.UndoRecord
        started, wrote = False, False
        undo_name = 'Resonance citations ' + self.session
        warnings, bindings = [], {}

        def owned_undo():
            return int(undo.CustomRecordLevel) == 1 and str(undo.CustomRecordName) == undo_name

        def mutable():
            self._same_document()
            require(not bool(self.document.ReadOnly) and int(self.document.ProtectionType) == -1
                    and not bool(self.document.TrackRevisions), 'Word editing protection or revision tracking changed.')
            require(owned_undo(), 'The owned Word undo group changed.')
            check()

        def will_write():
            nonlocal wrote
            mutable()
            if not wrote:
                first_write()
                wrote = True

        def end_owned_undo():
            nonlocal started
            require(owned_undo(), 'The owned Word undo group changed; no foreign group was closed.')
            undo.EndCustomRecord()
            started = False
            require(int(undo.CustomRecordLevel) == 0, 'Word did not close the owned undo group.')

        try:
            self._same_document()
            require(int(undo.CustomRecordLevel) == 0 and str(undo.CustomRecordName) == '',
                    'Another Word custom undo action is active.')
            check()
            undo.StartCustomRecord(undo_name)
            started = True
            require(owned_undo(), 'Word did not begin the owned undo group.')
            # Existing ranges first; the held insertion Range follows their shifts.
            ordered = sorted(proposal['operations'], key=lambda item: item['native_id'] is None)
            for operation in ordered:
                mutable()
                if operation['native_id'] is not None:
                    control = objects[operation['native_id']]
                    require(str(control.Tag) == operation['tag']
                            and not control.LockContents and not control.LockContentControl,
                            'The held citation target changed.')
                else:
                    require(int(selection.Start) == int(selection.End) and int(selection.StoryType) == 1,
                            'The held insertion point is no longer collapsed.')
                    target = selection
                    if operation['note']:
                        # Let Word retain its native reference mark and paragraph;
                        # bind only the literal citation text inside the new note.
                        will_write()
                        note = self.document.Endnotes.Add(selection, Text=operation['text'])
                        require(int(note.Index) == operation['note_index'], 'Word assigned an unexpected note position.')
                        whole = note.Range
                        note_text = self._range_text(whole)
                        content = operation['text']
                        require(content and note_text.count(content) == 1,
                                'Word did not expose the exact new note text.')
                        index = note_text.index(content)
                        require(all(c in '\x02 \t' for c in note_text[:index])
                                and all(c in '\r' for c in note_text[index + len(content):]),
                                'Word note contains additional unreviewed text.')
                        target = whole.Duplicate
                        first = int(whole.Start) + utf16_length(note_text[:index])
                        target.SetRange(first, first + utf16_length(content))
                    will_write()
                    control = self.document.ContentControls.Add(0, target)
                    mutable()
                    control.Tag = operation['tag']
                    mutable()
                    control.Title = 'Resonance citation' if operation['tag'].startswith(TAG_CITATION) else 'Resonance bibliography'
                target_range = control.Range
                will_write()
                target_range.Text = operation['text']
                self._typography(control, operation, mutable)
                require(self._range_text(control.Range) == operation['text'], 'Word did not retain the reviewed text.')
                bindings[operation['tag']] = {
                    'tag': operation['tag'], 'native_id': str(control.ID),
                    'text_sha256': _sha(operation['text']), 'story': int(control.Range.StoryType)}
            data = proposal['interchange']
            citation_bindings = [bindings['resonance:cite:' + row['id']] for row in data['clusters']]
            bibliography = bindings.get('resonance:biblio:' + proposal['document_id'])
            metadata = {'schema': 'resonance-word-citations/1',
                'document_id': proposal['document_id'], 'revision': proposal['revision'],
                'interchange': data, 'bindings': citation_bindings, 'bibliography': bibliography,
                'source_scope': proposal['source_scope'], 'last_request': proposal['session']}
            xml = metadata_xml(metadata)
            mutable()
            if metadata_part is None:
                metadata_part = self.document.CustomXMLParts.Add(xml)
            else:
                require(bool(metadata_part.LoadXML(xml)), 'Word did not accept the reviewed metadata.')
            # End the owned undo record before a strict final snapshot. Failure
            # does not automatically Undo: another user's changes are never rolled back.
            try:
                end_owned_undo()
            except Exception:
                warnings.append('The citation content was written, but Word could not confirm the undo group closed.')
            final = self.capture()
            require(final['metadata'] == metadata and outside_main(final) == original_outside,
                    'Word changed content outside the reviewed managed ranges.')
            require(final['legacy_parts'] == capture['legacy_parts'], 'Legacy citation metadata changed.')
            for note, outside in original_note_outside:
                require(self._outside_note(note, list(self._controls.values())) == outside,
                        'Existing endnote text outside managed citations changed.')
            final_foreign = {row['native_id']: row for row in final['controls'] if not owned_tag(row['tag'])}
            require(set(final_foreign) == set(foreign), 'Foreign content control inventory changed.')
            for native_id, before in foreign.items():
                after = final_foreign[native_id]
                require(all(after[key] == before[key] for key in ('tag', 'story', 'text', 'locked')),
                        'Foreign Word content changed.')
            require(len(self._fields) == len(fields), 'Foreign Word field inventory changed.')
            for field, code, result in fields:
                require(_sha(self._range_text(field.Code)) == code and _sha(self._range_text(field.Result)) == result,
                        'Foreign Word field content changed.')
            by_tag = {row['tag']: row for row in final['controls'] if owned_tag(row['tag'])}
            for operation in proposal['operations']:
                row = by_tag[operation['tag']]
                require(row['text'] == operation['text']
                        and row['note_index'] == operation['note_index'], 'Final Word citation differs from preview.')
            return {'schema': 'resonance-word-completed/1', 'session': self.session,
                    'proposal_sha256': fingerprint(proposal), 'document_id': metadata['document_id'],
                    'revision': metadata['revision'], 'metadata_sha256': fingerprint(metadata),
                    'bindings': [bindings[key] for key in sorted(bindings)],
                    'warnings': warnings, 'saved_by_bridge': False}
        finally:
            if started:
                try:
                    if owned_undo():
                        end_owned_undo()
                except Exception:
                    pass  # Never close a foreign group or Undo an unowned user's change.

    def close(self):
        # Only release references and our read-only process HANDLE. Word belongs
        # to its user; this adapter never calls document.Close or application.Quit.
        self._controls, self._notes, self._fields = {}, [], []
        self._metadata_part = None
        self.document = self.window = self.application = None
        self._document_iunknown = self._window_iunknown = None
        if self._process is not None:
            handle, self._process = self._process, None
            if not self._kernel.CloseHandle(handle):
                self._process = handle
                raise ctypes.WinError(ctypes.get_last_error())
        if self._initialized:
            self._initialized = False
            self._comtypes.CoUninitialize()
