"""Explicit plain-text translation derivatives on the existing provider gateway.

Jobs retain accepted chunks. Uncertain billable calls are never retried.
"""
from dataclasses import asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from uuid import uuid4

from .models import canonical, local_path, text
from .profiles import ProviderProfile, google_v3_parent
from .translation_source import capture_source, source_context, source_task_id, validate_source
from .providers import Authorization, Gateway, money, _request
from .state import JobStore, atomic_json, read_json, now
from ..speech.reading import ReadingDocument
from ..speech._io import new_output, digest
from ..state_backup import publish_state

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


def translation_payload(value, source, target, glossary=None):
    payload = {'q': value, 'target': target}
    if source != 'auto':
        payload['source'] = source
    if glossary is not None:
        payload['glossary'] = glossary
    return payload


def translation_approval(profile, value, source, target, budget, *, glossary=None,
                         source_anchor=None, source_metadata=None, source_topic=None):
    """Called only after explicit review; bind every text/model/glossary request."""
    hashes = []
    for chunk in split_text(value):
        if chunk.strip():
            _, body, _ = _request(profile, 'translation.text', translation_payload(chunk, source, target, glossary))
            hashes.append(_hash(canonical(body)))
    identity = uuid4().hex
    task = source_task_id(identity, value, source_anchor, source_metadata, source_topic)
    approval = Authorization(identity, task, ((profile.id, profile.endpoint),),
        ('translation.text',), ('selected-excerpt',), str(money(budget)), len(hashes),
        time() + 24 * 3600, tuple(hashes))
    per_call = (money(budget) / Decimal(len(hashes))).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
    return approval, str(per_call)


