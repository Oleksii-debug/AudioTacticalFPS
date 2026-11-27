"""Explicit plain-text translation derivatives on the existing provider gateway.

Jobs retain accepted chunks. Uncertain billable calls are never retried.
"""
from dataclasses import asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
from pathlib import Path
from time import time
from uuid import uuid4

from .models import canonical, local_path, text
from .profiles import ProviderProfile, google_v3_parent
from .providers import Authorization, Gateway, money, _request
from .state import JobStore, atomic_json, read_json, now
from ..speech.reading import ReadingDocument
from ..speech._io import new_output, digest

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


def translation_approval(profile, value, source, target, budget, *, glossary=None):
    """Called only after explicit review; bind every text/model/glossary request."""
    hashes = []
    for chunk in split_text(value):
        if chunk.strip():
            _, body, _ = _request(profile, 'translation.text', translation_payload(chunk, source, target, glossary))
            hashes.append(_hash(canonical(body)))
    identity = uuid4().hex
    approval = Authorization(identity, identity, ((profile.id, profile.endpoint),),
        ('translation.text',), ('selected-excerpt',), str(money(budget)), len(hashes),
        time() + 24 * 3600, tuple(hashes))
    per_call = (money(budget) / Decimal(len(hashes))).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
    return approval, str(per_call)


class TranslationService:
    def __init__(self, directory, *, gateway=None):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.jobs = JobStore(self.directory / 'jobs')
        self.gateway = gateway or Gateway(self.directory / 'provider')

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

    def submit(self, profile, value, source, target, *, title='Translation', authorization, per_call_cost, glossary=None):
        preview = self.preview(profile, value, source, target, glossary=glossary)
        if (not isinstance(authorization, Authorization) or authorization.task_id != authorization.id
                or tuple(authorization.operations) != ('translation.text',)
                or tuple(authorization.data_classes) != ('selected-excerpt',)
                or tuple(tuple(p) for p in authorization.profiles) != ((profile.id, profile.endpoint),)
                or authorization.max_calls < preview['calls']):
            raise PermissionError('A fresh translation-specific approval is required.')
        parts = split_text(value)
        hashes = []
        for part in parts:
            if not part.strip():
                continue
            payload = translation_payload(part, source, target, glossary)
            _, body, _ = _request(profile, 'translation.text', payload)
            hashes.append(_hash(canonical(body)))
        if tuple(hashes) != tuple(authorization.request_hashes):
            raise PermissionError('Approval does not match this exact selected text and languages.')
        cost = money(per_call_cost)
        if cost * preview['calls'] > money(authorization.budget):
            raise PermissionError('The per-call reservation exceeds the approved total budget.')
        text(title, 'translation title', maximum=512)
        identity = uuid4().hex
        job = {'schema': 'carr-assistant-job/1', 'kind': 'translation/1', 'id': identity,
            'created': now(), 'status': 'queued', 'profile': profile.to_dict(), 'title': title,
            'source_text': value, 'source_sha256': preview['source_sha256'], 'source_language': source,
            'target_language': target, 'chunks': list(parts), 'receipts': [],
            'glossary': glossary,
            'authorization': asdict(authorization), 'per_call_cost': str(cost)}
        self.jobs.save(job)
        return identity

    def _load(self, identity):
        job = self.jobs.load(identity)
        profile = ProviderProfile(**job['profile'])
        advanced = profile.adapter == 'google-translation-v3'
        if (job.get('kind') != 'translation/1' or job.get('source_sha256') != _hash(job.get('source_text', ''))
                or job.get('chunks') != list(split_text(job['source_text'])) or not isinstance(job.get('receipts'), list)
                or len(job['receipts']) > len(job['chunks'])):
            raise ValueError('The translation checkpoint is invalid.')
        bodies = []
        requests = []
        for chunk in job['chunks']:
            if chunk.strip():
                _, body, _ = _request(profile, 'translation.text', translation_payload(
                    chunk, job['source_language'], job['target_language'], job.get('glossary')))
                bodies.append(_hash(canonical(body)))
                requests.append(bodies[-1])
            else:
                requests.append(None)
        authorization = Authorization(**job['authorization'])
        if (tuple(bodies) != tuple(authorization.request_hashes)
                or tuple(tuple(p) for p in authorization.profiles) != ((profile.id, profile.endpoint),)):
            raise ValueError('The translation checkpoint differs from its approved request.')
        for index, row in enumerate(job['receipts']):
            if (type(row) is not dict or row.get('index') != index
                    or row.get('source_sha256') != _hash(job['chunks'][index])
                    or type(row.get('text')) is not str or len(row['text']) > 200000
                    or row.get('translation_sha256') != _hash(row['text'])
                    or row.get('request_sha256') != requests[index]):
                raise ValueError('A completed translation chunk is damaged.')
            if advanced and job['chunks'][index].strip():
                expected_glossary = {'glossary': job['glossary'], 'ignoreCase': False} if job.get('glossary') else None
                if (row.get('reported_model') != google_v3_parent(profile.endpoint) + '/models/' + profile.model
                        or row.get('glossary_config') != expected_glossary):
                    raise ValueError('The Advanced v3 receipt has no verified model/glossary provenance.')
        return job

    def get(self, identity):
        """Return the validated durable scope for a saved-job selection."""
        return self._load(identity)

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None):
        with self.jobs.lock(identity):
            job = self._load(identity)
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
                        payload = translation_payload(chunk, job['source_language'], job['target_language'], job.get('glossary'))
                        result = self.gateway.request(profile, 'translation.text', payload,
                            task_id=authorization.task_id, data_class='selected-excerpt', authorization=authorization,
                            max_cost=job['per_call_cost'], operation_id=identity + '_' + str(index),
                            cancelled=cancelled, max_response_bytes=1024 * 1024)
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
        target = local_path(output, must_exist=False)
        local_path(target.parent)
        target.mkdir(mode=0o700)  # Exclusive: never overwrite an original or prior export.
        translated = ''.join(row['text'] for row in job['receipts'])
        reading = ReadingDocument(job['source_text'], title=job['title'])
        reading.replace(0, len(reading.text), translated, label='Machine translation derivative; not a source quotation')
        reading.save(target / 'translation.reading.json')
        reading.export(target / 'translated.txt')
        reading.export(target / 'original.txt', original=True)
        bilingual = '\n\n'.join('ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                                for chunk, row in zip(job['chunks'], job['receipts']))
        with new_output(target / 'bilingual.txt') as staged:
            staged.write_text(bilingual, encoding='utf-8')
        paths = ('translation.reading.json', 'translated.txt', 'original.txt', 'bilingual.txt')
        report = {'schema': 'carr-translation-export/1', 'job_id': identity,
            'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
            'target_language': job['target_language'], 'provider_adapter': job['profile']['adapter'],
            'provider_endpoint': job['profile']['endpoint'], 'glossary': job.get('glossary'),
            'model': job['profile']['model'], 'source_is_immutable': True, 'quotation_authority': False,
            'automated_translation_quality': 'not independently qualified',
            'chunks': [{key: value for key, value in row.items() if key != 'text'} for row in job['receipts']],
            'files': {name: digest(target / name) for name in paths}}
        atomic_json(target / 'translation-provenance.json', report)
        return {'status': 'complete', 'path': str(target), 'files': report['files']}

    def list(self):
        return tuple(self._load(identity) for identity in self.jobs.identities())
