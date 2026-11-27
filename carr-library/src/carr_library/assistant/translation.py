"""Explicit Google and selected loopback translation through the shared gateway.

Source text is immutable; accepted chunks survive restart. A durable request
intent precedes each call. A lost ledger, uncertain charge or missing paid
output blocks resubmission instead of recreating remote authority.
"""
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_UP
from hashlib import sha256
import json
import re
from pathlib import Path
from uuid import uuid4

from .models import canonical, local_path
from .state import now, read_json, regular
from .translation_state import (
    ADVANCED_CACHE_SCHEMA, ADVANCED_SCHEMA, CACHE_SCHEMA, LOCAL_CACHE_SCHEMA, LOCAL_SCHEMA, MAX_CACHE_BYTES,
    MAX_OUTPUT_CHARACTERS, SCHEMA, STRUCTURED_SCHEMA, TranslationStore, job_parts, is_advanced_job,
    allocations, approval_from_dict, split_text, text_hash, validate_language_cache)
from .translation_structure import parts_and_mask, structure_hash, validate_structure, export_structure
from ..ingest.writer_guard import exclusive_writer
from ..providers import (
    BudgetLedger, GoogleTranslationRequest, JobApproval, ProfileStore, ProviderError,
    ProviderGateway, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT,
    GOOGLE_TRANSLATION_PROVIDERS, TRANSLATION_PROVIDERS, LOCAL_TRANSLATION_PROVIDER,
    decode_translation_response, translation_request)
from ..reader.narration_text import NarrationDraft
from ..research.export_file import _destination, _filename, write_export

ENDPOINT = GOOGLE_TRANSLATION_ENDPOINT


def _local_structure(profile, value, structure):
    if isinstance(profile, ProviderProfile) and profile.provider == LOCAL_TRANSLATION_PROVIDER and structure is None:
        from .translation_structure import capture_text
        return capture_text(value)
    return structure


def _cache_schema(profile):
    return (LOCAL_CACHE_SCHEMA if profile.provider == LOCAL_TRANSLATION_PROVIDER else
            ADVANCED_CACHE_SCHEMA if profile.provider == 'google-translation-v3' else CACHE_SCHEMA)


def translation_approval(profile, value, source, target, budget_micros, *, glossary='', structure=None):
    """Only call after the user reviews these exact data and integer USD bound."""
    if not isinstance(profile, ProviderProfile) or profile.provider not in TRANSLATION_PROVIDERS:
        raise ValueError('Choose an installed translation adapter and explicit connection profile.')
    local = profile.provider == LOCAL_TRANSLATION_PROVIDER
    if local and (type(budget_micros) is not int or budget_micros != 0):
        raise ValueError('The local translator uses zero remote reservation; enter a zero budget.')
    structure = _local_structure(profile, value, structure)
    parts, mask = parts_and_mask(value, structure)
    hashes = tuple(translation_request(profile, part, source, target, glossary=glossary).fingerprint(profile)
                   for part, remote in zip(parts, mask) if remote)
    return JobApproval(uuid4().hex, profile.fingerprint, hashes,
                       budget_micros=budget_micros, max_calls=len(hashes),
                       currency='USD', mode='LOCAL_ONLY' if local else 'APPROVED_REMOTE',
                       allow_loopback=local)


def language_approval(profile):
    request = translation_request(profile, operation='languages')
    return JobApproval(uuid4().hex, profile.fingerprint, (request.fingerprint(profile),),
                       budget_micros=0, max_calls=1, currency='USD',
                       mode='LOCAL_ONLY' if profile.local else 'APPROVED_REMOTE',
                       allow_loopback=profile.local)


def _json_value(value):
    return json.loads(canonical(value))


