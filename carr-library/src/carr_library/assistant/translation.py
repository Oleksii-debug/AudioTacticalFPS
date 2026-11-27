"""Explicit plain-text translation derivatives on the existing provider gateway.

Jobs retain accepted chunks. Uncertain billable calls are never retried.
"""
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import json
from pathlib import Path
from time import time
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .models import canonical, local_path, text
from .profiles import ProviderProfile, google_v3_parent
from .providers import Authorization, Gateway, money, _request
from .state import JobStore, atomic_json, read_json, now, MAX_CHECKPOINT_BYTES
from ..speech.reading import ReadingDocument, MAX_PROJECT_BYTES
from ..speech._io import new_output, digest, check_cancelled
from ..ingest.writer_guard import exclusive_writer
from ..reader.reading_desk import ReadingDesk
from ..research.export_file import _destination, _filename

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
    # Canonical pasted-text jobs use UUIDv4. A deterministic UUIDv5 preserves
    # the existing 32-hex JobStore contract and distinguishes source jobs even
    # before their first provider usage record exists.
    payload = canonical({key: job[key] for key in (
        'source_binding', 'source_sha256', 'authorization', 'profile')})
    return uuid5(NAMESPACE_URL, 'urn:carr-library:translation/2:' + payload).hex


def translation_payload(value, source, target, glossary=None):
    payload = {'q': value, 'target': target}
    if source != 'auto':
        payload['source'] = source
    if glossary is not None:
        payload['glossary'] = glossary
    return payload


def translation_approval(profile, value, source, target, budget, *, source_bound=False, glossary=None):
    """Bind exact request hashes and preserve source-job authorization scope."""
    if type(source_bound) is not bool:
        raise ValueError('Choose an explicit source-bound or pasted-text approval.')
    identity = uuid4().hex
    hashes = []
    for chunk in split_text(value):
        if not chunk.strip():
            continue
        _, body, _ = _request(profile, 'translation.text',
                             translation_payload(chunk, source, target, glossary))
        hashes.append(_hash(canonical(body)))
    approval = Authorization(identity, identity, ((profile.id, profile.endpoint),),
        ('translation.text',), ('selected-script',) if source_bound else ('selected-excerpt',),
        str(money(budget)), len(hashes),
        time() + 24 * 3600, tuple(hashes))
    per_call = (money(budget) / Decimal(len(hashes))).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
    return approval, str(per_call)