def _approved_requests(profile, chunks, source, target, authorization, per_call_cost, glossary=None,
                       source_anchor=None, source_metadata=None, source_topic=None):
    """Reconstruct the same approved requests for submission and later reads."""
    if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled:
        raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
    hashes = []
    for chunk in chunks:
        if not chunk.strip():
            hashes.append(None)
            continue
        payload = translation_payload(chunk, source, target, glossary)
        _, body, _ = _request(profile, 'translation.text', payload)
        hashes.append(_hash(canonical(body)))
    remote_hashes = tuple(value for value in hashes if value is not None)
    if (not isinstance(authorization, Authorization) or authorization.task_id != source_task_id(
            authorization.id, ''.join(chunks), source_anchor, source_metadata, source_topic)
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


class TranslationService:
    def __init__(self, directory, *, gateway=None, library=None, source_guard=lambda: None):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.jobs = JobStore(self.directory / 'jobs')
        self.gateway = gateway or Gateway(self.directory / 'provider')
        self.library, self.source_guard = library, source_guard

    def select_source(self, workspace, edition, **options):
        if self.library is None:
            raise ValueError('Open an existing library before selecting a source.')
        return capture_source(self.library, workspace, edition, guard=self.source_guard, **options)

    def _validate_source(self, job, *, cancelled=lambda: False):
        if job.get('source_anchor') is None:
            return
        if self.library is None or self.directory != self.library.root / 'state' / 'translation':
            raise ValueError('Source-bound translation belongs to its existing library state.')
        try:
            validate_source(self.library, job['source_text'], job['source_anchor'],
                job['source_metadata'], job.get('source_topic'), cancelled=cancelled, guard=self.source_guard)
        except InterruptedError:
            raise
        except (ValueError, OSError):
            if cancelled():
                raise InterruptedError('Translation source verification cancelled.') from None
            raise PermissionError('The reviewed translation source or scope changed. Reopen and review it before continuing.') from None

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

    def preview(self, profile, value, source, target, *, rate_per_million=None, glossary=None,
                source_anchor=None, source_metadata=None, source_topic=None, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Translation review cancelled.')
        if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled:
            raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
        parts = split_text(value)
        context = source_context(value, source_anchor, source_metadata, source_topic)
        if context is not None:
            self._validate_source({'source_text': value, **context}, cancelled=cancelled)
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
        if cancelled():
            raise InterruptedError('Translation review cancelled.')
        return {'source_sha256': _hash(value), 'characters': len(value),
            'calls': sum(bool(part.strip()) for part in parts), 'source': source, 'target': target,
            'provider_adapter': profile.adapter, 'endpoint': profile.endpoint, 'model': profile.model,
            'glossary': glossary, 'glossary_matching': 'case sensitive' if glossary else None,
            'estimated_cost': estimate, 'estimate_basis': 'owner-entered rate per million characters; not a provider quote',
            'preservation': 'plain text derivative; document formatting is not retained',
            **(context or {})}

    def submit(self, profile, value, source, target, *, title='Translation', authorization, per_call_cost, glossary=None,
               source_anchor=None, source_metadata=None, source_topic=None, cancelled=lambda: False):
        context = source_context(value, source_anchor, source_metadata, source_topic)
        preview = self.preview(profile, value, source, target, glossary=glossary,
                               cancelled=cancelled, **(context or {}))
        parts = split_text(value)
        _, cost = _approved_requests(profile, parts, source, target, authorization, per_call_cost, glossary,
                                     **(context or {}))
        text(title, 'translation title', maximum=512)
        identity = uuid4().hex
        job = {'schema': 'carr-assistant-job/1', 'kind': 'translation/1', 'id': identity,
            'created': now(), 'status': 'queued', 'profile': profile.to_dict(), 'title': title,
            'source_text': value, 'source_sha256': preview['source_sha256'], 'source_language': source,
            'target_language': target, 'chunks': list(parts), 'receipts': [], 'glossary': glossary,
            'authorization': asdict(authorization), 'per_call_cost': str(cost), **(context or {})}
        if cancelled():
            raise InterruptedError('Translation submission cancelled.')
        self.jobs.save(job)
        return identity

    def _load(self, identity):
        """Verify existing receipts without renewing remote authority or billing."""
        job = self.jobs.load(identity)
        if (job.get('kind') != 'translation/1' or job.get('source_sha256') != _hash(job.get('source_text', ''))
                or job.get('chunks') != list(split_text(job['source_text'])) or not isinstance(job.get('receipts'), list)
                or len(job['receipts']) > len(job['chunks'])):
            raise ValueError('The translation checkpoint is invalid.')
        try:
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            requests, cost = _approved_requests(profile, job['chunks'], job['source_language'],
                job['target_language'], authorization, job['per_call_cost'], job.get('glossary'),
                job.get('source_anchor'), job.get('source_metadata'), job.get('source_topic'))
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
        profile_hash = _hash(canonical(profile.to_dict()))
        for index, row in enumerate(job['receipts']):
            if (type(row) is not dict or row.get('index') != index
                    or row.get('source_sha256') != _hash(job['chunks'][index])
                    or type(row.get('text')) is not str or len(row['text']) > 200000
                    or row.get('translation_sha256') != _hash(row['text'])):
                raise ValueError('A completed translation chunk is damaged.')
            if requests[index] is None:
                if (row['text'] != job['chunks'][index] or row.get('request_sha256') is not None
                        or row.get('response_sha256') is not None or row.get('detected_source') is not None):
                    raise ValueError('A whitespace chunk differs from its preserved original.')
                continue
            text(row['text'], 'completed translation output', maximum=200000)
            recorded = operations.get(identity + '_' + str(index))
            if (row.get('request_sha256') != requests[index]
                    or not isinstance(row.get('response_sha256'), str)
                    or len(row['response_sha256']) != 64
                    or any(char not in '0123456789abcdef' for char in row['response_sha256'])
                    or type(recorded) is not dict or recorded.get('status') != 'complete'
                    or recorded.get('profile') != profile.id or recorded.get('model') != profile.model
                    or recorded.get('profile_sha256') != profile_hash
                    or recorded.get('operation') != 'translation.text'
                    or recorded.get('request_sha256') != requests[index]
                    or recorded.get('output_sha256') != row['response_sha256']
                    or money(recorded.get('reserved_cost')) != cost):
                raise ValueError('A completed translation receipt differs from its approved request or usage evidence.')
            if profile.adapter == 'google-translation-v3':
                expected_glossary = {'glossary': job['glossary'], 'ignoreCase': False} if job.get('glossary') else None
                if (row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                        or row.get('glossary_config') != expected_glossary):
                    raise ValueError('The Advanced v3 receipt has no verified model/glossary provenance.')
        return job

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None):
        with self.jobs.lock(identity):
            job = self._load(identity)
            self._validate_source(job, cancelled=cancelled)
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            total = len(job['chunks'])
            job['status'] = 'running'
            self.jobs.save(job)
            try:
                for index in range(len(job['receipts']), total):
                    if cancelled():
                        raise InterruptedError('Translation paused before the next request.')
                    self._validate_source(job, cancelled=cancelled)
                    chunk = job['chunks'][index]
                    if chunk.strip():
                        payload = translation_payload(chunk, job['source_language'], job['target_language'], job.get('glossary'))
                        result = self.gateway.request(profile, 'translation.text', payload,
                            task_id=authorization.task_id, data_class='selected-excerpt', authorization=authorization,
                            max_cost=job['per_call_cost'], operation_id=identity + '_' + str(index),
                            cancelled=cancelled, max_response_bytes=1024 * 1024,
                            before_request=lambda: self._validate_source(job, cancelled=cancelled),
                            before_publish=lambda: self._validate_source(job, cancelled=cancelled))
                    else:
                        result = {'text': chunk, 'detected_source': None, 'request_sha256': None, 'output_sha256': None}
                    receipt = {'index': index, 'source_sha256': _hash(chunk), 'text': result['text'],
                        'translation_sha256': _hash(result['text']), 'detected_source': result.get('detected_source'),
                        'request_sha256': result.get('request_sha256'), 'response_sha256': result.get('output_sha256')}
                    if profile.adapter == 'google-translation-v3' and chunk.strip():
                        receipt.update(reported_model=result['reported_model'], glossary_config=result['glossary_config'])
                    job['receipts'].append(receipt)
                    # Save accepted output before cancellation: never bill twice.
                    self.jobs.save(job)
                    progress(index + 1, total, 'Verified translation chunks')
                job['status'] = 'complete'
                self.jobs.save(job)
            except InterruptedError:
                job['status'] = 'paused'
                self.jobs.save(job)
            except Exception:
                job['status'] = 'attention'
                self.jobs.save(job)
                raise
            return {'id': identity, 'status': job['status'], 'completed': len(job['receipts']), 'total': total}

    def export(self, identity, output):
        job = self._load(identity)
        if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
            raise ValueError('Finish and verify all chunks before publishing the translation.')
        self._validate_source(job)
        target = local_path(output, must_exist=False)
        local_path(target.parent)
        if target.exists():
            raise FileExistsError('Translation export already exists; choose a new directory.')
        # Stage privately, then use the existing exclusive directory publisher.
        # A source revoked while serializing cannot leave exported source bytes.
        with TemporaryDirectory(dir=target.parent, prefix='.pending-translation-export-') as staged:
            report = self._export_into(job, Path(staged))
            self._validate_source(job)
            publish_state(staged, target)
        return {'status': 'complete', 'path': str(target), 'files': report['files']}

    def _export_into(self, job, target):
        translated = _assemble_translation(job['chunks'], job['receipts'])
        reading = ReadingDocument(job['source_text'], title=job['title'], source_anchor=job.get('source_anchor'))
        reading.replace(0, len(reading.text), translated, label='Machine translation derivative; not a source quotation')
        reading.save(target / 'translation.reading.json')
        reading.export(target / 'translated.txt')
        reading.export(target / 'original.txt', original=True)
        bilingual = '\n\n'.join('ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                                for chunk, row in zip(job['chunks'], job['receipts']))
        with new_output(target / 'bilingual.txt') as staged:
            staged.write_text(bilingual, encoding='utf-8')
        paths = ('translation.reading.json', 'translated.txt', 'original.txt', 'bilingual.txt')
        report = {'schema': 'carr-translation-export/1', 'job_id': job['id'],
            'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
            'target_language': job['target_language'], 'provider_adapter': job['profile']['adapter'],
            'provider_endpoint': job['profile']['endpoint'], 'glossary': job.get('glossary'),
            'model': job['profile']['model'], 'source_is_immutable': True, 'quotation_authority': False,
            'automated_translation_quality': 'not independently qualified',
            'chunks': [{key: value for key, value in row.items() if key != 'text'} for row in job['receipts']],
            'files': {name: digest(target / name) for name in paths}}
        if job.get('source_anchor') is not None:
            report.update(source_anchor=job['source_anchor'], source_metadata=job['source_metadata'],
                          source_topic=job.get('source_topic'))
        atomic_json(target / 'translation-provenance.json', report)
        return report

    def list(self):
        return tuple(self._load(identity) for identity in self.jobs.identities())
