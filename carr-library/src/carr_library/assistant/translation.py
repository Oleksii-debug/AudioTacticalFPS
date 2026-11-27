"""Explicit Google Basic v2 translation through the application's shared gateway.

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
    CACHE_SCHEMA, MAX_CACHE_BYTES, MAX_OUTPUT_CHARACTERS, SCHEMA, TranslationStore,
    allocations, approval_from_dict, split_text, text_hash, validate_language_cache)
from ..ingest.writer_guard import exclusive_writer
from ..providers import (
    BudgetLedger, GoogleTranslationRequest, JobApproval, ProfileStore, ProviderError,
    ProviderGateway, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT,
    decode_translation_response, translation_receipt_sha256)
from ..reader.narration_text import NarrationDraft
from ..research.export_file import _destination, _filename, write_export

ENDPOINT = GOOGLE_TRANSLATION_ENDPOINT


def translation_approval(profile, value, source, target, budget_micros):
    """Only call after the user reviews these exact data and integer USD bound."""
    if not isinstance(profile, ProviderProfile) or profile.provider != 'google-translation-v2':
        raise ValueError('Choose a Google Basic v2 profile.')
    parts = split_text(value)
    hashes = tuple(GoogleTranslationRequest(part, source, target).fingerprint(profile)
                   for part in parts if part.strip())
    return JobApproval(uuid4().hex, profile.fingerprint, hashes,
                       budget_micros=budget_micros, max_calls=len(hashes),
                       currency='USD', mode='APPROVED_REMOTE')


def language_approval(profile):
    request = GoogleTranslationRequest(operation='languages')
    return JobApproval(uuid4().hex, profile.fingerprint, (request.fingerprint(profile),),
                       budget_micros=0, max_calls=1, currency='USD', mode='APPROVED_REMOTE')


def _json_value(value):
    return json.loads(canonical(value))


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
    def __init__(self, library_root, *, gateway=None, profiles=None):
        self.jobs = TranslationStore(library_root)
        self.root, self.directory = self.jobs.root, self.jobs.directory
        self.profiles = profiles or ProfileStore(self.root / 'api-connections.json')
        if local_path(self.profiles.path, must_exist=False) != self.root / 'api-connections.json':
            raise ValueError('Translation must use this library\'s existing connection profiles.')
        if gateway is None:
            with exclusive_writer(self.root):
                gateway = ProviderGateway(BudgetLedger(self.root / 'api-operations.sqlite'),
                                          vault=self.profiles.vault)
        if local_path(gateway.ledger.path) != self.root / 'api-operations.sqlite':
            raise ValueError('Translation must use this library\'s existing provider ledger.')
        self.gateway = gateway
        self._ledger_identity()

    def _ledger_identity(self):
        info = regular(self.gateway.ledger.path)
        return [info.st_dev, info.st_ino]

    def available_profiles(self):
        return tuple(profile for profile in self.profiles.profiles()
                     if profile.provider == 'google-translation-v2')

    def _current_profile(self, profile):
        if not isinstance(profile, ProviderProfile) or profile.provider != 'google-translation-v2':
            raise ValueError('Choose a Google Basic v2 profile.')
        current = next((item for item in self.available_profiles() if item.id == profile.id), None)
        if current is None or current.fingerprint != profile.fingerprint or not current.credential_ref:
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
        if value['profile_fingerprint'] != profile.fingerprint:
            raise ValueError('Explicitly refresh languages for this exact account and model.')
        return tuple(value['languages'])

    def refresh_languages(self, profile, *, approval, cancelled=lambda: False):
        self._current_profile(profile)
        request = GoogleTranslationRequest(operation='languages')
        if (not isinstance(approval, JobApproval) or approval.profile_fingerprint != profile.fingerprint
                or approval.request_hashes != (request.fingerprint(profile),)
                or approval.max_calls != 1 or approval.budget_micros != 0
                or approval.currency != 'USD' or approval.mode != 'APPROVED_REMOTE'
                or approval.allow_loopback or not re.fullmatch('[0-9a-f]{32}', approval.job_id)):
            raise PermissionError('Approve only this account\'s supported-language query; no selected text is sent.')
        raw = self.gateway.perform(profile, request, approval=approval, operation_id=approval.job_id,
                                   estimated_micros=0, cancelled=cancelled)
        rows = decode_translation_response(request, raw)['languages']
        value = {'schema': CACHE_SCHEMA, 'profile_id': profile.id,
            'profile_fingerprint': profile.fingerprint, 'endpoint': profile.endpoint, 'model': profile.model,
            'fetched_at': now(), 'languages': rows, 'languages_sha256': text_hash(canonical(rows)),
            'operation_id': approval.job_id, 'request_hash': request.fingerprint(profile),
            'response_sha256': sha256(raw).hexdigest()}
        self.jobs.save_cache(profile, value)
        return tuple(rows)

    def preview(self, profile, value, source, target, *, rate_per_million=None):
        self._current_profile(profile)
        parts = split_text(value)
        first = next(part for part in parts if part.strip())
        GoogleTranslationRequest(first, source, target)
        rows = self.languages(profile)
        codes = {row['language'] for row in rows}
        if not codes:
            raise ValueError('Explicitly fetch the supported language list before translating.')
        if target not in codes or (source != 'auto' and source not in codes):
            raise ValueError('This source/target pair is not declared supported by this account\'s NMT model.')
        estimate = None
        if rate_per_million not in (None, ''):
            try:
                rate = Decimal(rate_per_million)
                if not rate.is_finite() or not 0 <= rate <= Decimal('1000000'):
                    raise ValueError
                estimate = str((rate * len(value) / Decimal(1_000_000)).quantize(
                    Decimal('0.000001'), rounding=ROUND_UP))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError('Enter a finite nonnegative price per million characters in USD.') from None
        return {'source_sha256': text_hash(value), 'characters': len(value),
            'calls': sum(bool(part.strip()) for part in parts), 'chunks': len(parts),
            'source': source, 'target': target, 'estimated_cost': estimate,
            'language_catalog_sha256': text_hash(canonical(list(rows))),
            'estimate_basis': 'owner-entered rate; not a provider quote or a provider-enforced spending cap',
            'preservation': 'plain-text derivative; layout, glossaries, Advanced v3 and local models are separate'}

    def submit(self, profile, value, source, target, *, approval, title='Translation'):
        preview = self.preview(profile, value, source, target)
        if not isinstance(approval, JobApproval):
            raise PermissionError('Review a fresh exact translation approval.')
        parts = split_text(value)
        stamp = now()
        job = {'schema': SCHEMA, 'id': approval.job_id, 'library_root': str(self.root),
            'ledger_identity': self._ledger_identity(), 'created': stamp, 'updated': stamp, 'status': 'queued',
            'profile': _json_value(asdict(profile)), 'title': title, 'source_text': value,
            'source_sha256': preview['source_sha256'], 'source_language': source, 'target_language': target,
            'language_catalog_sha256': preview['language_catalog_sha256'],
            'approval': _json_value(asdict(approval)), 'allocations': allocations(parts, approval.budget_micros),
            'receipts': [], 'pending': None}
        # Validation proves exact profile/request hashes, call count and budget.
        # It precedes publication and every possible provider call.
        self.jobs.save(job, new=True)
        return job['id']

    def _binding(self, job):
        if job['library_root'] != str(self.root) or job['ledger_identity'] != self._ledger_identity():
            raise PermissionError('The library or provider ledger was moved/restored. Review a new job; saved remote permission was not rebound.')
        return self._current_profile(ProviderProfile.from_dict(job['profile']))

    def _operations(self, job):
        return {row['id']: row for row in self.gateway.ledger.report(job['id'])}

    def _check_receipts(self, job, operations):
        integrity = []
        for receipt in job['receipts']:
            if receipt['operation_id'] is None:
                integrity.append('original-whitespace')
                continue
            row = operations.get(receipt['operation_id'])
            if (row is None or row['state'] != 'complete' or row['job'] != job['id']
                    or row['request_hash'] != receipt['request_hash']
                    or row['output_sha256'] != receipt['response_sha256']
                    or row['estimate_micros'] != job['allocations'][receipt['index']]):
                raise PermissionError('Accepted translation receipts disagree with the live provider ledger. No request was repeated.')
            key = 'decoded_translation_sha256'
            if key in receipt or row.get(key) is not None:
                expected = translation_receipt_sha256(receipt['text'], receipt['detected_source'])
                if receipt.get(key) != expected or row.get(key) != expected:
                    raise PermissionError('Decoded translation disagrees with the live provider ledger. No request was repeated.')
                integrity.append('decoded-ledger-bound')
            else:
                integrity.append('legacy-wire-only')
        return integrity

    def _receipt_integrity(self, job):
        # Passive restored derivatives remain usable without reconnecting a
        # profile or rebinding remote authority. Their self-consistent copies
        # must not be presented as independently checked against a live ledger.
        live = False
        if job['library_root'] == str(self.root):
            try:
                live = job['ledger_identity'] == self._ledger_identity()
            except FileNotFoundError:
                pass
        if live:
            return self._check_receipts(job, self._operations(job))
        return ['original-whitespace' if row['operation_id'] is None else 'archived-unverified'
                for row in job['receipts']]

    @staticmethod
    def _summary(job):
        return {'id': job['id'], 'title': job['title'], 'status': job['status'],
                'completed': len(job['receipts']), 'total': len(split_text(job['source_text'])),
                'source': job['source_language'], 'target': job['target_language'],
                'billed_cost': 'unknown; local amounts are reservations only'}

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None):
        with self.jobs.lock(identity):
            job = self.jobs.load(identity)
            if job['status'] == 'complete':
                self._receipt_integrity(job)
                return self._summary(job)
            profile = self._binding(job)
            operations = self._operations(job)
            self._check_receipts(job, operations)
            if job['pending'] is not None:
                pending = job['pending']
                row = operations.get(pending['operation_id'])
                index = pending['index']
                expected = GoogleTranslationRequest(
                    split_text(job['source_text'])[index], job['source_language'], job['target_language'])
                if (row is None or row['state'] != 'cancelled_before_send' or row['job'] != identity
                        or row['request_hash'] != expected.fingerprint(profile)
                        or row['estimate_micros'] != job['allocations'][index]):
                    raise ProviderError('A previous translation request is unresolved or its paid output is missing. Review the charge; it was not retried.')
            approval = approval_from_dict(job['approval'])
            parts = split_text(job['source_text'])
            job['status'] = 'running'
            self.jobs.save(job)
            try:
                for index in range(len(job['receipts']), len(parts)):
                    if cancelled():
                        job['status'] = 'paused'
                        self.jobs.save(job)
                        return self._summary(job)
                    chunk = parts[index]
                    if chunk.strip():
                        profile = self._binding(job)
                        request = GoogleTranslationRequest(chunk, job['source_language'], job['target_language'])
                        operation = identity + '_' + str(index)
                        job['pending'] = {'index': index, 'operation_id': operation}
                        self.jobs.save(job)  # Durable intent before reservation, credentials or HTTP.
                        raw = self.gateway.perform(profile, request, approval=approval, operation_id=operation,
                            estimated_micros=job['allocations'][index], cancelled=cancelled)
                        result = decode_translation_response(request, raw)
                        row = {'index': index, 'source_sha256': text_hash(chunk), 'text': result['text'],
                            'translation_sha256': text_hash(result['text']), 'detected_source': result['detected_source'],
                            'operation_id': operation, 'request_hash': request.fingerprint(profile),
                            'response_sha256': sha256(raw).hexdigest(),
                            'decoded_translation_sha256': translation_receipt_sha256(
                                result['text'], result['detected_source'])}
                    else:
                        row = {'index': index, 'source_sha256': text_hash(chunk), 'text': chunk,
                            'translation_sha256': text_hash(chunk), 'detected_source': None,
                            'operation_id': None, 'request_hash': None, 'response_sha256': None}
                    if sum(len(item['text']) for item in job['receipts']) + len(row['text']) > MAX_OUTPUT_CHARACTERS:
                        raise ValueError('The accepted output exceeds the translation storage budget. Do not repeat this request.')
                    job['receipts'].append(row)
                    job['pending'] = None
                    self.jobs.save(job)  # Accepted output is durable before cancellation/progress.
                    progress(index + 1, len(parts), 'Accepted translation chunks')
                job['status'] = 'complete'
                self.jobs.save(job)
            except BaseException as exc:
                pending = job['pending']
                unsent = False
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
            receipt_integrity = self._receipt_integrity(job)
            target = local_path(output, must_exist=False)
            _filename(target.name)
            parent = local_path(target.parent)
            _destination(parent / ('.translation-location-check-' + uuid4().hex), self.root)
            target.mkdir(mode=0o700)  # Exclusive, never reuse an original/prior export folder.
            translated = _assemble_translation(split_text(job['source_text']), job['receipts'])
            draft = NarrationDraft(job['source_text'], source_id='translation-source:' + job['source_sha256'])
            draft.replace_range(0, len(draft.text), translated,
                                reason='Machine translation derivative; not a source quotation')
            bilingual = 'MACHINE TRANSLATION DERIVATIVE; NOT SOURCE QUOTATION AUTHORITY\n\n' + '\n\n'.join(
                'ORIGINAL\n' + chunk + '\nTRANSLATION\n' + row['text']
                for chunk, row in zip(split_text(job['source_text']), job['receipts']))
            payloads = {'translation.narration.json': canonical(draft.to_dict()) + '\n',
                        'translated.txt': translated, 'original.txt': job['source_text'], 'bilingual.txt': bilingual}
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
                'chunks': [dict({key: value for key, value in row.items() if key != 'text'},
                                decoded_receipt_integrity=integrity)
                           for row, integrity in zip(job['receipts'], receipt_integrity)],
                'files': hashes}
            # A provenance manifest is the final complete-export marker.
            write_export(target / 'translation-provenance.json', canonical(report) + '\n',
                         private_root=self.root, cancelled=cancelled, require_absent=True)
            return {'status': 'complete', 'path': str(target), 'files': hashes}

    def list(self):
        return tuple(self._summary(self.jobs.load(identity)) for identity in self.jobs.identities())
