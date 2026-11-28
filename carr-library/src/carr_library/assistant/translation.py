"""Explicit plain-text translation derivatives on the existing provider gateway.

Jobs retain accepted chunks. Uncertain billable calls are never retried.
"""
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from time import time
import re
import os
from tempfile import TemporaryDirectory
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .models import canonical, local_path, text
from .profiles import ProviderProfile, google_v3_parent
from .providers import Authorization, Gateway, money, _request, translation_receipt_sha256
from .state import JobStore, atomic_json, read_json, now, MAX_CHECKPOINT_BYTES
from ..speech.reading import ReadingDocument, MAX_PROJECT_BYTES
from ..ingest.writer_guard import exclusive_writer
from ..speech._io import new_output, digest, check_cancelled
from .library_translation import (validate_source_binding, verify_library_sources, source_summary,
                                  authorize_library_binding)

ENDPOINT = 'https://translation.googleapis.com/language/translate/v2'


def split_text(value, limit=4500):
    text(value, 'selected translation text', maximum=2_000_000)
    if type(limit) is not int or not 100 <= limit <= 5000:
        raise ValueError('Invalid translation chunk size.')
    parts = []
    start = 0
    while start < len(value):
        end = min(start + limit, len(value))
        if end < len(value):
            boundary = max(value.rfind('\n', start + limit // 2, end),
                           value.rfind(' ', start + limit // 2, end))
            if boundary >= 0:
                end = boundary + 1
        parts.append(value[start:end])
        start = end
    return tuple(parts)


def _hash(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _source_job_id(job):
    """PR165: keep source identity detectable before its first usage record."""
    profile_hash = (job.get('original_profile_sha256') if job.get('restore_requires_review', False)
                    else _hash(canonical(job['profile'])))
    payload = canonical({**{key: job[key] for key in (
        'source_binding', 'source_sha256', 'authorization', 'execution_origin')},
        'profile_sha256': profile_hash})
    return uuid5(NAMESPACE_URL, 'urn:carr-library:translation-source/1:' + payload).hex


def validate_translation_source_record(job):
    """Pure source-job checks, also used before complete-backup recovery."""
    if 'source_binding' not in job:
        if 'execution_origin' in job or UUID(job['id']).version == 5:
            raise ValueError('A source-bound translation cannot use the legacy unbound job format.')
        return None
    binding = validate_source_binding(job['source_binding'], job['source_text'])
    origin = job.get('execution_origin')
    if (type(origin) is not dict or set(origin) != {'root', 'identity'}
            or type(origin['root']) is not str or not 1 <= len(origin['root']) <= 32768
            or '\x00' in origin['root']
            or not (PurePosixPath(origin['root']).is_absolute() or PureWindowsPath(origin['root']).is_absolute())
            or '..' in PurePosixPath(origin['root']).parts or '..' in PureWindowsPath(origin['root']).parts
            or type(origin['identity']) is not list or len(origin['identity']) != 2
            or any(type(part) is not int or part < 0 for part in origin['identity'])):
        raise ValueError('Invalid original translation library execution identity.')
    if job['id'] != _source_job_id(job):
        raise ValueError('The source translation differs from its saved identity or approved authority.')
    return binding


def translation_payload(value, source, target, glossary=None):
    """One typed request shared by preview, approval, submission and recovery."""
    payload = {'q': value, 'target': target}
    if source != 'auto':
        payload['source'] = source
    if glossary is not None:
        payload['glossary'] = glossary
    return payload


def _advanced_receipt(profile, glossary, row):
    """Verify the requested v3 model/glossary independently of saved text."""
    if profile.adapter != 'google-translation-v3':
        if 'reported_model' in row or 'glossary_config' in row:
            raise ValueError('A Basic translation receipt cannot claim Advanced model or glossary provenance.')
        return
    if row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model:
        raise ValueError('The Advanced v3 receipt has no verified model provenance.')
    observed = row.get('glossary_config')
    if glossary is None:
        if observed is not None:
            raise ValueError('The Advanced v3 receipt has an unapproved glossary.')
    elif (type(observed) is not dict or set(observed) != {'glossary', 'ignoreCase'}
          or observed['glossary'] != glossary or observed['ignoreCase'] is not False):
        raise ValueError('The Advanced v3 receipt has no verified glossary provenance.')


def _approved_requests(profile, chunks, source, target, authorization, per_call_cost, *,
                       source_binding=None, glossary=None, allow_disabled=False):
    """Reconstruct the same approved requests for submission and later reads."""
    if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled and not allow_disabled:
        raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
    hashes = []
    for chunk in chunks:
        if not chunk.strip():
            hashes.append(None)
            continue
        _, body, _ = _request(profile, 'translation.text', translation_payload(chunk, source, target, glossary))
        hashes.append(_hash(canonical(body)))
    remote_hashes = tuple(value for value in hashes if value is not None)
    expected_task = (authorization.id + ':source:' + source_binding['sha256']
                     if isinstance(authorization, Authorization) and source_binding is not None
                     else getattr(authorization, 'id', None))
    if (not isinstance(authorization, Authorization) or authorization.task_id != expected_task
            or tuple(authorization.operations) != ('translation.text',)
            or tuple(authorization.data_classes) != ('selected-excerpt',)
            or tuple(tuple(p) for p in authorization.profiles) != ((profile.id, profile.endpoint),)
            or authorization.max_calls < len(remote_hashes)):
        raise PermissionError('A fresh translation-specific approval is required.')
    if remote_hashes != tuple(authorization.request_hashes):
        raise PermissionError('Approval does not match this exact selected text and languages.')
    cost = money(per_call_cost)
    if cost * len(remote_hashes) > money(authorization.budget):
        raise PermissionError('The per-call reservation exceeds the approved total budget.')
    return hashes, cost


def _assemble_translation(chunks, receipts):
    """Restore source whitespace at joins without changing provider receipts.

    Providers may trim or normalize a chunk's outer whitespace. Preserve the
    exact source separator where two chunks meet, including whitespace-only
    chunks. A join without source whitespace and a single-chunk result remain
    unchanged; neither is evidence that an extra word separator is needed.
    """
    parts = []
    for index, (source, receipt) in enumerate(zip(chunks, receipts)):
        translated = receipt['text']
        if not source.strip():
            parts.append(source)
            continue
        if index and (chunks[index - 1][-1].isspace() or source[0].isspace()):
            leading = source[:len(source) - len(source.lstrip())]
            translated = leading + translated.lstrip()
        if index + 1 < len(chunks) and (source[-1].isspace() or chunks[index + 1][0].isspace()):
            trailing = source[len(source.rstrip()):]
            translated = translated.rstrip() + trailing
        parts.append(translated)
    return ''.join(parts)


def translation_approval(profile, value, source, target, budget, *, source_binding=None, glossary=None):
    """Create an exact selected-data envelope after the caller obtains consent."""
    binding = validate_source_binding(source_binding, value) if source_binding is not None else None
    identity, hashes = uuid4().hex, []
    for chunk in split_text(value):
        if not chunk.strip():
            continue
        _, body, _ = _request(profile, 'translation.text', translation_payload(chunk, source, target, glossary))
        hashes.append(_hash(canonical(body)))
    task = identity + ':source:' + binding['sha256'] if binding is not None else identity
    approval = Authorization(identity, task, ((profile.id, profile.endpoint),),
        ('translation.text',), ('selected-excerpt',), str(money(budget)), len(hashes),
        time() + 24 * 3600, tuple(hashes))
    per_call = (money(budget) / Decimal(len(hashes))).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
    return approval, str(per_call)


@dataclass(frozen=True)
class TranslationReadingSource:
    """The existing shared reader source-loader protocol, without a new player."""
    document: ReadingDocument
    title: str
    positions: tuple
    warnings: tuple

    def reading_document(self):
        return ReadingDocument.from_dict(self.document.to_dict())


class TranslationService:
    def __init__(self, directory, *, gateway=None, library=None, workspace=None,
                 scope_guard=lambda: None):
        if not callable(scope_guard):
            raise TypeError('A live translation scope guard is required.')
        self.library, self.workspace, self.scope_guard = library, workspace, scope_guard
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.jobs = JobStore(self.directory / 'jobs')
        self.gateway = gateway or Gateway(self.directory / 'provider')

    @staticmethod
    def _admission_budget(job):
        """PR165: reserve storage for accepted output before any paid call."""
        binding = job.get('source_binding')
        if binding is None:
            return
        anchor = binding['sources'][0]['anchor'] if len(binding['sources']) == 1 else None
        reading = ReadingDocument(job['source_text'], title=job['title'], source_anchor=anchor)
        original_bytes = len((json.dumps(reading.to_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        if original_bytes + 6 * 2_000_000 + 1024 > MAX_PROJECT_BYTES:
            raise ValueError('This source leaves insufficient reading-project space; choose fewer positions.')
        queued_bytes = len((canonical(job) + '\n').encode('utf-8'))
        if queued_bytes + 6 * (2_000_000 + 200000) + 1000 * len(job['chunks']) > MAX_CHECKPOINT_BYTES:
            raise ValueError('This source exceeds the translation checkpoint budget; choose fewer positions.')

    @staticmethod
    def _output_budget(job):
        if job.get('source_binding') is not None and (
                sum(len(row['text']) for row in job['receipts']) > 2_000_000
                or len(_assemble_translation(job['chunks'], job['receipts'])) > 2_000_000):
            raise ValueError('The translation exceeds the 2,000,000-character reading-copy limit. '
                             'Accepted chunks were retained; choose a smaller range for a new job.')

    def _check_execution_origin(self, job):
        origin = job.get('execution_origin')
        if origin is None:
            return
        if self.library is None:
            raise PermissionError('Open this translation from its original library before external execution.')
        observed = self.library.root.stat()
        if (Path(origin['root']) != self.library.root
                or origin['identity'] != [observed.st_dev, observed.st_ino]):
            raise PermissionError('This library was moved or copied. Preview and approve a new translation '
                                  'before further external requests; saved receipts remain readable.')

    def _languages_path(self, profile):
        return self.directory / ('languages-' + profile.id + '.json')

    def languages(self, profile):
        path = self._languages_path(profile)
        if not path.exists():
            return ()
        data = read_json(path, maximum=1024 * 1024)
        if (type(data) is not dict or data.get('schema') != 'carr-translation-languages/1' or data.get('profile') != profile.id
                or data.get('endpoint') != profile.endpoint or data.get('model') != profile.model):
            raise ValueError('Refresh languages for this exact provider and model.')
        rows = data.get('languages')
        advanced = profile.adapter == 'google-translation-v3'
        keys = {'language', 'name', 'source', 'target'} if advanced else {'language', 'name'}
        if (not isinstance(rows, list) or not 1 <= len(rows) <= 1000
                or any(type(row) is not dict or set(row) != keys
                       or type(row.get('language')) is not str or type(row.get('name')) is not str
                       or (advanced and (type(row['source']) is not bool or type(row['target']) is not bool)) for row in rows)
                or _hash(canonical(rows)) != data.get('languages_sha256')):
            raise ValueError('The cached language list is damaged; explicitly refresh it.')
        return tuple(rows)

    def refresh_languages(self, profile, *, authorization, cancelled=lambda: False):
        result = self.gateway.request(profile, 'translation.languages', {'target': 'en'},
            task_id=authorization.task_id, data_class='user-instruction', authorization=authorization,
            max_cost='0', operation_id=uuid4().hex, cancelled=cancelled, max_response_bytes=1024 * 1024)
        rows = result['languages']
        atomic_json(self._languages_path(profile), {'schema': 'carr-translation-languages/1',
            'profile': profile.id, 'endpoint': profile.endpoint, 'model': profile.model,
            'fetched_at': now(), 'languages': rows, 'languages_sha256': _hash(canonical(rows)),
            'qualification': 'provider-declared; not acoustic or translation-quality verification'})
        return rows

    def preview(self, profile, value, source, target, *, rate_per_million=None, source_binding=None,
                glossary=None, cancelled=lambda: False):
        if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled:
            raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
        self._check_source({'source_text': value, 'source_binding': source_binding}, cancelled=cancelled)
        parts = split_text(value)
        first = next(part for part in parts if part.strip())
        selected = translation_payload(first, source, target, glossary)
        _request(profile, 'translation.text', selected)
        languages = self.languages(profile)
        if not languages:
            raise ValueError('Explicitly fetch the supported language list before translating.')
        sources = {row['language'] for row in languages if row.get('source', True)}
        targets = {row['language'] for row in languages if row.get('target', True)}
        if target not in targets or (source != 'auto' and source not in sources):
            raise ValueError('The chosen source or target is not declared supported by this provider model.')
        if source == target:
            raise ValueError('Choose a target language different from the source.')
        estimate = None
        if rate_per_million not in (None, ''):
            estimate = str((money(rate_per_million) * len(value) / Decimal(1_000_000)).quantize(Decimal('0.000001'), rounding=ROUND_UP))
        return {'source_sha256': _hash(value), 'characters': len(value),
            'calls': sum(bool(part.strip()) for part in parts), 'source': source, 'target': target,
            'library_source': source_summary(source_binding) if source_binding is not None else None,
            'provider_adapter': profile.adapter, 'endpoint': profile.endpoint, 'model': profile.model,
            'glossary': glossary, 'glossary_matching': 'case sensitive' if glossary else None,
            'estimated_cost': estimate, 'estimate_basis': 'owner-entered rate per million characters; not a provider quote',
            'preservation': 'plain text derivative; document formatting is not retained'}

    def submit(self, profile, value, source, target, *, title='Translation', authorization, per_call_cost,
               source_binding=None, glossary=None, cancelled=lambda: False):
        binding = validate_source_binding(source_binding, value) if source_binding is not None else None
        preview = self.preview(profile, value, source, target, source_binding=binding,
                               glossary=glossary, cancelled=cancelled)
        parts = split_text(value)
        _, cost = _approved_requests(profile, parts, source, target, authorization, per_call_cost,
                                     source_binding=binding, glossary=glossary)
        text(title, 'translation title', maximum=512)
        identity = uuid4().hex
        job = {'schema': 'carr-assistant-job/1', 'kind': 'translation/1', 'id': identity,
            'created': now(), 'status': 'queued', 'profile': profile.to_dict(), 'title': title,
            'source_text': value, 'source_sha256': preview['source_sha256'], 'source_language': source,
            'target_language': target, 'chunks': list(parts), 'receipts': [], 'glossary': glossary,
            'authorization': asdict(authorization), 'per_call_cost': str(cost)}
        if binding is not None:
            observed = self.library.root.stat()
            job['source_binding'] = binding
            job['execution_origin'] = {'root': str(self.library.root),
                                       'identity': [observed.st_dev, observed.st_ino]}
            identity = job['id'] = _source_job_id(job)
        self._admission_budget(job)
        self._check_source(job, cancelled=cancelled)
        with self.jobs.lock(identity):
            if self.jobs.path(identity).exists():
                raise FileExistsError('This approved translation already exists. Resume its saved job.')
            self.jobs.save(job)
        return identity

    def _check_source(self, job, *, cancelled=lambda: False):
        self.scope_guard()
        check_cancelled(cancelled)
        binding = job.get('source_binding')
        if binding is not None:
            if self.workspace is not None and binding.get('workspace') != self.workspace:
                raise PermissionError('This saved translation belongs to another selected library.')
            verify_library_sources(self.library, binding, job['source_text'],
                                   cancelled=cancelled, guard=self.scope_guard)
        self.scope_guard()
        check_cancelled(cancelled)

    def _load(self, identity, *, receipt_integrity=None):
        """Verify existing receipts without renewing remote authority or billing."""
        job = self.jobs.load(identity)
        restored = job.get('restore_requires_review', False)
        original_profile = job.get('original_profile_sha256')
        if type(restored) is not bool or (original_profile is not None and
                (not restored or not isinstance(original_profile, str)
                 or not re.fullmatch('[0-9a-f]{64}', original_profile))):
            raise ValueError('The translation restore-review marker or provenance is invalid.')
        if (job.get('kind') != 'translation/1' or job.get('source_sha256') != _hash(job.get('source_text', ''))
                or job.get('chunks') != list(split_text(job['source_text'])) or not isinstance(job.get('receipts'), list)
                or len(job['receipts']) > len(job['chunks'])):
            raise ValueError('The translation checkpoint is invalid.')
        binding = validate_translation_source_record(job)
        try:
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            requests, cost = _approved_requests(profile, job['chunks'], job['source_language'],
                job['target_language'], authorization, job['per_call_cost'],
                source_binding=binding, glossary=job.get('glossary'), allow_disabled=restored)
        except (KeyError, TypeError, ValueError, PermissionError):
            raise ValueError('The saved translation authority is invalid.') from None
        operations = {}
        if any(value is not None for value in requests[:len(job['receipts'])]):
            try:
                usage = self.gateway.ledger.get(authorization.id)
            except (OSError, ValueError):
                raise ValueError('Accepted translation usage evidence is unavailable or invalid.') from None
            if (type(usage) is not dict or usage.get('schema') != 'carr-provider-usage/1'
                    or usage.get('authorization') != _hash(canonical(asdict(authorization)))
                    or usage.get('currency') != authorization.currency
                    or type(usage.get('operations')) is not dict):
                raise ValueError('Accepted translation authority differs from its usage evidence.')
            operations = usage['operations']
        profile_hash = original_profile if restored else _hash(canonical(profile.to_dict()))
        for index, row in enumerate(job['receipts']):
            if (type(row) is not dict or row.get('index') != index
                    or row.get('source_sha256') != _hash(job['chunks'][index])
                    or type(row.get('text')) is not str or len(row['text']) > 200000
                    or row.get('translation_sha256') != _hash(row['text'])):
                raise ValueError('A completed translation chunk is damaged.')
            if requests[index] is None:
                if (row['text'] != job['chunks'][index] or row.get('request_sha256') is not None
                        or row.get('response_sha256') is not None or row.get('detected_source') is not None
                        or any(key in row for key in ('decoded_translation_sha256', 'reported_model', 'glossary_config'))):
                    raise ValueError('A whitespace chunk differs from its preserved original.')
                if receipt_integrity is not None:
                    receipt_integrity.append('original-whitespace')
                continue
            text(row['text'], 'completed translation output', maximum=200000)
            _advanced_receipt(profile, job.get('glossary'), row)
            recorded = operations.get(identity + '_' + str(index))
            if (row.get('request_sha256') != requests[index]
                    or not isinstance(row.get('response_sha256'), str)
                    or len(row['response_sha256']) != 64
                    or any(char not in '0123456789abcdef' for char in row['response_sha256'])
                    or type(recorded) is not dict or recorded.get('status') != 'complete'
                    or recorded.get('profile') != profile.id or recorded.get('model') != profile.model
                    or profile_hash is not None and recorded.get('profile_sha256') != profile_hash
                    or recorded.get('operation') != 'translation.text'
                    or recorded.get('request_sha256') != requests[index]
                    or recorded.get('output_sha256') != row['response_sha256']
                    or money(recorded.get('reserved_cost')) != cost):
                raise ValueError('A completed translation receipt differs from its approved request or usage evidence.')
            usage = recorded.get('usage')
            if type(usage) is not dict:
                raise ValueError('Completed translation usage evidence is invalid.')
            key = 'decoded_translation_sha256'
            if binding is not None or key in row or key in usage:
                expected = translation_receipt_sha256(row['text'], row.get('detected_source'))
                if row.get(key) != expected or usage.get(key) != expected:
                    raise ValueError('A decoded translation receipt differs from its usage evidence.')
                integrity = 'decoded-ledger-bound'
            else:
                # Historical receipts predate the independent decoded digest.
                # Keep user-owned derivatives exportable without inventing evidence.
                integrity = 'legacy-wire-only'
            if receipt_integrity is not None:
                receipt_integrity.append(integrity + (';restored-profile-unbound' if restored and profile_hash is None else ''))
        return job

    def get(self, identity, *, cancelled=lambda: False):
        """Read a validated saved translation within the currently selected library."""
        job = self._load(identity)
        self._check_source(job, cancelled=cancelled)
        return job

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None):
        with self.jobs.lock(identity):
            job = self._load(identity)
            if job.get('restore_requires_review', False):
                raise PermissionError('Restored translation jobs require a fresh review and submission before execution.')
            total = len(job['chunks'])
            self._output_budget(job)
            if len(job['receipts']) == total:
                # Accepted complete output stays complete after a late stop.
                # Reading/export still check current source permission separately.
                if job['status'] != 'complete':
                    job['status'] = 'complete'
                    self.jobs.save(job)
                return {'id': identity, 'status': 'complete', 'completed': total, 'total': total}
            try:
                self._check_source(job, cancelled=cancelled)
                self._check_execution_origin(job)
            except (InterruptedError, TimeoutError) as exc:
                paused = isinstance(exc, InterruptedError) or cancelled()
                job['status'] = 'paused' if paused else 'attention'
                self.jobs.save(job)
                if not paused:
                    raise
                return {'id': identity, 'status': 'paused', 'completed': len(job['receipts']),
                        'total': len(job['chunks'])}
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            total = len(job['chunks'])
            job['status'] = 'running'
            self.jobs.save(job)
            try:
                for index in range(len(job['receipts']), total):
                    if cancelled():
                        raise InterruptedError('Translation paused before the next request.')
                    chunk = job['chunks'][index]
                    if chunk.strip():
                        self._check_source(job, cancelled=cancelled)
                        self._check_execution_origin(job)
                        def live_cancelled():
                            if cancelled():
                                return True
                            self.scope_guard()
                            if job.get('source_binding') is not None:
                                self._check_execution_origin(job)
                                authorize_library_binding(self.library, job['source_binding'])
                            return False
                        payload = translation_payload(chunk, job['source_language'],
                                                      job['target_language'], job.get('glossary'))
                        result = self.gateway.request(profile, 'translation.text', payload,
                            task_id=authorization.task_id, data_class='selected-excerpt', authorization=authorization,
                            max_cost=job['per_call_cost'], operation_id=identity + '_' + str(index),
                            cancelled=live_cancelled,
                            max_response_bytes=200000 if job.get('source_binding') is not None else 1024 * 1024)
                    else:
                        result = {'text': chunk, 'detected_source': None, 'request_sha256': None, 'output_sha256': None}
                    receipt = {'index': index, 'source_sha256': _hash(chunk), 'text': result['text'],
                        'translation_sha256': _hash(result['text']), 'detected_source': result.get('detected_source'),
                        'request_sha256': result.get('request_sha256'), 'response_sha256': result.get('output_sha256')}
                    if chunk.strip():
                        if profile.adapter == 'google-translation-v3':
                            receipt.update(reported_model=result['reported_model'],
                                           glossary_config=result['glossary_config'])
                        _advanced_receipt(profile, job.get('glossary'), receipt)
                        receipt['decoded_translation_sha256'] = translation_receipt_sha256(
                            result['text'], result.get('detected_source'))
                    job['receipts'].append(receipt)
                    # Save accepted output before cancellation: never bill twice.
                    self.jobs.save(job)
                    self._output_budget(job)
                    progress(index + 1, total, 'Verified translation chunks')
                job['status'] = 'complete'
                self.jobs.save(job)
            except InterruptedError:
                job['status'] = 'paused'
                self.jobs.save(job)
            except TimeoutError:
                job['status'] = 'paused' if cancelled() else 'attention'
                self.jobs.save(job)
                if job['status'] != 'paused':
                    raise
            except Exception:
                job['status'] = 'attention'
                self.jobs.save(job)
                raise
            return {'id': identity, 'status': job['status'], 'completed': len(job['receipts']), 'total': total}

    def reading_document(self, identity, *, view='translation', cancelled=lambda: False):
        """Open a linked derivative in the shared reader; never source evidence."""
        job = self.get(identity, cancelled=cancelled)
        return self._reading_document(job, view=view)

    def export_reading(self, identity, output, *, view='bilingual', cancelled=lambda: False):
        """Publish a new linked project through the shared reading writer."""
        job = self.get(identity, cancelled=cancelled)
        target = local_path(output, must_exist=False)
        if target.is_relative_to(self.directory) or self.library is not None and target.is_relative_to(self.library.root):
            raise ValueError('Choose a reading export outside private library and translation state.')
        document = self._reading_document(job, view=view)
        def publication_check():
            # ReadingDocument invokes this immediately before its no-clobber
            # publication. Preserve live source/scope authority through Save.
            self._check_source(job, cancelled=cancelled)
            return False
        with exclusive_writer(self.library.root) if job.get('source_binding') is not None else nullcontext():
            document.save(target, cancelled=publication_check)
        return {'status': 'complete', 'path': str(target), 'job_id': identity,
                'view': view, 'quotation_authority': False}

    def reading_source(self, identity, *, view='translation', cancelled=lambda: False):
        job = self.get(identity, cancelled=cancelled)
        document = self._reading_document(job, view=view)
        binding = job.get('source_binding')
        positions = tuple(p for row in binding['sources'] for p in row['anchor']['positions']) if binding else ()
        return TranslationReadingSource(document, job['title'] + ' — ' + view, positions,
            ('Machine translation is an unqualified derivative. Original text and source identities remain distinct.',))

    @staticmethod
    def _reading_document(job, *, view='translation'):
        if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
            raise ValueError('Finish and verify all chunks before reading the translated derivative.')
        if view not in ('original', 'translation', 'bilingual'):
            raise ValueError('Choose original, translation or bilingual reading.')
        TranslationService._output_budget(job)
        binding = job.get('source_binding')
        anchor = binding['sources'][0]['anchor'] if binding and len(binding['sources']) == 1 else None
        reading = ReadingDocument(job['source_text'], title=job['title'], source_anchor=anchor)
        if view != 'original':
            value = (_assemble_translation(job['chunks'], job['receipts']) if view == 'translation' else
                     '\n\n'.join('ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                                    for chunk, row in zip(job['chunks'], job['receipts'])))
            if view == 'bilingual' and len(value) > 2_000_000:
                raise ValueError('This bilingual reading view exceeds 2,000,000 characters after combining '
                                 'the original and translation. Choose Original or Translation, or a smaller source range; '
                                 'the completed job and plain bilingual export remain available.')
            reading.replace(0, len(reading.text), value,
                            label='Machine translation derivative; not a source quotation')
        return reading

    def export(self, identity, output, *, cancelled=lambda: False):
        receipt_integrity = []
        job = self._load(identity, receipt_integrity=receipt_integrity)
        if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
            raise ValueError('Finish and verify all chunks before publishing the translation.')
        self._check_source(job, cancelled=cancelled)
        target = local_path(output, must_exist=False)
        local_path(target.parent)
        if target.exists():
            raise FileExistsError('The translation export already exists. Choose a new folder.')
        if target.is_relative_to(self.directory) or self.library is not None and target.is_relative_to(self.library.root):
            raise ValueError('Choose an export folder outside private library and translation state.')
        with TemporaryDirectory(prefix='.translation-', dir=target.parent) as temporary:
            stage = Path(temporary)
            reading = self._reading_document(job)
            reading.save(stage / 'translation.reading.json', cancelled=cancelled)
            reading.export(stage / 'translated.txt', cancelled=cancelled)
            reading.export(stage / 'original.txt', original=True, cancelled=cancelled)
            bilingual = '\n\n'.join('ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                                    for chunk, row in zip(job['chunks'], job['receipts']))
            with new_output(stage / 'bilingual.txt', cancelled=cancelled) as staged:
                staged.write_bytes(bilingual.encode('utf-8'))
            paths = ('translation.reading.json', 'translated.txt', 'original.txt', 'bilingual.txt')
            report = {'schema': 'carr-translation-export/1', 'job_id': identity,
                'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
                'target_language': job['target_language'], 'provider_adapter': job['profile']['adapter'],
                'model': job['profile']['model'], 'source_is_immutable': True, 'quotation_authority': False,
                'provider_endpoint': job['profile']['endpoint'], 'glossary': job.get('glossary'),
                'source_binding': job.get('source_binding'),
                'original_profile_sha256': job.get('original_profile_sha256'),
                'automated_translation_quality': 'not independently qualified',
                'chunks': [dict({key: value for key, value in row.items() if key != 'text'},
                                decoded_receipt_integrity=integrity)
                           for row, integrity in zip(job['receipts'], receipt_integrity)],
                'files': {name: digest(stage / name, cancelled=cancelled) for name in paths}}
            with exclusive_writer(self.library.root) if job.get('source_binding') is not None else nullcontext():
                self._check_source(job, cancelled=cancelled)
                local_path(target.parent)
                # Exclusive ownership and no-replace links reuse the complete-backup
                # publication policy. POSIX rename must not replace a raced empty folder.
                target.mkdir(mode=0o700)
                incomplete = target / 'INCOMPLETE.txt'
                with new_output(incomplete) as marker:
                    marker.write_bytes(b'Translation export was not committed. Choose a new destination.\n')
                for name in paths:
                    self.scope_guard()
                    check_cancelled(cancelled)
                    os.link(stage / name, target / name)
                    (stage / name).unlink()
                self._check_source(job, cancelled=cancelled)
                with new_output(target / 'translation-provenance.json', cancelled=cancelled) as manifest:
                    manifest.write_bytes((canonical(report) + '\n').encode('utf-8'))
                incomplete.unlink()
        return {'status': 'complete', 'path': str(target), 'files': report['files']}

    def list(self):
        self.scope_guard()
        rows = []
        for identity in self.jobs.identities():
            job = self._load(identity)
            if (self.workspace is not None and job.get('source_binding') is not None
                    and job['source_binding']['workspace'] != self.workspace):
                continue
            rows.append(job)
        self.scope_guard()
        return tuple(rows)
