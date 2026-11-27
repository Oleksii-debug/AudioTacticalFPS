"""Optional desktop Word automation over the existing CARR citation snapshot.

Only an explicitly requested new copy is changed. Word is attached lazily, never
started or quit by this module. No VBA, field evaluation or external resources
are loaded. COM calls are cooperative: a busy Office dialog must be dismissed in
Word; cancellation cannot safely terminate someone else's Word process.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
from uuid import uuid4
from zipfile import ZipFile

from .bibliography_workbench import _parts, records_for_style
from .csl import PROCESSOR_PACKAGE_VERSION, PROCESSOR_REPORTED_VERSION, RenderBudget, _records
from .documents import (
    CitationParagraph, _MAX_DOCUMENT, _NAMESPACE, _json, _read_citation_bytes,
    citation_document, citation_xml, read_citation_xml,
)
from .regeneration import _body, _style
from ..reader.narration_text import read_document
from ..research.export_file import capture_export_destination, write_binary_export

_LOCK = threading.Lock()
_BEFORE = 'carr_word_body_before'
_AFTER = 'carr_word_body_after'
_BIBLIOGRAPHY = 'carr.bibliography.v1'
_PREFIX = 'carr.citation.'
_MAX_TEXT = 4_000_000
_MAX_CONTROLS = 10_000


class WordUnavailable(RuntimeError):
    """The optional bridge or running desktop Word is unavailable."""


class WordRecoveryRequired(RuntimeError):
    """Only the working copy was touched; inspect it before saving."""


@dataclass(frozen=True)
class WordCopy:
    path: Path
    original: Path
    original_sha256: str
    custom_style: object = None
    original_citation_ids: tuple[str, ...] = ()
    original_reference_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WordResult:
    path: Path
    citation_ids: tuple[str, ...]
    reference_ids: tuple[str, ...]
    kind: str
    changed: bool = False
    quotation_verified: bool = False
    saved_to_disk: bool = False
    status: str = 'complete'


def _phase(message):
    # Progress is advisory; a detached observer never retries a Word mutation.
    try:
        from ..progress import current_progress
        reporter = current_progress()
        if reporter is not None:
            reporter.update(phase=message)
    except Exception:
        pass


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Word citation action cancelled; check the working copy if Word was busy.')


def _word(value):
    return value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\v')


def _plain(markup):
    return _word(''.join(text for text, _ in _parts(markup)))


def _units(text):
    """Word range offsets count UTF-16 code units, including surrogate pairs."""
    return len(text.encode('utf-16-le')) // 2


def _bookmark(identity):
    return 'carr_' + sha256(identity.encode('utf-8')).hexdigest()[:30]


def _tag(identity):
    return _PREFIX + sha256(identity.encode('utf-8')).hexdigest()[:40]


def _path(value):
    raw = os.fspath(value)
    win = PureWindowsPath(raw)
    parts = [part for part in win.parts if part != win.anchor]
    if (not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//', '\\??\\'))
            or (win.drive and (len(win.drive) != 2 or win.drive[1] != ':'
                               or not win.root or not win.drive[0].isalpha()))
            or any(PureWindowsPath(part).is_reserved() or ':' in part
                   or any(ord(char) < 32 or char in '<>"|?*' for char in part) for part in parts)):
        raise ValueError('Choose a regular local DOCX without a network, device or alternate stream.')
    path = Path(raw).absolute()
    if path.suffix.lower() != '.docx':
        raise ValueError('Only a saved, macro-free .docx citation document is supported.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        classify = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        classify.argtypes, classify.restype = [wintypes.LPCWSTR], wintypes.UINT
        if classify(path.anchor) not in (2, 3, 5, 6):
            raise ValueError('Choose a DOCX on an available local drive.')
    import stat
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('The Word document path must not contain links or reparse points.')
    return path.resolve(strict=True)


def _safe_package(payload):
    """Opening a Word file is active; reject active parts and external targets first."""
    from defusedxml import ElementTree
    snapshot = _read_citation_bytes(payload)  # Same ZIP, metadata and CSL budgets.
    allowed_relationships = {
        'officeDocument', 'core-properties', 'extended-properties', 'custom-properties',
        'styles', 'stylesWithEffects', 'settings', 'webSettings', 'fontTable', 'theme',
        'numbering', 'customXml', 'customXmlProps', 'endnotes', 'thumbnail',
    }
    with ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        xml_bytes = 0
        for info in archive.infolist():
            name = info.filename.lower()
            if (any(part in name for part in ('vbaproject', '/activex/', '/embeddings/',
                                             '/externallinks/', '/webextensions/'))
                    or name.endswith(('.bin', '.vml'))):
                raise ValueError('Word automation does not open embedded or active document content.')
            if name.startswith('word/') and name.endswith('.xml'):
                xml_bytes += info.file_size
                if info.file_size > 16 * 1024 * 1024 or xml_bytes > 64 * 1024 * 1024:
                    raise ValueError('Word XML exceeds its automation parsing budget.')
                node = ElementTree.fromstring(archive.read(info), forbid_dtd=True,
                    forbid_entities=True, forbid_external=True)
                fields = {'{http://schemas.openxmlformats.org/wordprocessingml/2006/main}' + kind
                          for kind in ('fldSimple', 'fldChar', 'instrText')}
                if any(child.tag in fields for child in node.iter()):
                    raise ValueError('Word automation does not open field instructions or active content.')
            if name.endswith('.rels'):
                if info.file_size > 1024 * 1024:
                    raise ValueError('Word relationship metadata exceeds its byte budget.')
                root = ElementTree.fromstring(archive.read(info),
                    forbid_dtd=True, forbid_entities=True, forbid_external=True)
                for node in root:
                    target = node.get('Target', '')
                    relation = node.get('Type', '').rsplit('/', 1)[-1]
                    if (node.get('TargetMode', 'Internal') != 'Internal'
                            or not target or target.startswith(('/', '\\'))
                            or any(char in target for char in (':', '\\', '%', '#', '?'))
                            or relation not in allowed_relationships):
                        raise ValueError('Word automation refuses external or unsupported relationships.')
                    # Resolve only inside the captured ZIP; never against a
                    # filesystem, URL, template server or current directory.
                    components = list(PurePosixPath(info.filename).parts[:-2])
                    for component in target.split('/'):
                        if component == '..':
                            if not components:
                                raise ValueError('Word relationship escapes the captured package.')
                            components.pop()
                        elif component not in ('', '.'):
                            components.append(component)
                    if '/'.join(components) not in names:
                        raise ValueError('Word relationship does not name a captured local part.')
    return snapshot


def _validated(snapshot, custom_style):
    if snapshot['processor'] != {'package': PROCESSOR_PACKAGE_VERSION,
                                 'reported': PROCESSOR_REPORTED_VERSION}:
        raise ValueError('The saved citation processor differs from the installed processor.')
    style, kind = _style(snapshot, custom_style)
    if any(not _plain(value).strip()
           for value in (*snapshot['rendered']['citations'], *snapshot['rendered']['entries'])):
        raise ValueError('Word citation controls require nonblank rendered content.')
    title, paragraphs = _body(snapshot['document'], snapshot['citation_ids'])
    # Reuse the real CSL renderer and the exact existing snapshot schema.
    regenerated = _read_citation_bytes(citation_document(snapshot['records'], style=style,
        locale=snapshot['locale'], citations=snapshot['clusters'], paragraphs=paragraphs, title=title))
    if (regenerated['clusters'] != snapshot['clusters']
            or regenerated['records'] != snapshot['records']
            or regenerated['rendered'] != snapshot['rendered']):
        raise ValueError('The saved citation content does not match its pinned CSL identity.')
    return style, kind


def _rows(snapshot, additions, *, edited=None, items=None):
    rows = list(snapshot['records'])
    if additions is None:
        return rows
    normalized, _, _ = _records(additions, RenderBudget())
    by_id = {row['id']: row for row in rows}
    changed = {row['id'] for row in normalized if row['id'] in by_id and row != by_id[row['id']]}
    for cluster in snapshot['clusters']:
        if cluster['id'] != edited and any('locator' in item and item['id'] in changed
                                          for item in cluster['items']):
            raise ValueError('A cited source changed. Explicitly review its retained locators before editing.')
    for row in normalized:
        by_id[row['id']] = row
    result = [by_id.pop(row['id']) for row in rows]
    result.extend(by_id.values())
    return result


def _result(copy, snapshot, changed=False):
    return WordResult(copy.path, tuple(snapshot['citation_ids']),
                      tuple(row['id'] for row in snapshot['records']),
                      snapshot['style']['kind'], changed)


@contextmanager
def _running_word():
    if os.name != 'nt':
        raise WordUnavailable('Desktop Word automation is available only on Windows.')
    try:
        import pythoncom
        from win32com.client import GetActiveObject
    except ImportError as exc:
        raise WordUnavailable('The optional pinned Word runtime is not installed.') from exc
    initialized = False
    application = None
    session = None
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        try:
            application = GetActiveObject('Word.Application')
        except pythoncom.com_error as exc:
            if (int(exc.hresult) & 0xffffffff) in (0x800401e3, 0x80040154):
                raise WordUnavailable('Open desktop Microsoft Word and a saved CARR citation DOCX first.') from exc
            raise
        session = _WordApplication(application)
        application = None
        yield session
    finally:
        # Caller frames may still retain the public wrappers. Detach every
        # session proxy while this thread's COM apartment is still initialized;
        # never close documents, quit Word or manually release its IUnknown.
        if session is not None:
            session._release()
        session = None
        application = None
        if initialized:
            pythoncom.CoUninitialize()


class WordBridge:
    """Synchronous commands; the native dialog invokes them on its one worker."""

    def __init__(self, workbench, *, connector=None):
        self.workbench = workbench
        self.connector = connector or _running_word

    def _permission(self, cancelled, preview=None):
        _check(cancelled)
        with self.workbench.controller.library.read(self.workbench.controller.workspace):
            pass
        if preview is not None:
            return records_for_style(self.workbench.check(preview), name_mode=preview.name_mode)[0]
        return None

    @contextmanager
    def _application(self, cancelled):
        _check(cancelled)
        if not _LOCK.acquire(blocking=False):
            raise RuntimeError('A Word citation action is already in progress.')
        try:
            with self.connector() as application:
                yield application
        finally:
            _LOCK.release()

    def connect(self, destination, *, custom_style=None, approval=None, cancelled=lambda: False):
        """Copy one explicitly selected saved document; never replace or save the original."""
        self._permission(cancelled)
        _phase('Checking the saved Word citation document')
        with self._application(cancelled) as application:
            source = application.active()
            original = source.path
            source.require_editable(saved=True)
            payload = read_document(original, byte_budget=_MAX_DOCUMENT)
            snapshot = _safe_package(payload)
            _validated(snapshot, custom_style)
            source.preflight(snapshot)
            before = source.signature()
            digest = sha256(payload).hexdigest()
            if approval is None:
                approval = capture_export_destination(destination, private_root=self.workbench.controller.library.root)
            if not approval.require_absent:
                raise FileExistsError('Choose a new Word filename; the original is never overwritten.')
            if Path(destination).suffix.lower() != '.docx':
                raise ValueError('Choose a new .docx filename for the working copy.')

            def still_current():
                self._permission(cancelled)
                source.require_editable(saved=True)
                if (application.active().path != original or source.signature() != before
                        or sha256(read_document(original, byte_budget=_MAX_DOCUMENT)).hexdigest() != digest):
                    raise ValueError('The original Word document changed; choose the copy again.')
                return False

            _phase('Publishing a new Word working copy')
            write_binary_export(destination, payload, private_root=self.workbench.controller.library.root,
                                require_absent=True, approval=approval, cancelled=still_current)
            target = _path(destination)
            # A cancelled launch leaves a complete, unchanged copy on disk.
            self._permission(cancelled)
            if sha256(read_document(target, byte_budget=_MAX_DOCUMENT)).hexdigest() != digest:
                raise ValueError('The newly published Word copy changed before opening.')
            _phase('Opening and binding Word citation controls')
            document = application.open_copy(target)
            if document.path != target:
                raise ValueError('Word opened a different document.')
            document.preflight(snapshot)
            def permitted():
                self._permission(cancelled)
                return False
            document.adopt(snapshot, cancelled=permitted)
            return WordCopy(target, original, digest, custom_style,
                tuple(snapshot['citation_ids']), tuple(row['id'] for row in snapshot['records']))

    def inspect(self, copy, *, cancelled=lambda: False):
        self._permission(cancelled)
        with self._application(cancelled) as application:
            document, snapshot, _ = self._current(application, copy)
            return _result(copy, snapshot)

    def _current(self, application, copy):
        if type(copy) is not WordCopy or copy.path == copy.original:
            raise ValueError('Connect an explicit new Word working copy first.')
        document = application.active()
        if document.path != copy.path:
            raise ValueError('Activate the connected Word working copy before this action.')
        document.require_editable()
        snapshot = document.snapshot()
        if (not set(copy.original_citation_ids) <= set(snapshot['citation_ids'])
                or not set(copy.original_reference_ids) <= {row['id'] for row in snapshot['records']}):
            raise ValueError('The managed document no longer contains its connected source identities.')
        style, _ = _validated(snapshot, copy.custom_style)
        document.capture(snapshot)
        return document, snapshot, style

    def change(self, copy, *, preview=None, items=None, occurrence_id=None,
               insert=False, cancelled=lambda: False):
        """Insert at a supported caret, replace one occurrence, or refresh the bibliography.

        Items are the existing typed CSL item contract. They are explicit user
        choices, never inferred publication page ranges or verified quotations.
        """
        additions = self._permission(cancelled, preview)
        _phase('Checking Word scope and citation identities')
        if type(insert) is not bool or (insert and occurrence_id is not None):
            raise ValueError('Choose one insertion or one existing citation.')
        if (insert or occurrence_id is not None) != (items is not None):
            raise ValueError('Insertion and editing require explicit citation items.')
        with self._application(cancelled) as application:
            document, snapshot, style = self._current(application, copy)
            view = document.capture(snapshot, insertion=insert)
            before = document.signature()
            ordered = {cluster['id']: dict(cluster) for cluster in snapshot['clusters']}
            clusters = [ordered[identity] for identity in view['ids']]
            edited = occurrence_id
            insertion = None
            if insert:
                edited = 'citation-' + uuid4().hex
                insertion = (view['caret'], edited)
                index = sum(start < view['caret'] for _, start, _ in view['anchors'])
                clusters.insert(index, {'id': edited, 'items': items, 'note_index': None})
            elif occurrence_id is not None:
                if occurrence_id not in ordered:
                    raise ValueError('Choose an existing citation occurrence.')
                clusters = [dict(cluster, items=items) if cluster['id'] == occurrence_id else cluster
                            for cluster in clusters]
            rows = _rows(snapshot, additions, edited=edited, items=items)
            if snapshot['style']['kind'] == 'note':
                clusters = [dict(cluster, note_index=index) for index, cluster in enumerate(clusters, 1)]
            body = document.paragraphs(snapshot, insertion=insertion)
            _phase('Formatting the existing CSL citation snapshot')
            rendered = _read_citation_bytes(citation_document(rows, style=style, locale=snapshot['locale'],
                citations=clusters, paragraphs=body, title=snapshot['document']['title'], cancelled=cancelled))
            if any(not _plain(value).strip() for value in
                   (*rendered['rendered']['citations'], *rendered['rendered']['entries'])):
                raise ValueError('Word citation controls require nonblank rendered content.')
            # Authorization and the exact captured selection/content are checked
            # again after CSL work and immediately before the first Word change.
            self._permission(cancelled, preview)
            if application.active().path != copy.path or document.signature() != before:
                raise ValueError('The Word document or selection changed during formatting; try again.')
            def permitted():
                self._permission(cancelled, preview)
                return False
            _phase('Applying one undoable Word citation update')
            document.apply(snapshot, rendered, insertion=insertion, cancelled=permitted)
            return _result(copy, rendered, True)


class _WordApplication:
    def __init__(self, application):
        self.application = application
        self._documents = []

    def _document(self, document):
        wrapper = _ComDocument(self.application, document)
        self._documents.append(wrapper)
        return wrapper

    def _release(self):
        for document in self._documents:
            document.doc = None
            document.app = None
        self._documents.clear()
        self.application = None

    def active(self):
        if not self.application.Documents.Count:
            raise WordUnavailable('Open a saved CARR citation DOCX in desktop Word first.')
        return self._document(self.application.ActiveDocument)

    def open_copy(self, path):
        # Package inspection has already refused macros, embedded objects and
        # every external relationship. Do not mutate global Office security policy.
        document = self._document(self.application.Documents.Open(FileName=str(path),
            ConfirmConversions=False, ReadOnly=False, AddToRecentFiles=False,
            Visible=True, OpenAndRepair=False, NoEncodingDialog=True))
        document.doc.Activate()
        return document


def _items(collection, limit=_MAX_CONTROLS):
    count = int(collection.Count)
    if count < 0 or count > limit:
        raise ValueError('The Word document exceeds its automation object budget.')
    return [collection.Item(index) for index in range(1, count + 1)]


def _subrange(parent, start, end):
    result = parent.Duplicate
    result.SetRange(start, end)
    return result


class _ComDocument:
    def __init__(self, application, document):
        self.app, self.doc = application, document

    @property
    def path(self):
        return _path(str(self.doc.FullName))

    def require_editable(self, *, saved=False):
        if (bool(self.doc.ReadOnly) or int(self.doc.ProtectionType) != -1
                or bool(self.doc.TrackRevisions) or int(self.doc.Revisions.Count)
                or int(self.doc.Tables.Count) or int(self.doc.Fields.Count)
                or int(self.doc.Footnotes.Count)):
            raise ValueError('Use an editable CARR document without tables, fields, tracked changes or footnotes.')
        if saved and not bool(self.doc.Saved):
            raise ValueError('Save the original in Word before creating a working copy.')
        if bool(self.app.UndoRecord.IsRecordingCustomRecord):
            raise ValueError('Finish the current Word undo operation before using citations.')
        if len(str(self.doc.Content.Text)) > _MAX_TEXT:
            raise ValueError('The Word document exceeds its text budget.')

    def _part(self):
        parts = _items(self.doc.CustomXMLParts.SelectByNamespace(_NAMESPACE), 2)
        if len(parts) != 1:
            raise ValueError('The Word document has no unique CARR citation snapshot.')
        return parts[0]

    def snapshot(self):
        return read_citation_xml(str(self._part().XML))

    def signature(self, *, selection=True):
        text = str(self.doc.Content.Text)
        notes = [(int(note.Reference.Start), str(note.Range.Text)) for note in _items(self.doc.Endnotes)]
        controls = [(str(control.Tag), int(control.Range.StoryType), int(control.Range.Start),
                     int(control.Range.End), str(control.Range.Text))
                    for control in _items(self.doc.ContentControls)]
        xml = str(self.doc.Content.WordOpenXML)
        if len(xml.encode('utf-8')) > 100_000_000:
            raise ValueError('The Word content exceeds its captured XML budget.')
        selected = self.app.Selection.Range if selection else None
        # Raw XML remains readable even after a partial metadata write, allowing
        # rollback rather than trying to validate the incomplete new snapshot.
        value = {'path': str(self.path), 'text': text, 'notes': notes, 'controls': controls,
                 'content_xml_sha256': sha256(xml.encode('utf-8')).hexdigest(),
                 'metadata': str(self._part().XML),
                 'selection': None if selected is None else
                     [str(selected.Document.FullName), int(selected.StoryType),
                      int(selected.Start), int(selected.End)]}
        if len(text) + sum(len(note[1]) for note in notes) > _MAX_TEXT:
            raise ValueError('The Word document exceeds its text budget.')
        return sha256(_json(value)).hexdigest()

    def _body(self):
        before = self.doc.Bookmarks.Item(_BEFORE).Range
        after = self.doc.Bookmarks.Item(_AFTER).Range
        if (int(before.StoryType) != 1 or int(after.StoryType) != 1
                or int(before.End) > int(after.Start) or str(after.Text) != 'Bibliography\r'):
            raise ValueError('The managed body boundaries changed.')
        return self.doc.Range(int(before.End), int(after.Start))

    def _control(self, tag):
        values = _items(self.doc.SelectContentControlsByTag(tag), 2)
        if len(values) != 1 or int(values[0].Type) != 0:
            raise ValueError('A managed Word citation control is missing or duplicated.')
        control = values[0]
        if bool(control.LockContents) or bool(control.LockContentControl):
            raise ValueError('Unlock the managed citation control before editing.')
        return control

    def _note(self, identity):
        if not self.doc.Bookmarks.Exists(_bookmark(identity)):
            raise ValueError('A citation endnote bookmark is missing.')
        text_range = self.doc.Bookmarks.Item(_bookmark(identity)).Range
        if int(text_range.StoryType) != 3:
            raise ValueError('A citation endnote moved to an unsupported story.')
        matches = [note for note in _items(self.doc.Endnotes)
                   if int(note.Range.Start) <= int(text_range.Start)
                   and int(text_range.End) <= int(note.Range.End)]
        if len(matches) != 1:
            raise ValueError('A citation must name exactly one native endnote.')
        return matches[0], text_range

    def _note_text(self, note, expected):
        raw = str(note.Range.Text)
        suffix = expected + ('\r' if raw.endswith('\r') else '')
        if not raw.endswith(suffix) or raw[:-len(suffix)] not in ('', ' ', '\x02 ', '\x02'):
            raise ValueError('The native endnote text differs from the saved citation.')
        end = int(note.Range.End) - (1 if raw.endswith('\r') else 0)
        return _subrange(note.Range, end - _units(expected), end)

    def preflight(self, snapshot):
        self.require_editable()
        if _json(self.snapshot()) != _json(snapshot):
            raise ValueError('The visible Word document and selected file have different citation snapshots.')
        if self.doc.Bookmarks.Exists(_BEFORE) or self.doc.Bookmarks.Exists(_AFTER):
            self.capture(snapshot)
            return
        self._adoption_ranges(snapshot)  # Validate every range before any mutation.

    def _adoption_ranges(self, snapshot):
        if _items(self.doc.ContentControls):
            raise ValueError('Existing foreign content controls cannot be adopted as CARR citations.')
        body = snapshot['document']['paragraphs']
        paragraphs = _items(self.doc.Paragraphs)
        if len(paragraphs) < len(body) + len(snapshot['rendered']['entries']) + 5:
            raise ValueError('The saved citation document layout changed.')
        if (str(paragraphs[0].Range.Text) != _word(snapshot['document']['title']) + '\r'
                or str(paragraphs[1].Range.Text) != _word(snapshot['style']['title']) + '\r'
                or str(paragraphs[2 + len(body)].Range.Text) != 'Bibliography\r'):
            raise ValueError('Connect an editable CARR citation export with its original body headings.')
        texts = dict(zip(snapshot['citation_ids'], map(_plain, snapshot['rendered']['citations'])))
        ranges = {}
        for row, paragraph in zip(body, paragraphs[2:2 + len(body)]):
            value = _word(row['text'])
            start = int(paragraph.Range.Start) + _units(value)
            for identity in row['citation_ids']:
                if snapshot['style']['kind'] == 'note':
                    value += '\x02'
                else:
                    separator = ' ' if value else ''
                    start += _units(separator)
                    value += separator + texts[identity]
                    ranges[identity] = self.doc.Range(start, start + _units(texts[identity]))
                    start += _units(texts[identity])
                if not self.doc.Bookmarks.Exists(_bookmark(identity)):
                    raise ValueError('An original citation occurrence bookmark is missing.')
                if snapshot['style']['kind'] != 'note':
                    prior = self.doc.Bookmarks.Item(_bookmark(identity)).Range
                    if (int(prior.StoryType) != 1 or int(prior.Start) != int(paragraph.Range.Start)
                            or int(prior.End) != int(paragraph.Range.End) - 1):
                        raise ValueError('An original citation bookmark moved to another paragraph.')
            if str(paragraph.Range.Text) != value + '\r':
                raise ValueError('Visible citation-document body edits need review before initial connection.')
        notes = _items(self.doc.Endnotes)
        if snapshot['style']['kind'] == 'note':
            if len(notes) != len(texts):
                raise ValueError('Unmanaged or missing native endnotes cannot be adopted.')
            for index, identity in enumerate(snapshot['citation_ids']):
                note = notes[index]
                original = self.doc.Bookmarks.Item(_bookmark(identity)).Range
                if (int(original.StoryType) != 3 or int(original.End)
                        not in (int(note.Range.End), int(note.Range.End) - 1)):
                    raise ValueError('The original endnote occurrence bookmark moved.')
                ranges[identity] = self._note_text(note, texts[identity])
        elif notes:
            raise ValueError('This in-text style does not own native endnotes.')
        offset = 3 + len(body)
        entries = snapshot['rendered']['entries']
        if not entries:
            raise ValueError('The Word document needs a nonempty bibliography.')
        for index, (markup, ids) in enumerate(zip(entries, snapshot['rendered']['entry_ids'])):
            paragraph = paragraphs[offset + index]
            name = _bookmark('reference:' + '|'.join(ids))
            if (str(paragraph.Range.Text) != _plain(markup) + '\r'
                    or not self.doc.Bookmarks.Exists(name)):
                raise ValueError('The original bibliography text or reference identity changed.')
            prior = self.doc.Bookmarks.Item(name).Range
            if (int(prior.StoryType) != 1 or int(prior.Start) != int(paragraph.Range.Start)
                    or int(prior.End) != int(paragraph.Range.End) - 1):
                raise ValueError('An original bibliography bookmark moved to another paragraph.')
        biblio = self.doc.Range(int(paragraphs[offset].Range.Start),
                              int(paragraphs[offset + len(entries) - 1].Range.End) - 1)
        return ranges, paragraphs[1].Range, paragraphs[2 + len(body)].Range, biblio

    @contextmanager
    def _undo(self, name):
        self.require_editable()
        part = self._part()
        old_xml = str(part.XML)
        old_signature = self.signature(selection=False)
        undo = self.app.UndoRecord
        undo.StartCustomRecord(name)
        active = True
        try:
            yield
            undo.EndCustomRecord()
            active = False
        except BaseException as failure:
            try:
                if active:
                    undo.EndCustomRecord()
                # Avoid undoing an unrelated prior action when nothing changed.
                if self.signature(selection=False) != old_signature:
                    if not self.doc.Undo(1):
                        raise RuntimeError('Word did not undo the working-copy operation.')
                    if _json(self.snapshot()) != _json(read_citation_xml(old_xml)):
                        if not self._part().LoadXML(old_xml):
                            raise RuntimeError('Word did not restore citation provenance.')
            except BaseException as recovery:
                raise WordRecoveryRequired(
                    'Word could not confirm rollback. The original file is unchanged. '
                    'Inspect or close the working copy without saving before reconnecting.') from recovery
            raise failure

    def _add_control(self, value, tag, title):
        control = self.doc.ContentControls.Add(Type=0, Range=value)
        control.Tag, control.Title = tag, title[:64]
        control.LockContents = control.LockContentControl = False
        return control

    def adopt(self, snapshot, *, cancelled):
        if self.doc.Bookmarks.Exists(_BEFORE):
            self.capture(snapshot)
            return
        ranges, before, after, biblio = self._adoption_ranges(snapshot)
        _check(cancelled)
        with self._undo('Connect CARR citation working copy'):
            self.doc.Bookmarks.Add(_BEFORE, before)
            self.doc.Bookmarks.Add(_AFTER, after)
            for identity, value in ranges.items():
                _check(cancelled)
                if snapshot['style']['kind'] == 'note':
                    self.doc.Bookmarks.Add(_bookmark(identity), value)
                else:
                    self._add_control(value, _tag(identity), 'CARR citation ' + identity)
            self._add_control(biblio, _BIBLIOGRAPHY, 'CARR bibliography')
            self.capture(snapshot)

    def capture(self, snapshot, *, insertion=False):
        body = self._body()
        if int(body.Tables.Count) or int(body.Fields.Count):
            raise ValueError('Citations in tables or field results are not supported.')
        texts = dict(zip(snapshot['citation_ids'], map(_plain, snapshot['rendered']['citations'])))
        kind, anchors = snapshot['style']['kind'], []
        expected_tags = {_BIBLIOGRAPHY}
        for identity in snapshot['citation_ids']:
            if kind == 'note':
                note, value = self._note(identity)
                anchor = note.Reference
            else:
                control = self._control(_tag(identity))
                expected_tags.add(_tag(identity))
                value = anchor = control.Range
            if (str(value.Text) != texts[identity] or int(anchor.StoryType) != 1
                    or int(anchor.Start) < int(body.Start) or int(anchor.End) > int(body.End)):
                raise ValueError('A visible citation changed or moved outside the managed document body.')
            anchors.append((identity, int(anchor.Start), int(anchor.End)))
        actual_tags = [str(control.Tag) for control in _items(self.doc.ContentControls)]
        if len(actual_tags) != len(expected_tags) or set(actual_tags) != expected_tags:
            raise ValueError('Unknown, duplicated or missing Word citation controls are present.')
        if int(self.doc.Endnotes.Count) != (len(anchors) if kind == 'note' else 0):
            raise ValueError('Native endnotes no longer match the saved citation occurrences.')
        biblio = self._control(_BIBLIOGRAPHY).Range
        if (int(biblio.StoryType) != 1 or int(biblio.Start) < int(self.doc.Bookmarks.Item(_AFTER).Range.End)
                or str(biblio.Text) != '\r'.join(map(_plain, snapshot['rendered']['entries']))):
            raise ValueError('The visible bibliography differs from its saved formatting.')
        anchors.sort(key=lambda value: value[1])
        if any(left[2] > right[1] for left, right in zip(anchors, anchors[1:])):
            raise ValueError('Nested or overlapping citation controls are not supported.')
        caret = None
        if insertion:
            selected = self.app.Selection.Range
            if (str(selected.Document.FullName) != str(self.doc.FullName)
                    or int(selected.StoryType) != 1 or int(selected.Start) != int(selected.End)
                    or not int(body.Start) <= int(selected.Start) < int(body.End)
                    or bool(selected.Information(12)) or bool(selected.Information(46))
                    or any(start <= int(selected.Start) <= end for _, start, end in anchors)):
                raise ValueError('Place a collapsed caret in ordinary managed body text, outside citations and tables.')
            caret = int(selected.Start)
        return {'ids': tuple(value[0] for value in anchors), 'anchors': tuple(anchors), 'caret': caret}

    def paragraphs(self, snapshot, *, insertion=None):
        body = self._body()
        anchors = list(self.capture(snapshot)['anchors'])
        if insertion is not None:
            anchors.append((insertion[1], insertion[0], insertion[0]))
            anchors.sort(key=lambda value: value[1])
        result, cursor = [], int(body.Start)

        def append(text, identities=()):
            # This is explicitly a plain-text snapshot for the existing
            # regeneration API. Live Word prose/formatting is not replaced.
            chunks = text.replace('\v', '\n').split('\r')
            result.extend(CitationParagraph(chunk) for chunk in chunks[:-1])
            result.append(CitationParagraph(chunks[-1], tuple(identities)))

        for identity, start, end in anchors:
            append(str(self.doc.Range(cursor, start).Text), (identity,))
            cursor = end
        tail = str(self.doc.Range(cursor, int(body.End)).Text)
        if tail.endswith('\r'):
            tail = tail[:-1]
        if tail or not result:
            append(tail)
        if len(result) > 10_000:
            raise ValueError('The Word body exceeds the citation paragraph budget.')
        return tuple(result)

    def _format(self, value, markup):
        return self._format_runs(value, [(_word(text), styles) for text, styles in _parts(markup)])

    def _format_runs(self, value, pieces):
        content = ''.join(text for text, _ in pieces)
        start = int(value.Start)
        value.Text = content
        value.SetRange(start, start + _units(content))
        value.Font.Bold = value.Font.Italic = value.Font.Superscript = False
        value.Font.Subscript = value.Font.SmallCaps = False
        offset = start
        for text, styles in pieces:
            if text:
                span = _subrange(value, offset, offset + _units(text))
                span.Font.Bold = bool(styles & {'b', 'strong'})
                span.Font.Italic = bool(styles & {'i', 'em'})
                span.Font.Superscript = 'sup' in styles
                span.Font.Subscript = 'sub' in styles
                span.Font.SmallCaps = 'small-caps' in styles
                offset += _units(text)
        return value

    def apply(self, previous, snapshot, *, insertion, cancelled):
        self.capture(previous, insertion=insertion is not None)
        old = dict(zip(previous['citation_ids'], map(_plain, previous['rendered']['citations'])))
        new = dict(zip(snapshot['citation_ids'], snapshot['rendered']['citations']))
        kind = snapshot['style']['kind']
        with self._undo('Update CARR citations and bibliography'):
            if insertion is not None:
                _check(cancelled)
                position, identity = insertion
                selected = self.doc.Range(position, position)
                if kind == 'note':
                    note = self.doc.Endnotes.Add(Range=selected, Text=_plain(new[identity]))
                    value = self._note_text(note, _plain(new[identity]))
                    self.doc.Bookmarks.Add(_bookmark(identity), value)
                else:
                    value = self._format(selected, new[identity])
                    self._add_control(value, _tag(identity), 'CARR citation ' + identity)
            for identity in snapshot['citation_ids']:
                _check(cancelled)
                if kind == 'note':
                    _, value = self._note(identity)
                else:
                    value = self._control(_tag(identity)).Range
                if identity in old and str(value.Text) != old[identity]:
                    raise ValueError('A citation changed while the update was being applied.')
                value = self._format(value, new[identity])
                if kind == 'note':
                    self.doc.Bookmarks.Add(_bookmark(identity), value)
            _check(cancelled)
            bibliography = self._control(_BIBLIOGRAPHY).Range
            if str(bibliography.Text) != '\r'.join(map(_plain, previous['rendered']['entries'])):
                raise ValueError('The bibliography changed while updating citations.')
            pieces = []
            for index, entry in enumerate(snapshot['rendered']['entries']):
                if index:
                    pieces.append(('\r', frozenset()))
                pieces.extend((_word(text), styles) for text, styles in _parts(entry))
            self._format_runs(bibliography, pieces)
            _check(cancelled)
            if not self._part().LoadXML(citation_xml(snapshot)):
                raise RuntimeError('Word did not accept the updated citation snapshot.')
            self.capture(snapshot)