def _approved_requests(profile, chunks, source, target, authorization, per_call_cost,
                       *, data_class='selected-excerpt', glossary=None):
    """Reconstruct the same approved requests for submission and later reads."""
    if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled:
        raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
    hashes = []
    for chunk in chunks:
        if not chunk.strip():
            hashes.append(None)
            continue
        _, body, _ = _request(profile, 'translation.text', translation_payload(chunk, source, target, glossary))
        hashes.append(_hash(canonical(body)))
    remote_hashes = tuple(value for value in hashes if value is not None)
    if (not isinstance(authorization, Authorization) or authorization.task_id != authorization.id
            or tuple(authorization.operations) != ('translation.text',)
            or tuple(authorization.data_classes) != (data_class,)
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
    def __init__(self, directory, *, gateway=None, library=None):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.jobs = JobStore(self.directory / 'jobs')
        self.gateway = gateway or Gateway(self.directory / 'provider')
        self.library = library

    @staticmethod
    def _binding(job):
        """Older pasted-text jobs stay unbound; source jobs cannot downgrade."""
        if job.get('kind') == 'translation/1':
            if 'source_binding' in job or UUID(job['id']).version != 4:
                raise ValueError('A source-bound translation cannot use the legacy job format.')
            return None
        value = job.get('source_binding')
        if (job.get('kind') != 'translation/2' or type(value) is not dict
                or set(value) != {'schema', 'library_root', 'root_identity', 'topic', 'anchor'}
                or value['schema'] != 'carr-translation-source/1'
                or not isinstance(value['library_root'], str)
                or not 1 <= len(value['library_root']) <= 32768
                or '\0' in value['library_root'] or not Path(value['library_root']).is_absolute()
                or '..' in Path(value['library_root']).parts
                or type(value['root_identity']) is not list or len(value['root_identity']) != 2
                or any(type(part) is not int or part < 0 for part in value['root_identity'])
                or (value['topic'] is not None and (
                    not isinstance(value['topic'], str) or not value['topic'].strip()
                    or len(value['topic']) > 1024))):
            raise ValueError('The translation source binding is invalid.')
        document = ReadingDocument(job['source_text'], title=job['title'],
                                   source_anchor=value['anchor'])
        anchor = document.source_anchor
        if anchor is None:
            raise ValueError('A library translation requires canonical source positions.')
        positions = anchor['positions']
        if [row['ordinal'] for row in positions] != list(
                range(positions[0]['ordinal'], positions[-1]['ordinal'] + 1)):
            raise ValueError('The translation source range is not contiguous.')
        if job.get('id') != _source_job_id(job):
            raise ValueError('The translation source or approval differs from its saved job identity.')
        return value

    def _source_context(self, job, *, index=None, remote=False,
                        cancelled=lambda: False, guard=lambda: None):
        """Reopen the original revision; never retarget a changed source.

        A full selection is checked at admission, resume and export. Each
        outgoing chunk rechecks the retained original and its source positions
        through ReadingDesk rather than a new source verifier.
        """
        binding = self._binding(job)
        if binding is None:
            guard()
            return None
        check_cancelled(cancelled)
        guard()
        if self.library is None:
            raise PermissionError('Reopen this source translation from its library.')
        root_stat = self.library.root.stat()
        if remote and (str(self.library.root) != binding['library_root']
                       or [root_stat.st_dev, root_stat.st_ino] != binding['root_identity']):
            raise PermissionError('This library was moved or restored. Preview and approve a new '
                                  'translation before further external requests; saved receipts remain available.')
        anchor = binding['anchor']
        desk = ReadingDesk(self.library, anchor['workspace'], anchor['edition'],
                           topic=binding['topic'], revision=anchor['revision'],
                           cancelled=cancelled)
        if desk.identity.asset_sha256 != anchor['asset_sha256']:
            raise ValueError('The retained translation original changed.')
        positions = anchor['positions']
        for row in positions:
            ordinal = row['ordinal']
            if ordinal > len(desk.pages):
                raise ValueError('The translation source positions changed.')
            current = desk.pages[ordinal - 1]
            if (current.kind != row['kind'] or current.index != row['index']
                    or current.text_sha256 != row['text_sha256']):
                raise ValueError('The translation source positions changed.')
        if index is not None:
            start = sum(len(chunk) for chunk in job['chunks'][:index])
            end = start + len(job['chunks'][index])
            positions = [row for row in positions if row['start'] < end and row['end'] > start]
        for row in positions:
            check_cancelled(cancelled)
            guard()
            page = desk.page(row['ordinal'])
            if page.raw_text != job['source_text'][row['start']:row['end']]:
                raise ValueError('The selected translation text changed.')
        self._source_current(desk, cancelled=cancelled, guard=guard)
        return desk

    @staticmethod
    def _source_current(desk, *, cancelled=lambda: False, guard=lambda: None):
        """Fresh scope/revision checks also run at the gateway's cancel points."""
        check_cancelled(cancelled)
        guard()
        if desk is not None:
            with desk.library.read(desk.workspace) as db:
                identity, metadata = desk._header(db)
                if identity != desk.identity or metadata != desk.metadata:
                    raise ValueError('The translation source revision changed. Review the source again.')
        check_cancelled(cancelled)

    @staticmethod
    def _admission_budget(job):
        """Reserve the existing checkpoint/project limits before a paid call.

        A Unicode character can require six bytes in JSON. A source project
        exports one new translation edit, never the caller's prior edit history.
        Retain room for the single accepted response that can cross the text
        limit, and bounded per-chunk receipt fields and timestamps.
        """
        if job.get('kind') != 'translation/2':
            return
        reading = ReadingDocument(job['source_text'], title=job['title'],
                                  source_anchor=job['source_binding']['anchor'])
        original_bytes = len((json.dumps(reading.to_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        if original_bytes + 6 * 2_000_000 + 1024 > MAX_PROJECT_BYTES:
            raise ValueError('This source leaves insufficient reading-project space for translation; choose fewer positions.')
        queued_bytes = len((canonical(job) + '\n').encode('utf-8'))
        if queued_bytes + 6 * (2_000_000 + 200000) + 1000 * len(job['chunks']) > MAX_CHECKPOINT_BYTES:
            raise ValueError('This source exceeds the translation checkpoint budget; choose fewer positions.')

    @staticmethod
    def _output_budget(job):
        if job.get('kind') == 'translation/2' and (
                sum(len(row['text']) for row in job['receipts']) > 2_000_000
                or len(_assemble_translation(job['chunks'], job['receipts'])) > 2_000_000):
            raise ValueError('The translated text exceeds the 2,000,000-character reading-copy limit. '
                             'Accepted chunks were retained; choose a smaller source range for a new job.')

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

    def preview(self, profile, value, source, target, *, rate_per_million=None, glossary=None):
        if profile.adapter not in ('google-translation-v2', 'google-translation-v3') or not profile.enabled:
            raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 NMT profile.')
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
            'provider_adapter': profile.adapter, 'endpoint': profile.endpoint, 'model': profile.model,
            'glossary': glossary, 'glossary_matching': 'case sensitive' if glossary else None,
            'estimated_cost': estimate, 'estimate_basis': 'owner-entered rate per million characters; not a provider quote',
            'preservation': 'plain text derivative; document formatting is not retained'}

    def submit(self, profile, value, source, target, *, title='Translation', authorization,
               per_call_cost, source_document=None, topic=None, glossary=None,
               cancelled=lambda: False, guard=lambda: None):
        preview = self.preview(profile, value, source, target, glossary=glossary)
        parts = split_text(value)
        _, cost = _approved_requests(profile, parts, source, target, authorization, per_call_cost,
            data_class='selected-script' if source_document is not None else 'selected-excerpt', glossary=glossary)
        text(title, 'translation title', maximum=512)
        identity = uuid4().hex
        job = {'schema': 'carr-assistant-job/1', 'kind': 'translation/1', 'id': identity,
            'created': now(), 'status': 'queued', 'profile': profile.to_dict(), 'title': title,
            'source_text': value, 'source_sha256': preview['source_sha256'], 'source_language': source,
            'target_language': target, 'chunks': list(parts), 'receipts': [], 'glossary': glossary,
            'authorization': asdict(authorization), 'per_call_cost': str(cost)}
        if source_document is not None:
            if (not isinstance(source_document, ReadingDocument) or self.library is None
                    or source_document.source_anchor is None
                    or source_document.source_text != value or source_document.text != value):
                raise ValueError('Translate the exact unedited library source with its original lineage.')
            job['kind'] = 'translation/2'
            root_stat = self.library.root.stat()
            job['source_binding'] = {'schema': 'carr-translation-source/1',
                'library_root': str(self.library.root),
                'root_identity': [root_stat.st_dev, root_stat.st_ino], 'topic': topic,
                'anchor': source_document.source_anchor}
            job['id'] = identity = _source_job_id(job)
        elif topic is not None:
            raise ValueError('A source topic requires a bound library document.')
        self._admission_budget(job)
        desk = self._source_context(job, cancelled=cancelled, guard=guard)
        with exclusive_writer(self.library.root) if desk is not None else nullcontext():
            self._source_current(desk, cancelled=cancelled, guard=guard)
            if self.jobs.path(identity).exists():
                raise FileExistsError('This approved translation already exists. Resume its saved job.')
            self.jobs.save(job)
        return identity

    def _load(self, identity):
        """Verify existing receipts without renewing remote authority or billing."""
        job = self.jobs.load(identity)
        self._binding(job)
        if (job.get('source_sha256') != _hash(job.get('source_text', ''))
                or job.get('chunks') != list(split_text(job['source_text'])) or not isinstance(job.get('receipts'), list)
                or len(job['receipts']) > len(job['chunks'])):
            raise ValueError('The translation checkpoint is invalid.')
        try:
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            requests, cost = _approved_requests(profile, job['chunks'], job['source_language'],
                job['target_language'], authorization, job['per_call_cost'],
                data_class='selected-script' if job.get('kind') == 'translation/2' else 'selected-excerpt',
                glossary=job.get('glossary'))
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
            usage = recorded.get('usage')
            recorded_text = usage.get('translation_sha256') if type(usage) is dict else None
            if ((job.get('kind') == 'translation/2' or profile.adapter == 'google-translation-v3'
                    or (type(usage) is dict and 'translation_sha256' in usage))
                    and recorded_text != row['translation_sha256']):
                raise ValueError('The completed translation text differs from its accepted decoded-output receipt.')
            if profile.adapter == 'google-translation-v3':
                expected_glossary = {'glossary': job['glossary'], 'ignoreCase': False} if job.get('glossary') else None
                if (row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                        or row.get('glossary_config') != expected_glossary):
                    raise ValueError('The Advanced v3 receipt has no verified model/glossary provenance.')
        return job

    def _text_binding(self, job):
        """Legacy accepted work stays readable without inventing missing proof."""
        operations = self.gateway.ledger.get(job['authorization']['id'])['operations']
        for index, row in enumerate(job['receipts']):
            if not job['chunks'][index].strip():
                continue
            usage = operations[job['id'] + '_' + str(index)].get('usage')
            if type(usage) is not dict or usage.get('translation_sha256') != row['translation_sha256']:
                if job.get('kind') == 'translation/2' or job['profile']['adapter'] == 'google-translation-v3':
                    raise ValueError('The accepted decoded-output receipt is missing or changed.')
                return 'unrecorded-legacy'
        return 'usage-ledger-sha256'

    def get(self, identity):
        """Read one validated saved translation for explicit UI selection."""
        return self._load(identity)

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None,
               guard=lambda: None):
        with self.jobs.lock(identity):
            job = self._load(identity)
            profile = ProviderProfile(**job['profile'])
            authorization = Authorization(**job['authorization'])
            total = len(job['chunks'])
            self._output_budget(job)
            if len(job['receipts']) == total:
                # A finished request set remains completed after late cancellation.
                if job['status'] != 'complete':
                    job['status'] = 'complete'
                    self.jobs.save(job)
                return {'id': identity, 'status': 'complete', 'completed': total, 'total': total}
            job['status'] = 'running'
            self.jobs.save(job)
            try:
                self._output_budget(job)
                self._source_context(job, remote=len(job['receipts']) < total,
                                     cancelled=cancelled, guard=guard)
                for index in range(len(job['receipts']), total):
                    if cancelled():
                        raise InterruptedError('Translation paused before the next request.')
                    chunk = job['chunks'][index]
                    desk = self._source_context(job, index=index, remote=bool(chunk.strip()),
                                                cancelled=cancelled, guard=guard)
                    def stopped():
                        if cancelled():
                            return True
                        self._source_current(desk, guard=guard)
                        return False
                    if chunk.strip():
                        payload = translation_payload(chunk, job['source_language'], job['target_language'], job.get('glossary'))
                        result = self.gateway.request(profile, 'translation.text', payload,
                            task_id=authorization.task_id,
                            data_class='selected-script' if desk is not None else 'selected-excerpt',
                            authorization=authorization,
                            max_cost=job['per_call_cost'], operation_id=identity + '_' + str(index),
                            cancelled=stopped, max_response_bytes=200000
                            if desk is not None or profile.adapter == 'google-translation-v3' else 1024 * 1024)
                    else:
                        result = {'text': chunk, 'detected_source': None, 'request_sha256': None, 'output_sha256': None}
                    receipt = {'index': index, 'source_sha256': _hash(chunk), 'text': result['text'],
                        'translation_sha256': _hash(result['text']), 'detected_source': result.get('detected_source'),
                        'request_sha256': result.get('request_sha256'), 'response_sha256': result.get('output_sha256')}
                    if profile.adapter == 'google-translation-v3' and chunk.strip():
                        receipt.update(reported_model=result['reported_model'], glossary_config=result['glossary_config'])
                    job['receipts'].append(receipt)
                    # Accepted output precedes later scope/cancel checks: never bill twice.
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

    def export(self, identity, output, *, cancelled=lambda: False, guard=lambda: None):
        with self.jobs.lock(identity):
            job = self._load(identity)
            if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
                raise ValueError('Finish and verify all chunks before publishing the translation.')
            self._output_budget(job)
            desk = self._source_context(job, cancelled=cancelled, guard=guard)
            binding = self._binding(job)
            text_binding = self._text_binding(job)
            translated = _assemble_translation(job['chunks'], job['receipts'])
            reading = ReadingDocument(job['source_text'], title=job['title'],
                                      source_anchor=binding['anchor'] if binding else None)
            reading.replace(0, len(reading.text), translated,
                            label='Machine translation derivative; not a source quotation')
            target = local_path(output, must_exist=False)
            local_path(target.parent)
            if self.library is not None:
                _filename(target.name)
                target, _, _ = _destination(target, self.library.root)
            # This existing writer guard also protects archival. No writable
            # database transaction spans source hashing or output publication.
            with exclusive_writer(self.library.root) if desk is not None else nullcontext():
                # The writer blocks application mutations, while an external
                # file edit still requires a fresh retained-original hash.
                if desk is not None:
                    desk = self._source_context(job, index=0, cancelled=cancelled, guard=guard)
                self._source_current(desk, cancelled=cancelled, guard=guard)
                target.mkdir(mode=0o700)  # Exclusive; never overwrite a prior export.
                reading.save(target / 'translation.reading.json')
                self._source_current(desk, cancelled=cancelled, guard=guard)
                reading.export(target / 'translated.txt', cancelled=cancelled)
                reading.export(target / 'original.txt', original=True, cancelled=cancelled)
                bilingual = '\n\n'.join('ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                                        for chunk, row in zip(job['chunks'], job['receipts']))
                with new_output(target / 'bilingual.txt', cancelled=cancelled) as staged:
                    staged.write_bytes(bilingual.encode('utf-8'))
                paths = ('translation.reading.json', 'translated.txt', 'original.txt', 'bilingual.txt')
                report = {'schema': 'carr-translation-export/1', 'job_id': identity,
                    'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
                    'target_language': job['target_language'], 'provider_adapter': job['profile']['adapter'],
                    'provider_endpoint': job['profile']['endpoint'], 'glossary': job.get('glossary'),
                    'model': job['profile']['model'], 'source_is_immutable': True, 'quotation_authority': False,
                    'automated_translation_quality': 'not independently qualified',
                    'decoded_text_binding': text_binding,
                    'chunks': [{key: value for key, value in row.items() if key != 'text'} for row in job['receipts']],
                    'files': {name: digest(target / name) for name in paths}}
                if binding:
                    report['source_anchor'] = binding['anchor']
                    self._source_context(job, index=len(job['chunks']) - 1,
                                         cancelled=cancelled, guard=guard)
                self._source_current(desk, cancelled=cancelled, guard=guard)
                atomic_json(target / 'translation-provenance.json', report)
                return {'status': 'complete', 'path': str(target), 'files': report['files']}

    def list(self):
        return tuple(self._load(identity) for identity in self.jobs.identities())
