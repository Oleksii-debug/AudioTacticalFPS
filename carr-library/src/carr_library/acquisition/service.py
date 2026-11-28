"""Source-preserving DOI workflows composed with the existing library services.

Lookup previews are bound to this application instance and one active workspace.
Only reviewed metadata is saved; provider statements never verify source text,
physical pages, a version match, or the right to redistribute a document.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory, gettempdir
from threading import Lock, RLock
from time import monotonic
from urllib.parse import quote, urlsplit
from uuid import uuid4

from ..catalog.membership import Catalog
from ..data_paths import guard_data_directory
from ..durable_state import regular, sync_directory as _sync_directory
from ..operations import ResourceBroker, ResourceRequest
from ..research.academic_library import AcademicLibrary, normalize_doi, normalize_isbn, _isbn_key
from . import providers as provider_api
from .models import MetadataPreview, DOI_PROVIDERS
from .transport import (NetworkConsent, SafeTransport, PROVIDER_HOSTS,
                        MAX_DOWNLOAD_BYTES, validate_https_url)


@dataclass(frozen=True)
class AcquisitionPreview:
    token: str
    workspace: str
    doi: str
    metadata: MetadataPreview
    provider_records: tuple
    sources: tuple
    warnings: tuple[str, ...]
    fetched_utc: str
    cached: bool = False
    identifier_kind: str = 'doi'
    identifier: str = ''


@dataclass(frozen=True)
class AcquisitionResult:
    path: Path
    receipt_path: Path
    reference_id: str | None
    edition_id: str | None
    state: str
    message: str
    sha256: str


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Acquisition cancelled.')


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _preview_bytes(preview):
    return sum(len(record.raw_json) for record in preview.provider_records)


def _write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _identity(path):
    value = Path(path).lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise ValueError('The acquisition destination changed.')
    return value.st_dev, value.st_ino


def _unlink_owned(path, identity):
    """A failed attempt cannot remove another writer's replacement."""
    try:
        if _identity(path) != identity:
            return False
        path.unlink()
        return True
    except (FileNotFoundError, OSError, ValueError):
        return False


def _attest(path, expected_digest, expected_size, cancelled):
    """Bounded, cancellable read of the exact newly acquired regular file."""
    if (type(expected_size) is not int or not 0 < expected_size <= MAX_DOWNLOAD_BYTES
            or not isinstance(expected_digest, str) or len(expected_digest) != 64):
        raise ValueError('The downloaded file has an invalid size or digest.')
    before = regular(path)
    if before.st_size != expected_size:
        raise ValueError('The acquired file size changed.')
    digest = sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('The acquired file changed before validation.')
        remaining = expected_size
        while remaining:
            _cancel(cancelled)
            block = stream.read(min(65536, remaining))
            if not block:
                raise ValueError('The acquired file ended before its declared size.')
            digest.update(block)
            remaining -= len(block)
        _cancel(cancelled)
        if stream.read(1):
            raise ValueError('The acquired file exceeds its declared size.')
    after = regular(path)
    state = lambda value: (value.st_dev, value.st_ino, value.st_size,
                           value.st_mtime_ns, value.st_ctime_ns)
    if state(before) != state(after) or digest.hexdigest() != expected_digest:
        raise ValueError('The acquired file changed or failed its content digest.')
    return before.st_dev, before.st_ino


def _publish_file(source, target, cancelled):
    """Existing create-only pair publication, including FAT/exFAT fallback."""
    source_identity = _identity(source)
    try:
        os.link(source, target)
        return source_identity
    except OSError as exc:
        if isinstance(exc, FileExistsError):
            raise
        # Some filesystem failures arrive after the link was committed. Only
        # the exact source inode may establish that this is our publication.
        if os.path.lexists(target) and _identity(target) == source_identity:
            return source_identity
        if not (exc.errno in {errno.EPERM, errno.EXDEV, errno.ENOSYS,
                              getattr(errno, 'EOPNOTSUPP', errno.ENOSYS)}
                or getattr(exc, 'winerror', None) in (1, 50, 1314)):
            raise
    identity = None
    try:
        with target.open('xb') as output:
            value = os.fstat(output.fileno())
            identity = value.st_dev, value.st_ino
            with source.open('rb') as stream:
                while True:
                    _cancel(cancelled)
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
            _cancel(cancelled)
            output.flush()
            os.fsync(output.fileno())
        return identity
    except BaseException:
        if identity is not None:
            _unlink_owned(target, identity)
        raise


