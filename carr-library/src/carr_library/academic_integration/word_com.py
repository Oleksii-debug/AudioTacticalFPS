"""Optional desktop Word bridge for the existing academic citation schema.

Only an explicitly requested new working copy receives edits. The current
formatter's issued source snapshot and result authorize the connection. Word is
attached lazily and is never started, saved, closed or quit by this module.
A pending COM call remains owned by the existing Desktop worker. Cancellation
cannot stop someone else's Word process. An uncertain write blocks further
edits; it is never retried or described as a confirmed rollback.
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

from .documents import (
    CitationParagraph, _MAX_DOCUMENT, _NAMESPACE, _json,
    _read_citation_docx_bytes, _citation_xml, _read_citation_xml,
)
from .formatting import CitationError, CitationCluster, CitationRequest, render_citations
from .regeneration import _references, _restore_clusters
from ..catalog.intake import snapshot as source_snapshot
from ..citations.bibliography_workbench import _parts
from ..durable_state import local_path, regular
from ..research.export_file import capture_export_destination, write_binary_export

_LOCK = threading.Lock()
_RUNTIME = None
_BEFORE = 'carr_word_body_before'
_AFTER = 'carr_word_body_after'
_BIBLIOGRAPHY = 'carr.bibliography.v1'
_PREFIX = 'carr.citation.'
_MAX_TEXT = 4_000_000
_MAX_CONTROLS = 10_000


class WordUnavailable(RuntimeError):
    """No supported running desktop Word session is available."""


class WordRecoveryRequired(RuntimeError):
    """The working-copy result needs explicit inspection; no replay is permitted."""


@dataclass(frozen=True)
class WordCopy:
    path: Path
    original: Path
    original_sha256: str
    original_citation_ids: tuple[str, ...]
    original_reference_ids: tuple[str, ...]


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
    try:
        from ..progress import current_progress
        reporter = current_progress()
        if reporter is not None:
            reporter.update(phase=message)
    except Exception:
        pass


def _check(cancelled):
    if cancelled():
        raise InterruptedError(
            'Word citation action cancelled; inspect the working copy if Word was busy.')


def _word(value):
    return value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\v')


def _plain(markup):
    return _word(''.join(text for text, _ in _parts(markup)))


def _units(text):
    """Word ranges use UTF-16 code units, including both halves of a surrogate."""
    return len(text.encode('utf-16-le')) // 2


def _ids(snapshot):
    return tuple(value['id'] for value in snapshot['clusters'])


def _citations(snapshot):
    return tuple(value['html'] for value in snapshot['citations'])


def _entries(snapshot):
    return tuple(value['html'] for value in snapshot['bibliography'])


def _entry_ids(snapshot):
    return tuple(value['reference_ids'] for value in snapshot['bibliography'])


def _bookmark(identity):
    return 'carr_' + sha256(identity.encode('utf-8')).hexdigest()[:30]


def _original_bookmark(snapshot, identity):
    # Exactly the existing schema-1 DOCX writer's occurrence spelling.
    return _bookmark('citation:' + identity + ':' + str(_ids(snapshot).index(identity) + 1))


def _tag(identity):
    return _PREFIX + sha256(identity.encode('utf-8')).hexdigest()[:40]


def _directory_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode,
            getattr(info, 'st_file_attributes', 0))


def _file_identity(info):
    return (*_directory_identity(info), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise ValueError('Choose a regular local DOCX path.')
    win = PureWindowsPath(raw)
    parts = [part for part in win.parts if part != win.anchor]
    if (not raw or raw.startswith(('\\\\', '//', '\\??\\'))
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
    path = local_path(path, exists=True)
    regular(path)
    return path


def _read(path, cancelled):
    path = _path(path)
    return source_snapshot(path.parent, path.name, max_bytes=_MAX_DOCUMENT,
                           cancelled=cancelled).data


def _safe_package(payload):
    """Refuse active/external content before Word opens the captured package."""
    from defusedxml import ElementTree
    snapshot = _read_citation_docx_bytes(payload)
    allowed_relationships = {
        'officeDocument', 'core-properties', 'extended-properties', 'custom-properties',
        'styles', 'stylesWithEffects', 'settings', 'webSettings', 'fontTable', 'theme',
        'numbering', 'customXml', 'customXmlProps', 'endnotes', 'thumbnail',
    }
    with ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        xml_bytes = 0
        metadata_name, property_links = None, []
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
            if (info.filename.startswith('customXml/') and info.filename.count('/') == 1
                    and info.filename.endswith('.xml')):
                node = ElementTree.fromstring(archive.read(info), forbid_dtd=True,
                    forbid_entities=True, forbid_external=True)
                if node.tag == '{%s}citations' % _NAMESPACE:
                    metadata_name = info.filename
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
                    components = list(PurePosixPath(info.filename).parts[:-2])
                    for component in target.split('/'):
                        if component == '..':
                            if not components:
                                raise ValueError('Word relationship escapes the captured package.')
                            components.pop()
                        elif component not in ('', '.'):
                            components.append(component)
                    target_name = '/'.join(components)
                    if target_name not in names:
                        raise ValueError('Word relationship does not name a captured local part.')
                    if relation == 'customXmlProps':
                        source_parts = PurePosixPath(info.filename).parts
                        if (len(source_parts) < 2 or source_parts[-2] != '_rels'
                                or not source_parts[-1].endswith('.rels')):
                            raise ValueError('The Word datastore relationship is invalid.')
                        source_name = '/'.join((*source_parts[:-2], source_parts[-1][:-5]))
                        property_links.append((source_name, target_name))
        # A renamed Word part must retain one unique standard datastore identity.
        # Other package custom XML is inert and cannot declare our namespace.
        from uuid import UUID
        import re
        namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/customXml'
        seen, managed = set(), []
        for source_name, target_name in property_links:
            info = archive.getinfo(target_name)
            if info.file_size > 1024 * 1024:
                raise ValueError('Word datastore properties exceed their byte budget.')
            node = ElementTree.fromstring(archive.read(info), forbid_dtd=True,
                forbid_entities=True, forbid_external=True)
            identity = node.get('{%s}itemID' % namespace, '')
            if (node.tag != '{%s}datastoreItem' % namespace
                    or re.fullmatch(r'\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}', identity) is None):
                raise ValueError('Word datastore identity is invalid.')
            identity = UUID(identity.strip('{}'))
            if identity in seen:
                raise ValueError('Word datastore identities must be unique.')
            seen.add(identity)
            refs = [child.get('{%s}uri' % namespace)
                    for child in node.iter('{%s}schemaRef' % namespace)]
            if source_name == metadata_name:
                if (set(node.attrib) != {'{%s}itemID' % namespace}
                        or len(node) != 1 or node[0].tag != '{%s}schemaRefs' % namespace
                        or node[0].attrib or len(node[0]) != 1
                        or node[0][0].tag != '{%s}schemaRef' % namespace
                        or set(node[0][0].attrib) != {'{%s}uri' % namespace}
                        or len(node[0][0]) or refs != [_NAMESPACE]):
                    raise ValueError('The CARR Word datastore properties changed.')
                managed.append(identity)
            elif _NAMESPACE in refs:
                raise ValueError('A foreign Word datastore declares the CARR namespace.')
        if len(managed) != 1:
            raise ValueError('Export a current CARR DOCX with one native Word datastore identity.')
    return snapshot


def _body(snapshot):
    document = snapshot.get('document')
    if (type(document) is not dict or set(document) != {'title', 'paragraphs'}
            or type(document['title']) is not str or not document['title'].strip()
            or len(document['title']) > 255 or type(document['paragraphs']) is not list
            or len(document['paragraphs']) > 10_000):
        raise CitationError('The saved citation document body is invalid.')
    body, size = [], 0
    for row in document['paragraphs']:
        if (type(row) is not dict or set(row) != {'text', 'citation_ids'}
                or type(row['text']) is not str or len(row['text']) > 200_000
                or type(row['citation_ids']) is not list or len(row['citation_ids']) > 100):
            raise CitationError('The saved citation paragraph is invalid.')
        size += len(row['text'])
        if size > _MAX_TEXT:
            raise CitationError('The saved citation body exceeds its text budget.')
        body.append(CitationParagraph(row['text'], tuple(row['citation_ids'])))
    if [identity for row in body for identity in row.citation_ids] != list(_ids(snapshot)):
        raise CitationError('The saved citation body order differs from its citation identities.')
    return document['title'], tuple(body)


def _with_body(result, paragraphs, title):
    snapshot = result.interchange
    snapshot['document'] = {
        'title': title,
        'paragraphs': [{'text': row.text, 'citation_ids': list(row.citation_ids)}
                       for row in paragraphs],
    }
    _body(snapshot)
    if len(_json(snapshot)) > 16_000_000:
        raise CitationError('The citation document provenance exceeds its size budget.')
    return snapshot


def _validated(snapshot, cancelled):
    if type(snapshot) is not dict or type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
        raise CitationError('The saved citation document uses an unsupported schema.')
    rows = _references(snapshot.get('references'))
    if rows != snapshot['references']:
        raise CitationError('The saved references are not the canonical processing copy.')
    clusters = _restore_clusters(snapshot.get('clusters'), {row['id'] for row in rows})
    title, paragraphs = _body(snapshot)
    style = snapshot.get('style')
    if type(style) is not dict or type(style.get('id')) is not str:
        raise CitationError('The saved citation style is invalid.')
    # This optional host path accepts the bundled styles already selectable by
    # the existing formatter. No custom-file or document-supplied style is run.
    result = render_citations(rows, style=style['id'], locale=snapshot.get('locale'),
                              citations=clusters, cancelled=cancelled)
    expected = _with_body(result, paragraphs, title)
    if _json(expected) != _json(snapshot):
        raise CitationError('The saved citation snapshot differs from the current pinned formatter.')
    if not _ids(snapshot) or any(not _plain(value).strip()
            for value in (*_citations(snapshot), *_entries(snapshot))):
        raise CitationError('Word citation controls require nonblank citations and bibliography.')
    return clusters


def _result(copy, snapshot, changed=False):
    return WordResult(copy.path, _ids(snapshot),
                      tuple(row['id'] for row in snapshot['references']),
                      snapshot['style']['kind'], changed)


def _detached_failure(failure):
    """Do not let a COM traceback retain native proxies past this STA scope."""
    import traceback
    traceback.clear_frames(failure.__traceback__)
    message = str(failure)[:2000]
    for kind in (WordRecoveryRequired, WordUnavailable, InterruptedError,
                 FileExistsError, CitationError, ValueError, OSError,
                 KeyboardInterrupt, SystemExit):
        if isinstance(failure, kind):
            return kind(message)
    return RuntimeError('Word rejected the current action: ' + message)


def prepare_word_runtime():
    """Initialize the import-owned COM lifetime on the long-lived main thread.

    Called only by the explicit Word UI action or native qualification command.
    It does not attach to or start Word. Worker calls balance only their own STA
    initialization. An unowned prior import is refused rather than guessing
    which thread owns comtypes' automatic first-import initialization.
    """
    global _RUNTIME
    if os.name != 'nt':
        raise WordUnavailable('Desktop Word automation is available only on Windows.')
    if threading.current_thread() is not threading.main_thread():
        raise WordUnavailable('Initialize the optional Word runtime from the desktop main thread.')
    import sys
    if _RUNTIME is not None:
        runtime, attach = _RUNTIME
        if sys.modules.get('comtypes') is not runtime:
            raise WordUnavailable('The initialized Word runtime changed.')
        return
    if 'comtypes' in sys.modules:
        raise WordUnavailable(
            'COM was initialized outside this Word action. Restart the desktop and open Word controls first.')
    try:
        import comtypes
        from comtypes.client import GetActiveObject
    except ImportError as exc:
        raise WordUnavailable('The installed Windows Word automation dependency is unavailable.') from exc
    # comtypes 1.5.0 balances its first-import initialization at normal process
    # shutdown. Keeping that import on main matches its registered exit owner.
    _RUNTIME = (comtypes, GetActiveObject)


@contextmanager
def _running_word():
    if os.name != 'nt':
        raise WordUnavailable('Desktop Word automation is available only on Windows.')
    initialized = False
    application = session = failure = None
    try:
        import sys
        if _RUNTIME is None or sys.modules.get('comtypes') is not _RUNTIME[0]:
            raise WordUnavailable('Open Word controls on the desktop before running a Word action.')
        comtypes, GetActiveObject = _RUNTIME
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        initialized = True
        try:
            application = GetActiveObject('Word.Application', dynamic=True)
        except comtypes.COMError as exc:
            if (int(exc.hresult) & 0xffffffff) in (0x800401e3, 0x80040154):
                raise WordUnavailable(
                    'Open desktop Microsoft Word and a saved CARR citation DOCX first.') from exc
            raise
        session = _WordApplication(application)
        application = None
        yield session
    except ImportError as exc:
        failure = WordUnavailable('The installed Windows Word automation dependency is unavailable.')
        import traceback
        traceback.clear_frames(exc.__traceback__)
    except BaseException as exc:
        failure = _detached_failure(exc)
    finally:
        if session is not None:
            session._release()
        session = None
        application = None
        if initialized:
            comtypes.CoUninitialize()
    if failure is not None:
        raise failure from None


class WordBridge:
    """Synchronous commands executed only by the existing Desktop worker."""

    def __init__(self, controller, snapshot, result, *, connector=None):
        from ..ui.citation_formatting import CitationFormattingController, CitationSnapshot
        from .formatting import FormattedCitations
        if (type(controller) is not CitationFormattingController
                or type(snapshot) is not CitationSnapshot or type(result) is not FormattedCitations
                or result is not controller._result or snapshot.sha256 != controller._result_snapshot):
            raise ValueError('Format and review the current library references before connecting Word.')
        self.controller, self.snapshot, self.result = controller, snapshot, result
        if connector is None:
            prepare_word_runtime()
        self.connector = connector or _running_word
        self._root = local_path(controller.library.root, exists=True)
        self._root_identity = _directory_identity(regular(self._root, directory=True))
        self._copy = self._known = self._previous = self._owner = None
        self._uncertain = False

    @property
    def connected_copy(self):
        return self._copy

    @property
    def recovery_required(self):
        return self._uncertain

    def _permission(self, cancelled):
        _check(cancelled)
        root = local_path(self.controller.library.root, exists=True)
        if root != self._root or _directory_identity(regular(root, directory=True)) != self._root_identity:
            raise ValueError('The reviewed library root changed; reconnect from the current formatter.')
        self.controller._check(self.snapshot, cancelled)
        if _directory_identity(regular(root, directory=True)) != self._root_identity:
            raise ValueError('The reviewed library root changed during source verification.')
        _check(cancelled)

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

    def connect(self, destination, *, approval=None, cancelled=lambda: False):
        """Create a new copy of the exact currently reviewed schema-1 DOCX."""
        self._permission(cancelled)
        if self._copy is not None:
            raise ValueError('This dialog already owns a working copy; inspect it or close this dialog.')
        _phase('Checking the saved Word document and current reviewed sources')
        with self._application(cancelled) as application:
            source = application.active()
            original = source.path
            source.require_editable(saved=True)
            payload = _read(original, cancelled)
            snapshot = _safe_package(payload)
            _validated(snapshot, cancelled)
            if {key: value for key, value in snapshot.items() if key != 'document'} != self.result.interchange:
                raise CitationError(
                    'The saved document differs from the current reviewed formatting result. '
                    'Export that result, open and save it in Word, then connect again.')
            source.preflight(snapshot)
            before, source_owner = source.signature(), source.owner()
            digest, original_identity = sha256(payload).hexdigest(), _file_identity(regular(original))
            if approval is None:
                approval = capture_export_destination(destination, private_root=self._root,
                    _reject_existing=FileExistsError('Choose a new Word filename.'))
            if not approval.require_absent:
                raise FileExistsError('Choose a new filename; the original is never overwritten.')
            if Path(destination).suffix.lower() != '.docx':
                raise ValueError('Choose a new .docx filename for the working copy.')

            def still_current():
                self._permission(cancelled)
                source.require_editable(saved=True)
                active = application.active()
                if (active.path != original or active.owner() != source_owner
                        or source.owner() != source_owner or source.signature() != before
                        or _file_identity(regular(original)) != original_identity
                        or sha256(_read(original, cancelled)).hexdigest() != digest):
                    raise ValueError('The original Word document changed; review it again.')
                return False

            _phase('Publishing a new Word working copy')
            write_binary_export(destination, payload, private_root=self._root,
                                require_absent=True, approval=approval, cancelled=still_current)
            target = _path(destination)
            self._permission(cancelled)
            copied_identity = _file_identity(regular(target))
            if sha256(_read(target, cancelled)).hexdigest() != digest:
                raise ValueError('The newly published Word copy changed before opening.')
            copy = WordCopy(target, original, digest, _ids(snapshot),
                            tuple(row['id'] for row in snapshot['references']))
            # Retain the created-copy capability even if the external open or
            # adoption outcome becomes uncertain. Never retry the same mutation.
            self._copy, self._known, self._uncertain = copy, snapshot, True
            _phase('Opening and binding Word citation controls')
            try:
                document = application.open_copy(target)
                if (document.path != target or _file_identity(regular(target)) != copied_identity
                        or sha256(_read(target, cancelled)).hexdigest() != digest):
                    raise ValueError('Word opened a different or changed document.')
                self._owner = document.owner()
                document.preflight(snapshot)
                def permitted():
                    self._permission(cancelled)
                    if (application.active().owner() != self._owner
                            or document.path != copy.path or document.owner() != self._owner):
                        raise ValueError('The active working-copy identity changed.')
                    return False
                document.adopt(snapshot, cancelled=permitted)
                # Cancellation arriving after a completed mutation does not
                # turn that completed result into an invented rollback.
                self._permission(lambda: False)
                document.capture(snapshot)
                self._uncertain = False
            except BaseException as exc:
                raise WordRecoveryRequired(
                    'The new working copy exists, but Word connection was not confirmed. '
                    'Inspect it before saving; the original remains unchanged.') from exc
            return copy

    def _current(self, application, copy, *, inspect=False):
        if copy is not self._copy or type(copy) is not WordCopy or copy.path == copy.original:
            raise ValueError('Use the working-copy capability issued by this dialog.')
        if self._uncertain and not inspect:
            raise WordRecoveryRequired('Inspect the uncertain working-copy action before another edit.')
        document = application.active()
        if document.path != copy.path or self._owner is None or document.owner() != self._owner:
            raise ValueError('Activate the same connected Word window before this action.')
        document.require_editable()
        snapshot = document.snapshot()
        candidates = [self._known]
        if inspect and self._previous is not None:
            candidates.append(self._previous)
        known = next((value for value in candidates if _json(value) == _json(snapshot)), None)
        if known is None:
            raise WordRecoveryRequired('The working-copy metadata no longer matches an issued action.')
        document.capture(known)
        return document, known

    def completed_result(self, copy):
        """Return only the already confirmed result; this makes no host call."""
        if copy is not self._copy or self._uncertain or self._known is None:
            raise WordRecoveryRequired('The working-copy action has not been confirmed.')
        return _result(copy, self._known)

    def inspect(self, copy, *, cancelled=lambda: False):
        self._permission(cancelled)
        with self._application(cancelled) as application:
            document, snapshot = self._current(application, copy, inspect=True)
            self._permission(cancelled)
            document.capture(snapshot)
            self._known, self._uncertain = snapshot, False
            return _result(copy, snapshot)

    def change(self, copy, *, items=None, occurrence_id=None,
               insert=False, cancelled=lambda: False):
        """Insert/edit explicit typed requests, or regenerate all managed citations."""
        self._permission(cancelled)
        if type(insert) is not bool or (insert and occurrence_id is not None):
            raise ValueError('Choose one insertion or one existing citation.')
        if (insert or occurrence_id is not None) != (items is not None):
            raise ValueError('Insertion and editing require explicit citation items.')
        if items is not None and (type(items) is not tuple or not items
                                  or any(type(item) is not CitationRequest for item in items)):
            raise CitationError('Choose current typed citation requests with explicitly reviewed locators.')
        with self._application(cancelled) as application:
            document, snapshot = self._current(application, copy)
            view = document.capture(snapshot, insertion=insert)
            before = document.signature()
            saved = _restore_clusters(snapshot['clusters'],
                                      {row['id'] for row in snapshot['references']})
            ordered = {cluster.id: cluster for cluster in saved}
            clusters = [ordered[identity] for identity in view['ids']]
            insertion = None
            if insert:
                identity = 'citation-' + uuid4().hex
                insertion = (view['caret'], identity)
                index = sum(start < view['caret'] for _, start, _ in view['anchors'])
                clusters.insert(index, CitationCluster(items, identity))
            elif occurrence_id is not None:
                if occurrence_id not in ordered:
                    raise ValueError('Choose an existing citation occurrence.')
                clusters = [CitationCluster(items, cluster.id, cluster.note_index)
                            if cluster.id == occurrence_id else cluster for cluster in clusters]
            if snapshot['style']['kind'] == 'note':
                clusters = [CitationCluster(cluster.items, cluster.id, index)
                            for index, cluster in enumerate(clusters, 1)]
            current = {row['id']: row for row in _references(self.snapshot.references)}
            rows = list(snapshot['references'])
            if any(current.get(row['id']) != row for row in rows):
                raise CitationError('The document sources changed; review and reconnect from a fresh export.')
            existing = {row['id'] for row in rows}
            for cluster in clusters:
                for item in cluster.items:
                    if item.reference_id not in current:
                        raise CitationError('The citation names a source outside the current reviewed library.')
                    if item.reference_id not in existing:
                        rows.append(current[item.reference_id])
                        existing.add(item.reference_id)
            body = document.paragraphs(snapshot, insertion=insertion)
            _phase('Formatting the current local citation snapshot')
            formatted = render_citations(rows, style=snapshot['style']['id'],
                locale=snapshot['locale'], citations=tuple(clusters), cancelled=cancelled)
            rendered = _with_body(formatted, body, snapshot['document']['title'])
            if any(not _plain(value).strip() for value in (*_citations(rendered), *_entries(rendered))):
                raise CitationError('Word citation controls require nonblank rendered content.')
            self._permission(cancelled)
            if (application.active().path != copy.path
                    or application.active().owner() != self._owner
                    or document.owner() != self._owner or document.signature() != before):
                raise ValueError('The Word document or selection changed during formatting; review it again.')
            def permitted():
                self._permission(cancelled)
                if (application.active().owner() != self._owner
                        or document.path != copy.path or document.owner() != self._owner):
                    raise ValueError('The active working-copy identity changed.')
                return False
            self._previous, self._uncertain = snapshot, True
            # Retain both exact potential postimages for explicit inspection.
            self._known = rendered
            _phase('Applying one native undo group to the working copy')
            try:
                document.apply(snapshot, rendered, insertion=insertion, cancelled=permitted)
                self._permission(lambda: False)
                document.capture(rendered)
                if document.owner() != self._owner:
                    raise ValueError('The working-copy window changed before completion.')
                self._uncertain = False
            except BaseException as exc:
                raise WordRecoveryRequired(
                    'Word did not confirm the complete action. Do not repeat it. '
                    'Inspect the working copy or close it without saving; the original is unchanged.') from exc
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

    def owner(self):
        windows = _items(self.doc.Windows, 2)
        if len(windows) != 1:
            raise ValueError('Use one window for the connected Word document.')
        return (int(self.app.Hwnd), int(windows[0].Hwnd), str(self.path))

    def require_editable(self, *, saved=False):
        if (bool(self.doc.ReadOnly) or int(self.doc.ProtectionType) != -1
                or bool(self.doc.TrackRevisions) or int(self.doc.Revisions.Count)
                or int(self.doc.Tables.Count) or int(self.doc.Fields.Count)
                or int(self.doc.Footnotes.Count) or int(self.doc.Shapes.Count)
                or int(self.doc.InlineShapes.Count)):
            raise ValueError('Use an editable CARR document without tables, fields, tracked changes, shapes or footnotes.')
        if saved and not bool(self.doc.Saved):
            raise ValueError('Save the original in Word before creating a working copy.')
        if bool(self.app.UndoRecord.IsRecordingCustomRecord):
            raise ValueError('Finish the current Word undo operation before using citations.')
        if int(self.doc.Content.End) - int(self.doc.Content.Start) > _MAX_TEXT:
            raise ValueError('The Word document exceeds its text budget.')
        if len(str(self.doc.Content.Text)) > _MAX_TEXT:
            raise ValueError('The Word document exceeds its text budget.')

    def _part(self):
        parts = _items(self.doc.CustomXMLParts.SelectByNamespace(_NAMESPACE), 2)
        if len(parts) != 1:
            raise ValueError('The Word document has no unique CARR citation snapshot.')
        return parts[0]

    def snapshot(self):
        return _read_citation_xml(str(self._part().XML))

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
        # Raw XML remains readable for a bounded signature even if a partial
        # host write cannot yet be interpreted as a completed citation snapshot.
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
        if len(paragraphs) < len(body) + len(_entries(snapshot)) + 5:
            raise ValueError('The saved citation document layout changed.')
        if (str(paragraphs[0].Range.Text) != _word(snapshot['document']['title']) + '\r'
                or str(paragraphs[1].Range.Text) != _word(snapshot['style']['title']) + '\r'
                or str(paragraphs[2 + len(body)].Range.Text) != 'Bibliography\r'):
            raise ValueError('Connect an editable CARR citation export with its original body headings.')
        texts = dict(zip(_ids(snapshot), map(_plain, _citations(snapshot))))
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
                if not self.doc.Bookmarks.Exists(_original_bookmark(snapshot, identity)):
                    raise ValueError('An original citation occurrence bookmark is missing.')
                if snapshot['style']['kind'] != 'note':
                    prior = self.doc.Bookmarks.Item(_original_bookmark(snapshot, identity)).Range
                    if (int(prior.StoryType) != 1 or int(prior.Start) != int(paragraph.Range.Start)
                            or int(prior.End) != int(paragraph.Range.End) - 1):
                        raise ValueError('An original citation bookmark moved to another paragraph.')
            if str(paragraph.Range.Text) != value + '\r':
                raise ValueError('Visible citation-document body edits need review before initial connection.')
        notes = _items(self.doc.Endnotes)
        if snapshot['style']['kind'] == 'note':
            if len(notes) != len(texts):
                raise ValueError('Unmanaged or missing native endnotes cannot be adopted.')
            for index, identity in enumerate(_ids(snapshot)):
                note = notes[index]
                original = self.doc.Bookmarks.Item(_original_bookmark(snapshot, identity)).Range
                if (int(original.StoryType) != 3 or int(original.End)
                        not in (int(note.Range.End), int(note.Range.End) - 1)):
                    raise ValueError('The original endnote occurrence bookmark moved.')
                ranges[identity] = self._note_text(note, texts[identity])
        elif notes:
            raise ValueError('This in-text style does not own native endnotes.')
        offset = 3 + len(body)
        entries = _entries(snapshot)
        if not entries:
            raise ValueError('The Word document needs a nonempty bibliography.')
        for index, (markup, ids) in enumerate(zip(entries, _entry_ids(snapshot))):
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
        undo = self.app.UndoRecord
        started = ending = False
        try:
            undo.StartCustomRecord(name)
            started = True
            yield
            ending = True
            undo.EndCustomRecord()
            started = False
        except BaseException as failure:
            # Never undo or replay an ambiguous external write. If our record
            # has not yet been ended, make one cleanup attempt and keep the
            # uncertain result visible even when that attempt returns normally.
            if started and not ending:
                try:
                    undo.EndCustomRecord()
                except BaseException:
                    pass
            raise WordRecoveryRequired(
                'The native Word action was not confirmed. Inspect the working copy; '
                'no automatic Undo or retry was performed.') from failure

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
        texts = dict(zip(_ids(snapshot), map(_plain, _citations(snapshot))))
        kind, anchors = snapshot['style']['kind'], []
        expected_tags = {_BIBLIOGRAPHY}
        for identity in _ids(snapshot):
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
                or str(biblio.Text) != '\r'.join(map(_plain, _entries(snapshot)))):
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
        old = dict(zip(_ids(previous), map(_plain, _citations(previous))))
        new = dict(zip(_ids(snapshot), _citations(snapshot)))
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
            for identity in _ids(snapshot):
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
            if str(bibliography.Text) != '\r'.join(map(_plain, _entries(previous))):
                raise ValueError('The bibliography changed while updating citations.')
            pieces = []
            for index, entry in enumerate(_entries(snapshot)):
                if index:
                    pieces.append(('\r', frozenset()))
                pieces.extend((_word(text), styles) for text, styles in _parts(entry))
            self._format_runs(bibliography, pieces)
            _check(cancelled)
            if not self._part().LoadXML(_citation_xml(snapshot).decode('utf-8')):
                raise RuntimeError('Word did not accept the updated citation snapshot.')
            self.capture(snapshot)