class TranslationService:
    def __init__(self, library_root, *, gateway=None, profiles=None):
        self.jobs = TranslationStore(library_root)
        self.root, self.directory = self.jobs.root, self.jobs.directory
        self.profiles = profiles or ProfileStore(self.root / 'api-connections.json')
        if Path(self.profiles.path).absolute() != self.root / 'api-connections.json':
            raise ValueError('Translation must use this library\'s existing connection profiles.')
        if gateway is None:
            with exclusive_writer(self.root):
                gateway = ProviderGateway(BudgetLedger(self.root / 'api-operations.sqlite'),
                                          vault=self.profiles.vault)
        if Path(gateway.ledger.path).absolute() != self.root / 'api-operations.sqlite':
            raise ValueError('Translation must use this library\'s existing provider ledger.')
        self.gateway = gateway
        self._ledger_identity()

    def _ledger_identity(self):
        info = regular(self.gateway.ledger.path)
        return [info.st_dev, info.st_ino]

    def available_profiles(self):
        return tuple(profile for profile in self.profiles.profiles()
                     if profile.provider in TRANSLATION_PROVIDERS)

    def _current_profile(self, profile):
        if not isinstance(profile, ProviderProfile) or profile.provider not in TRANSLATION_PROVIDERS:
            raise ValueError('Choose an installed translation adapter and explicit connection profile.')
        current = next((item for item in self.available_profiles() if item.id == profile.id), None)
        if (current is None or current.fingerprint != profile.fingerprint
                or not current.local and not current.credential_ref):
            raise PermissionError('This account changed or was disconnected. Review the current profile before sending text.')
        return current

    def _languages_path(self, profile):
        return self.jobs.cache_path(profile)

    def languages(self, profile):
        self._current_profile(profile)
        path = self._languages_path(profile)
        if not path.exists():
            return ()
        value = validate_language_cache(read_json(path, maximum=MAX_CACHE_BYTES), profile_id=profile.id)
        expected_schema = _cache_schema(profile)
        if (value['profile_fingerprint'] != profile.fingerprint or value['schema'] != expected_schema
                or value['endpoint'] != profile.endpoint or value['model'] != profile.model):
            raise ValueError('Explicitly refresh languages for this exact account and model.')
        return tuple(value['languages'])

    def refresh_languages(self, profile, *, approval, cancelled=lambda: False):
        self._current_profile(profile)
        request = translation_request(profile, operation='languages')
        if (not isinstance(approval, JobApproval) or approval.profile_fingerprint != profile.fingerprint
                or approval.request_hashes != (request.fingerprint(profile),)
                or approval.max_calls != 1 or approval.budget_micros != 0
                or approval.currency != 'USD'
                or approval.mode != ('LOCAL_ONLY' if profile.local else 'APPROVED_REMOTE')
                or approval.allow_loopback is not profile.local
                or not re.fullmatch('[0-9a-f]{32}', approval.job_id)):
            raise PermissionError('Approve only this account\'s supported-language query; no selected text is sent.')
        raw = self.gateway.perform(profile, request, approval=approval, operation_id=approval.job_id,
                                   estimated_micros=0, cancelled=cancelled)
        rows = decode_translation_response(request, raw, profile=profile)['languages']
        schema = _cache_schema(profile)
        value = {'schema': schema, 'profile_id': profile.id,
            'profile_fingerprint': profile.fingerprint, 'endpoint': profile.endpoint, 'model': profile.model,
            'fetched_at': now(), 'languages': rows, 'languages_sha256': text_hash(canonical(rows)),
            'operation_id': approval.job_id, 'request_hash': request.fingerprint(profile),
            'response_sha256': sha256(raw).hexdigest()}
        self.jobs.save_cache(profile, value)
        return tuple(rows)

    def preview(self, profile, value, source, target, *, rate_per_million=None, glossary='', structure=None):
        self._current_profile(profile)
        structure = _local_structure(profile, value, structure)
        parts, mask = parts_and_mask(value, structure)
        first = next(part for part, remote in zip(parts, mask) if remote)
        billable = sum(len(part) for part, remote in zip(parts, mask) if remote)
        translation_request(profile, first, source, target, glossary=glossary)
        rows = self.languages(profile)
        sources = {row['language'] for row in rows if row.get('source', True)}
        targets = {row['language'] for row in rows if row.get('target', True)}
        if not rows:
            raise ValueError('Explicitly fetch the supported language list before translating.')
        if target not in targets or (source != 'auto' and source not in sources):
            raise ValueError('This source/target pair is not declared supported by this account\'s NMT model.')
        local = profile.provider == LOCAL_TRANSLATION_PROVIDER
        if local:
            from .translation_local import require_pair
            require_pair(list(rows), source, target)
            if rate_per_million not in (None, ''):
                raise ValueError('The local translator has no remote price; leave the contract price empty.')
        estimate = '0' if local else None
        if rate_per_million not in (None, ''):
            try:
                rate = Decimal(rate_per_million)
                if not rate.is_finite() or not 0 <= rate <= Decimal('1000000'):
                    raise ValueError
                estimate = str((rate * billable / Decimal(1_000_000)).quantize(
                    Decimal('0.000001'), rounding=ROUND_UP))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError('Enter a finite nonnegative price per million characters in USD.') from None
        return {'source_sha256': text_hash(value), 'characters': len(value),
            'calls': sum(mask), 'chunks': len(parts), 'billable_characters': billable,
            'structure_sha256': None if structure is None else structure_hash(structure),
            'documents': 1 if structure is None else len(structure['documents']),
            'source': source, 'target': target, 'estimated_cost': estimate,
            'language_catalog_sha256': text_hash(canonical(list(rows))),
            'estimate_basis': ('local endpoint; zero remote reservation, no runtime or quality attestation' if local
                               else 'owner-entered rate; not a provider quote or a provider-enforced spending cap'),
            'glossary': glossary, 'provider': profile.provider, 'endpoint': profile.endpoint, 'model': profile.model,
            'preservation': ('plain-text derivative; original text is retained; document layout and translation quality are not guaranteed'
                if structure is None else 'ordered documents, exact original sections, paragraph boundaries and explicit heading levels; other layout and translation quality are not qualified')}

    def submit(self, profile, value, source, target, *, approval, title='Translation', glossary='',
               structure=None, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Translation preparation cancelled before a job was committed.')
        structure = _local_structure(profile, value, structure)
        if structure is not None:
            structure = _json_value(validate_structure(structure, value))
            self._verify_structure_sources(structure, cancelled=cancelled)
        preview = self.preview(profile, value, source, target, glossary=glossary, structure=structure)
        if not isinstance(approval, JobApproval):
            raise PermissionError('Review a fresh exact translation approval.')
        parts, mask = parts_and_mask(value, structure)
        stamp = now()
        schema = (LOCAL_SCHEMA if profile.provider == LOCAL_TRANSLATION_PROVIDER else
                  STRUCTURED_SCHEMA if structure is not None else
                  ADVANCED_SCHEMA if profile.provider == 'google-translation-v3' else SCHEMA)
        job = {'schema': schema, 'id': approval.job_id, 'library_root': str(self.root),
            'ledger_identity': self._ledger_identity(), 'created': stamp, 'updated': stamp, 'status': 'queued',
            'profile': _json_value(asdict(profile)), 'title': title, 'source_text': value,
            'source_sha256': preview['source_sha256'], 'source_language': source, 'target_language': target,
            'language_catalog_sha256': preview['language_catalog_sha256'],
            'approval': _json_value(asdict(approval)), 'allocations': allocations(parts, approval.budget_micros, mask=mask),
            'receipts': [], 'pending': None}
        if schema in (ADVANCED_SCHEMA, STRUCTURED_SCHEMA, LOCAL_SCHEMA):
            job['glossary'] = glossary
        if structure is not None:
            job['structure'] = structure
        # Validation proves exact profile/request hashes, call count and budget.
        # It precedes publication and every possible provider call.
        if cancelled():
            raise InterruptedError('Translation preparation cancelled before a job was committed.')
        self.jobs.save(job, new=True)
        return job['id']

    def _verify_structure_sources(self, structure, *, cancelled=lambda: False):
        if structure is None:
            return
        validate_structure(structure)
        if any(item['context'] is not None for item in structure['documents']):
            # Never create a new empty source library merely to resume a
            # historical remote approval. Existing source identity is required.
            regular(self.root / 'library.sqlite')
            from ..application import open_existing_library
            from .translation_structure import validate_current_sources
            validate_current_sources(open_existing_library(self.root, cancelled=cancelled),
                                     structure, cancelled=cancelled)

    def _binding(self, job):
        if job['library_root'] != str(self.root) or job['ledger_identity'] != self._ledger_identity():
            raise PermissionError('The library or provider ledger was moved/restored. Review a new job; saved remote permission was not rebound.')
        return self._current_profile(ProviderProfile.from_dict(job['profile']))

    def _operations(self, job):
        return {row['id']: row for row in self.gateway.ledger.report(job['id'])}

    def _check_receipts(self, job, operations):
        for receipt in job['receipts']:
            if receipt['operation_id'] is None:
                continue
            row = operations.get(receipt['operation_id'])
            if (row is None or row['state'] != 'complete' or row['job'] != job['id']
                    or row['request_hash'] != receipt['request_hash']
                    or row['output_sha256'] != receipt['response_sha256']
                    or row['estimate_micros'] != job['allocations'][receipt['index']]):
                raise PermissionError('Accepted translation receipts disagree with the live provider ledger. No request was repeated.')

    @staticmethod
    def _summary(job):
        return {'id': job['id'], 'title': job['title'], 'status': job['status'],
                'completed': len(job['receipts']), 'total': len(job_parts(job)[0]),
                'source': job['source_language'], 'target': job['target_language'],
                'billed_cost': 'unknown; local amounts are reservations only'}

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None,
               expected_job_sha256=None):
        with self.jobs.lock(identity):
            job = self.jobs.load(identity)
            if expected_job_sha256 is not None and (
                    not isinstance(expected_job_sha256, str)
                    or not re.fullmatch('[0-9a-f]{64}', expected_job_sha256)
                    or sha256(canonical(job).encode('utf-8')).hexdigest() != expected_job_sha256):
                raise PermissionError('The reviewed translation job changed before run. Inspect and approve it again.')
            if job['status'] == 'complete':
                return self._summary(job)
            profile = self._binding(job)
            operations = self._operations(job)
            self._check_receipts(job, operations)
            if job['pending'] is not None:
                pending = job['pending']
                row = operations.get(pending['operation_id'])
                index = pending['index']
                expected = translation_request(
                    profile, job_parts(job)[0][index], job['source_language'],
                    job['target_language'], glossary=job.get('glossary', ''))
                if (row is None or row['state'] != 'cancelled_before_send' or row['job'] != identity
                        or row['request_hash'] != expected.fingerprint(profile)
                        or row['estimate_micros'] != job['allocations'][index]):
                    raise ProviderError('A previous translation request is unresolved or its paid output is missing. Review the charge; it was not retried.')
            approval = approval_from_dict(job['approval'])
            parts, mask = job_parts(job)
            job['status'] = 'running'
            self.jobs.save(job)
            try:
                for index in range(len(job['receipts']), len(parts)):
                    if cancelled():
                        job['status'] = 'paused'
                        self.jobs.save(job)
                        return self._summary(job)
                    chunk = parts[index]
                    if mask[index]:
                        self._verify_structure_sources(job.get('structure'), cancelled=cancelled)
                        profile = self._binding(job)
                        local_pairs = None
                        if profile.provider == LOCAL_TRANSLATION_PROVIDER:
                            from .translation_local import require_pair
                            local_pairs = self.languages(profile)
                            if text_hash(canonical(list(local_pairs))) != job['language_catalog_sha256']:
                                raise ValueError('The local language inventory changed. Review a new translation job.')
                            require_pair(list(local_pairs), job['source_language'], job['target_language'])
                        request = translation_request(profile, chunk, job['source_language'],
                                                      job['target_language'], glossary=job.get('glossary', ''))
                        operation = identity + '_' + str(index)
                        job['pending'] = {'index': index, 'operation_id': operation}
                        self.jobs.save(job)  # Durable intent before reservation, credentials or HTTP.
                        raw = self.gateway.perform(profile, request, approval=approval, operation_id=operation,
                            estimated_micros=job['allocations'][index], cancelled=cancelled)
                        result = decode_translation_response(request, raw, profile=profile)
                        if local_pairs is not None:
                            require_pair(list(local_pairs), job['source_language'], job['target_language'],
                                         detected=result['detected_source'])
                        row = {'index': index, 'source_sha256': text_hash(chunk), 'text': result['text'],
                            'translation_sha256': text_hash(result['text']), 'detected_source': result['detected_source'],
                            'operation_id': operation, 'request_hash': request.fingerprint(profile),
                            'response_sha256': sha256(raw).hexdigest()}
                    else:
                        row = {'index': index, 'source_sha256': text_hash(chunk), 'text': chunk,
                            'translation_sha256': text_hash(chunk), 'detected_source': None,
                            'operation_id': None, 'request_hash': None, 'response_sha256': None}
                    if is_advanced_job(job):
                        row.update(reported_model=result['reported_model'] if mask[index] else None,
                                   glossary=result['glossary'] if mask[index] else None)
                    if sum(len(item['text']) for item in job['receipts']) + len(row['text']) > MAX_OUTPUT_CHARACTERS:
                        raise ValueError('The accepted output exceeds the translation storage budget. Do not repeat this request.')
                    job['receipts'].append(row)
                    job['pending'] = None
                    self.jobs.save(job)  # Accepted output is durable before cancellation/progress.
                    try:
                        progress(index + 1, len(parts), 'Accepted translation chunks')
                    except Exception:
                        # Observers cannot undo durable accepted output or
                        # manufacture unresolved provider work.
                        pass
                job['status'] = 'complete'
                self.jobs.save(job)
            except BaseException as exc:
                pending = job['pending']
                # Source validation precedes request intent. Cancellation at
                # that boundary has no uncertain remote work to reconcile.
                unsent = pending is None
                if pending is not None and self._ledger_identity() == job['ledger_identity']:
                    row = self._operations(job).get(pending['operation_id'])
                    unsent = row is None or row['state'] == 'cancelled_before_send'
                    if row is None:
                        job['pending'] = None
                job['status'] = 'paused' if cancelled() and unsent else 'attention'
                self.jobs.save(job)
                if job['status'] == 'paused' and not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    return self._summary(job)
                raise
            return self._summary(job)

    def charge_report(self, identity):
        job = self.jobs.load(identity)
        if job['library_root'] != str(self.root) or job['ledger_identity'] != self._ledger_identity():
            raise ValueError('The original provider ledger is unavailable; saved receipts remain inspectable offline.')
        return {'job': self._summary(job), 'operations': self.gateway.ledger.report(job['id']),
                'qualification': 'local reservations and response hashes, not independently verified provider bills'}

    def export(self, identity, output, *, cancelled=lambda: False):
        with self.jobs.lock(identity):
            job = self.jobs.load(identity)
            if job['status'] != 'complete':
                raise ValueError('Finish every translation chunk before exporting.')
            if cancelled():
                raise InterruptedError('Translation export cancelled.')
            target = local_path(output, must_exist=False)
            _filename(target.name)
            parent = local_path(target.parent)
            _destination(parent / ('.translation-location-check-' + uuid4().hex), self.root)
            target.mkdir(mode=0o700)  # Exclusive, never reuse an original/prior export folder.
            translated = ''.join(row['text'] for row in job['receipts'])
            draft = NarrationDraft(job['source_text'], source_id='translation-source:' + job['source_sha256'])
            draft.replace_range(0, len(draft.text), translated,
                                reason='Machine translation derivative; not a source quotation')
            bilingual = 'MACHINE TRANSLATION DERIVATIVE; NOT SOURCE QUOTATION AUTHORITY\n\n' + '\n\n'.join(
                'ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                for chunk, row in zip(job_parts(job)[0], job['receipts']))
            payloads = {'translation.narration.json': canonical(draft.to_dict()) + '\n',
                        'translated.txt': translated, 'original.txt': job['source_text'], 'bilingual.txt': bilingual}
            if job.get('structure') is not None:
                payloads.update(export_structure(job['structure'], job['receipts']))
            hashes = {}
            for name, value in payloads.items():
                write_export(target / name, value, private_root=self.root,
                             cancelled=cancelled, require_absent=True)
                hashes[name] = sha256(value.encode('utf-8')).hexdigest()
            report = {'schema': 'carr-translation-export/1', 'job_id': identity,
                'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
                'target_language': job['target_language'], 'provider': job['profile']['provider'],
                'endpoint': job['profile']['endpoint'], 'model': job['profile']['model'],
                'source_is_immutable': True, 'quotation_authority': False,
                'automated_translation_quality': 'not independently qualified',
                'billed_cost': 'unknown; local amounts are reservations only',
                'chunks': [{key: value for key, value in row.items() if key != 'text'} for row in job['receipts']],
                'files': hashes}
            if is_advanced_job(job):
                report.update(schema='carr-translation-export/2', glossary=job['glossary'])
            if job.get('structure') is not None:
                report.update(schema='carr-translation-export/3', structure_sha256=structure_hash(job['structure']),
                    documents=len(job['structure']['documents']), structure_profile='exact-sections-and-heading-levels')
            if job['profile']['provider'] == LOCAL_TRANSLATION_PROVIDER:
                report.update(schema='carr-translation-export/4',
                    local_runtime_identity='not attested by the local API',
                    local_protocol='LibreTranslate selected loopback origin; credentials forbidden',
                    billed_cost='zero remote reservation; runtime operating costs not measured')
            # A provenance manifest is the final complete-export marker.
            write_export(target / 'translation-provenance.json', canonical(report) + '\n',
                         private_root=self.root, cancelled=cancelled, require_absent=True)
            return {'status': 'complete', 'path': str(target), 'files': hashes}

    def list(self):
        return tuple(self._summary(self.jobs.load(identity)) for identity in self.jobs.identities())