def _rollback(target, directory_identity, marker, marker_identity, owned):
    """Keep an incomplete marker if any foreign or uncertain file remains."""
    try:
        if _identity(target) != directory_identity:
            return
        for path, identity in reversed(owned):
            _unlink_owned(path, identity)
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.name != marker.name:
                    return
        if marker_identity is not None and not _unlink_owned(marker, marker_identity):
            return
        target.rmdir()
    except (OSError, ValueError):
        pass


def _destination(value, library_root):
    path = guard_data_directory(value, purpose='acquired document').absolute()
    if path == library_root or path.is_relative_to(library_root):
        raise ValueError('Choose a download folder outside the private library data directory.')
    if (not path.name or len(path.name) > 120 or path.name[-1:] in ('.', ' ')
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in path.name)
            or path.stem.upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Choose a portable new folder name, without reserved characters.')
    parent = path.parent
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('Choose a local download directory.')
    for ancestor in (parent, *parent.parents):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Choose an existing local folder without symbolic links or junctions.')
    if parent.resolve(strict=True) != parent or os.path.lexists(path):
        raise ValueError('The destination must be a new folder; existing folders are never overwritten.')
    return path


class AcquisitionService:
    """An in-memory capability/cache plus durable existing reference/import APIs."""
    def __init__(self, app, *, transport_factory=SafeTransport, clock=monotonic, resource_broker=None):
        self.app = app
        self.transport_factory = transport_factory
        self.clock = clock
        self._resource_broker = resource_broker
        self._previews = {}
        self._cache = {}
        self._lock = RLock()
        self._lookup_lock = Lock()

    def _scope(self, workspace, cancelled=lambda: False, collection=None):
        _cancel(cancelled)
        with self.app.read(workspace) as db:
            if collection is not None:
                Catalog(db, workspace)._active_collection(collection)
        _cancel(cancelled)

    def _checked(self, workspace, preview, cancelled):
        self._scope(workspace, cancelled)
        with self._lock:
            if (not isinstance(preview, AcquisitionPreview) or preview.workspace != workspace
                    or self._previews.get(preview.token) is not preview):
                raise ValueError('This metadata preview is stale or belongs to another library. Look up the identifier again.')
        return preview

    def lookup(self, workspace, doi, *, online_allowed=False, providers=('crossref',),
               unpaywall_email='', cancelled=lambda: False):
        return self._lookup(workspace, doi, identifier_kind='doi', online_allowed=online_allowed,
            providers=providers, unpaywall_email=unpaywall_email, cancelled=cancelled)

    def lookup_isbn(self, workspace, isbn, *, online_allowed=False, cancelled=lambda: False):
        return self._lookup(workspace, isbn, identifier_kind='isbn', online_allowed=online_allowed,
            providers=('openlibrary',), cancelled=cancelled)

    def _lookup(self, workspace, identifier, *, identifier_kind, online_allowed=False,
                providers=('crossref',), unpaywall_email='', cancelled=lambda: False):
        self._scope(workspace, cancelled)
        if online_allowed is not True:
            raise PermissionError('Online metadata lookup is disabled. Approve this request explicitly.')
        identifier = (normalize_doi if identifier_kind == 'doi' else normalize_isbn)(identifier)
        if not identifier:
            raise ValueError('Enter a valid DOI or ISBN to look up.')
        allowed_providers = ('openlibrary',) if identifier_kind == 'isbn' else DOI_PROVIDERS
        if (not isinstance(providers, (tuple, list)) or not providers or len(providers) > len(allowed_providers)
                or any(not isinstance(provider, str) or provider not in allowed_providers for provider in providers)
                or len(set(providers)) != len(providers)):
            raise ValueError('Select each supported metadata provider at most once for this identifier type.')
        providers = tuple(providers)
        config = provider_api.ProviderConfig(unpaywall_email=unpaywall_email)
        if not isinstance(unpaywall_email, str):
            raise ValueError('Unpaywall requires a valid contact email.')
        key = (workspace, identifier_kind, identifier, providers, sha256(unpaywall_email.encode('utf-8')).hexdigest())
        if not self._lookup_lock.acquire(blocking=False):
            raise ValueError('A metadata lookup is already running. Cancel it or wait for it to finish.')
        try:
            with self._lock:
                cached = self._cache.get(key)
            if cached and self.clock() - cached[0] < 600:
                self._scope(workspace, cancelled)
                preview = replace(cached[1], token=uuid4().hex, cached=True)
            else:
                records, warnings = [], []
                transport = self.transport_factory(NetworkConsent(True, 'metadata',
                    tuple(PROVIDER_HOSTS[p] for p in providers), timeout=60.0))
                for provider in providers:
                    self._scope(workspace, cancelled)
                    try:
                        if identifier_kind == 'isbn':
                            record = provider_api.lookup_isbn(identifier, transport=transport,
                                consent=True, cancelled=cancelled)
                        else:
                            record = provider_api.lookup_doi(provider, identifier, transport=transport,
                                consent=True, config=config, cancelled=cancelled)
                    except (InterruptedError, TimeoutError):
                        raise
                    except (ValueError, OSError):
                        # Provider exceptions already use fixed messages. Do not
                        # include external transport exception text or URLs here.
                        warnings.append(f'{provider}: lookup unavailable or the response could not be validated.')
                        continue
                    records.append(record)
                    warnings.extend(record.warnings)
                if not records:
                    raise ValueError('No selected provider returned valid metadata for this exact identifier. '
                                     'Check the DOI or ISBN, connection, and any required contact email.')
                # Prefer Crossref for bibliographic metadata but never splice
                # mismatched fields into a fictitious combined publication.
                metadata = next((r for r in records if r.provider == 'crossref'), records[0])
                sources = []
                seen = set()
                for record in records:
                    for source in record.locations:
                        identity = source.id
                        if identity not in seen:
                            sources.append(source)
                            seen.add(identity)
                if len({r.title for r in records if r.title}) > 1:
                    warnings.append('Providers returned different titles. Review the edition and author list before using metadata.')
                warnings.append('Online metadata and access statements are provider claims; they do not verify source text or citation page numbers.')
                preview = AcquisitionPreview(uuid4().hex, workspace, identifier if identifier_kind == 'doi' else '',
                    metadata, tuple(records), tuple(sources), tuple(dict.fromkeys(warnings)), _utc(),
                    identifier_kind=identifier_kind, identifier=identifier)
                self._scope(workspace, cancelled)
                with self._lock:
                    self._cache[key] = (self.clock(), preview)
                    while (len(self._cache) > 64 or
                           sum(_preview_bytes(item[1]) for item in self._cache.values()) > 16 * 1024 * 1024):
                        self._cache.pop(next(iter(self._cache)))
            with self._lock:
                self._previews[preview.token] = preview
                while (len(self._previews) > 128 or
                       sum(_preview_bytes(item) for item in self._previews.values()) > 24 * 1024 * 1024):
                    self._previews.pop(next(iter(self._previews)))
            return preview
        finally:
            self._lookup_lock.release()

    @staticmethod
    def _provenance(preview):
        return {'schema': 'research-library-acquisition-v1', 'doi': preview.doi,
            'identifier_kind': preview.identifier_kind,
            'identifier': preview.identifier or preview.doi,
            'isbn': list(preview.metadata.isbn),
            'retrieved_utc': preview.fetched_utc,
            'metadata_provider': preview.metadata.provider,
            'metadata_source_url': preview.metadata.source_url,
            'provider_responses': [{'provider': r.provider, 'sha256': r.raw_sha256}
                                   for r in preview.provider_records],
            'source_text_verified': False,
            'note': 'Provider metadata is not an independent source or quotation review.'}

    @staticmethod
    def _fields(preview):
        meta = preview.metadata
        isbn = ''
        for candidate in meta.isbn:
            try:
                isbn = normalize_isbn(candidate)
            except ValueError:
                continue
            if isbn:
                break
        return dict(title=meta.title, authors=meta.authors, item_type=meta.item_type,
            year=meta.year, container_title=meta.container_title, publisher=meta.publisher,
            volume=meta.volume, issue=meta.issue, pages=meta.pages, doi=preview.doi,
            isbn=isbn, url=('https://doi.org/' + quote(preview.doi, safe='/')
                            if preview.doi else meta.source_url), abstract=meta.abstract,
            notes='Acquisition provenance (metadata only):\n' + json.dumps(
                AcquisitionService._provenance(preview), ensure_ascii=False, sort_keys=True))

    def save_reference(self, workspace, preview, *, cancelled=lambda: False):
        preview = self._checked(workspace, preview, cancelled)
        with self.app.write(workspace) as db:
            _cancel(cancelled)
            library = AcademicLibrary(db, workspace)
            if preview.doi:
                rows = tuple(db.execute('SELECT id FROM academic_reference '
                    'WHERE workspace=? AND doi=? AND archived=0 ORDER BY id LIMIT 2', (workspace, preview.doi)))
            else:
                isbn = normalize_isbn(preview.identifier)
                if preview.identifier_kind != 'isbn' or not isbn:
                    raise ValueError('The metadata preview has no valid bibliographic identifier.')
                # Reuse the catalogue's ISBN equivalence, but never merge or
                # overwrite existing references. An ISBN-10 and its 978 form
                # identify the same edition; a 979 ISBN has no ISBN-10 form.
                canonical = _isbn_key(isbn)
                variants = {isbn, canonical}
                if canonical.startswith('978'):
                    prefix = canonical[3:12]
                    check = (-sum((10 - i) * int(c) for i, c in enumerate(prefix))) % 11
                    variants.add(prefix + ('X' if check == 10 else str(check)))
                marks = ','.join('?' for _ in variants)
                rows = tuple(db.execute('SELECT id FROM academic_reference '
                    f'WHERE workspace=? AND isbn IN ({marks}) AND archived=0 ORDER BY id LIMIT 2',
                    (workspace, *sorted(variants))))
            if len(rows) > 1:
                raise ValueError('More than one reference already has this identifier. Resolve its duplicates in the academic library first.')
            if rows:
                return library.get(rows[0][0])
            reference = library.create(**self._fields(preview))
            _cancel(cancelled)
            return reference

    def apply_metadata(self, workspace, edition_id, preview, *, expected_metadata,
                       cancelled=lambda: False):
        preview = self._checked(workspace, preview, cancelled)
        meta = preview.metadata
        if expected_metadata is None:
            raise ValueError('Review the current book metadata before applying a metadata suggestion.')
        # Preserve fields absent from the remote record. Publication page extent
        # is never applied as a source locator or printed page assertion.
        return self.app.update_metadata(workspace, edition_id, title=meta.title or expected_metadata.title,
            authors=meta.authors or expected_metadata.authors,
            language=meta.language or expected_metadata.language,
            edition=expected_metadata.edition,
            volume=meta.volume or expected_metadata.volume,
            expected_metadata=expected_metadata, cancelled=cancelled)

    def acquire(self, workspace, preview, source_index, destination, *, online_allowed=False,
                rights_confirmed=False, collection=None, cancelled=lambda: False):
        preview = self._checked(workspace, preview, cancelled)
        self._scope(workspace, cancelled, collection)
        if online_allowed is not True or rights_confirmed is not True:
            raise PermissionError('Approve this separate document download and confirm you may obtain this source.')
        if type(source_index) is not int or not 0 <= source_index < len(preview.sources):
            raise ValueError('Choose a source from this metadata preview.')
        source = preview.sources[source_index]
        if source.reported_open_access is not True or source.format.casefold() != 'pdf':
            raise ValueError('Automatic acquisition requires a provider-reported open-access PDF. Other links remain metadata only.')
        host = urlsplit(source.url).hostname
        validate_https_url(source.url, (host,))
        target = _destination(destination, self.app.root)
        receipt = self._provenance(preview)
        receipt.update(source_provider=source.provider, source_url=source.url,
            landing_url=source.landing_url, provider_license=source.license,
            provider_version=source.version, provider_reports_open_access=True,
            user_confirmed_right_to_obtain=True, rights_do_not_imply_redistribution=True,
            acquired_utc=_utc(), title=preview.metadata.title, authors=preview.metadata.authors)
        notices = []
        owned = []
        committed = False
        marker = target / '.acquisition-incomplete'
        directory_identity = marker_identity = None
        broker = self._resource_broker or ResourceBroker.for_app(self.app.root, cancelled=cancelled)
        request = ResourceRequest(cpu_slots=1, memory_bytes=128 * 1024 * 1024,
            disk_path=target.parent, disk_bytes=2 * (MAX_DOWNLOAD_BYTES + 4 * 1024 * 1024),
            additional_disks=((Path(gettempdir()), MAX_DOWNLOAD_BYTES),))
        # The isolated network worker uses the ordinary temporary filesystem;
        # staging and the selected destination use the chosen parent filesystem.
        # This is declared cooperative admission, not a system RAM/network cap.
        with broker.reserve(request, operation_id='acquisition:download:' + uuid4().hex,
                cancelled=cancelled, timeout=60, run_timeout=180) as lease:
            temporary = TemporaryDirectory(prefix='.carr-acquire-', dir=target.parent)
            staging = Path(temporary.name)
            failure = None
            try:
                transport = self.transport_factory(NetworkConsent(True, 'download', (host,),
                    max_bytes=MAX_DOWNLOAD_BYTES, timeout=120.0))
                with (staging / 'document.pdf').open('xb') as stream:
                    info = transport.download(source.url, stream, cancelled=lease.cancelled)
                    stream.flush()
                    os.fsync(stream.fileno())
                _attest(staging / 'document.pdf', info.sha256, info.size, lease.cancelled)
                receipt.update(final_url=info.url, sha256=info.sha256, bytes=info.size,
                               content_type=info.content_type)
                _write_json(staging / 'provenance.json', receipt)
                provenance = (staging / 'provenance.json').read_bytes()
                receipt_digest = sha256(provenance).hexdigest()
                self._scope(workspace, lease.cancelled, collection)
                _destination(target, self.app.root)
                target.mkdir(mode=0o700)
                directory_identity = _identity(target)
                with marker.open('x', encoding='utf-8') as stream:
                    stream.write('Incomplete acquisition; do not import.\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                    value = os.fstat(stream.fileno())
                    marker_identity = value.st_dev, value.st_ino
                _sync_directory(target)
                for name in ('document.pdf', 'provenance.json'):
                    lease.checkpoint()
                    output = target / name
                    identity = _publish_file(staging / name, output, lease.cancelled)
                    owned.append((output, identity))
                self._scope(workspace, lease.cancelled, collection)
                _sync_directory(target)
                for name in ('document.pdf', 'provenance.json'):
                    (staging / name).unlink()
                _sync_directory(staging)
                expected = ((target / 'document.pdf', info.sha256, info.size),
                            (target / 'provenance.json', receipt_digest, len(provenance)))
                for (output, digest, size), (owned_path, identity) in zip(expected, owned):
                    if output != owned_path or _attest(output, digest, size, lease.cancelled) != identity:
                        raise ValueError('The acquisition publication changed before completion.')
                # Native ownership failures are still pre-completion failures.
                # The metadata-only cleanup flag is truthful after release.
                lease.release()
                if lease.cleanup_pending:
                    notices.append('The shared resource reservation was released; stale lease metadata awaits cleanup.')
                self._scope(workspace, cancelled, collection)
                if _identity(target) != directory_identity or _identity(marker) != marker_identity:
                    raise ValueError('The acquisition destination changed before completion.')
                try:
                    marker.unlink()
                except OSError:
                    # An interrupted unlink can still have removed the marker.
                    # Adopt only our exact verified pair, never a foreign file.
                    if os.path.lexists(marker) or any(_identity(path) != identity for path, identity in owned):
                        raise
                    committed = True
                    notices.append('Completion was published, but removing its marker reported a filesystem error.')
                else:
                    committed = True
                for directory in (target, target.parent):
                    try:
                        _sync_directory(directory)
                    except OSError:
                        notices.append('The PDF and provenance are saved; directory durability confirmation needs attention.')
            except BaseException as exc:
                failure = exc
                if not committed and directory_identity is not None:
                    _rollback(target, directory_identity, marker, marker_identity, owned)
                raise
            finally:
                try:
                    temporary.cleanup()
                except OSError as exc:
                    if committed:
                        notices.append('The PDF and provenance are saved; temporary-folder cleanup is pending. Do not download again.')
                    elif failure is not None:
                        failure.add_note('Acquisition staging cleanup is also pending: ' + str(exc))
                    else:
                        raise
        def completion(message):
            return message + (' ' + ' '.join(dict.fromkeys(notices)) if notices else '')
        path, receipt_path = target / 'document.pdf', target / 'provenance.json'
        # Download publication and source import are deliberately separate. A
        # failed/quarantined import retains the verified PDF and its receipt so
        # the existing Import Center can review or retry without downloading.
        try:
            self._scope(workspace, cancelled, collection)
            with self.app.read(workspace) as db:
                existing = db.execute('SELECT e.id FROM catalog_edition e JOIN active a '
                    'ON a.workspace=e.workspace AND a.edition=e.id '
                    'WHERE e.workspace=? AND e.asset=? ORDER BY e.id LIMIT 1',
                    (workspace, info.sha256)).fetchone()
            if existing:
                # Matching database hashes are not proof that the retained file
                # still exists with these exact bytes. Revalidate before linking
                # a reference or publishing collection membership.
                self.app.assets.resolve(info.sha256, cancelled=cancelled)
                edition_id, state = existing[0], 'duplicate'
                message = 'Identical source bytes already exist in this library; linked to the existing edition.'
                if collection is not None:
                    with self.app.catalog_transaction(workspace) as catalog:
                        _cancel(cancelled)
                        catalog.assign(collection, edition_id)
            else:
                edition_id = uuid4().hex
                imported = self.app.import_file(workspace, path, title=preview.metadata.title,
                    authors=preview.metadata.authors, language=preview.metadata.language or None,
                    volume=preview.metadata.volume or None, edition_id=edition_id,
                    collections=() if collection is None else (collection,), cancelled=cancelled)
                state = imported['state']
                if state != 'complete':
                    edition_id = None
                message = imported.get('message') or ('Source imported; quotation review remains separate.'
                    if state == 'complete' else 'Source downloaded. Continue its pending review in Import Center.')
            self._scope(workspace, cancelled, collection)
            reference = self.save_reference(workspace, preview, cancelled=cancelled)
            with self.app.write(workspace) as db:
                _cancel(cancelled)
                store = AcademicLibrary(db, workspace)
                current = store.get(reference.id)
                changes = {}
                if edition_id is not None:
                    if current.edition_id is None:
                        changes['edition_id'] = edition_id
                    elif current.edition_id != edition_id:
                        message += ' The existing reference links another edition and was preserved; review the link manually.'
                note = '\n\nDownloaded source provenance:\n' + json.dumps(receipt,
                    ensure_ascii=False, sort_keys=True)
                if note not in current.notes:
                    changes['notes'] = current.notes + note
                if changes:
                    reference = store.update(current.id, expected=current, **changes)
            return AcquisitionResult(path, receipt_path, reference.id, edition_id,
                                     state, completion(message), info.sha256)
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            return AcquisitionResult(path, receipt_path, None, None, 'downloaded',
                completion('The PDF and provenance were saved, but import or reference linking did not finish. '
                'Use Import Center to review or retry this local file; no redownload is needed.'), info.sha256)


_SERVICE_LOCK = Lock()


def service_for(app):
    with _SERVICE_LOCK:
        if not hasattr(app, '_acquisition_service'):
            app._acquisition_service = AcquisitionService(app)
        return app._acquisition_service
