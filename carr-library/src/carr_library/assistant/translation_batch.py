"""Bounded document batches over the existing translation jobs and provider ledger.

The batch manifest contains immutable child templates, not a second execution
queue. Every request, accepted chunk and uncertain attempt belongs to the normal
translation/1 child. Review and preparation never contact a provider.
"""
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from .models import canonical, local_path, text
from .profiles import ProviderProfile
from .providers import money, translation_authorization_type
from .state import atomic_json, now
from .translation import TranslationService, _approved_requests, _preview_fields, split_text, translation_approval
from .translation_source import source_context
from ..speech._io import digest
from ..state_backup import publish_state

KIND = 'translation-batch/1'
MAX_ITEMS = 32
MAX_CHARACTERS = 2_000_000
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_JOB_FIELDS = {'schema', 'kind', 'id', 'created', 'status', 'profile', 'title', 'source_text',
    'source_sha256', 'source_language', 'target_language', 'chunks', 'receipts',
    'authorization', 'per_call_cost', 'glossary'}
_CONTEXT = {'source_anchor', 'source_metadata', 'source_topic'}
_STATES = {'queued', 'running', 'paused', 'attention', 'complete'}


def _hash(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def _copy(value):
    return json.loads(canonical(value))


def _stop(cancelled):
    if cancelled():
        raise InterruptedError('Translation batch paused before the next operation.')


def _context(value):
    return source_context(value['text'], value.get('source_anchor'),
                          value.get('source_metadata'), value.get('source_topic')) or {}


def _inputs(items):
    if type(items) not in (list, tuple) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError(f'Choose between 1 and {MAX_ITEMS} translation documents.')
    if (any(type(item) is not dict or type(item.get('text')) is not str for item in items)
            or sum(len(item['text']) for item in items) > MAX_CHARACTERS):
        raise ValueError('Choose bounded explicit document text for this batch.')
    result, characters = [], 0
    required = {'profile', 'text', 'source', 'target', 'budget'}
    optional = {'title', 'glossary', 'rate_per_million'} | _CONTEXT
    for item in _copy(list(items)):
        if type(item) is not dict or not required <= set(item) or set(item) - required - optional:
            raise ValueError('Invalid translation batch item.')
        if type(item['profile']) is not dict:
            raise ValueError('Invalid batch provider profile.')
        try:
            profile = ProviderProfile(**item['profile'])
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid batch provider profile.') from exc
        if profile.adapter == 'libretranslate-local' and money(item['budget']) != 0:
            raise ValueError('Local translation requires a zero external cost bound.')
        value = text(item['text'], 'batch source text', maximum=MAX_CHARACTERS)
        characters += len(value)
        if characters > MAX_CHARACTERS:
            raise ValueError('The batch exceeds the two-million-character review limit.')
        title = text(item.get('title', 'Translation'), 'batch item title', maximum=512)
        rate = item.get('rate_per_million')
        result.append({'profile': profile.to_dict(), 'text': value, 'title': title,
            'source': item['source'], 'target': item['target'], 'budget': str(money(item['budget'])),
            'glossary': item.get('glossary'),
            'rate_per_million': None if rate in (None, '') else str(money(rate)), **_context(item)})
    return result


def _row(preview, profile, title, budget, rate, index):
    bound = str(money(budget))
    if preview['estimated_cost'] is not None and money(preview['estimated_cost']) > money(bound):
        raise ValueError('A document rate estimate exceeds its explicitly approved reservation.')
    return {**preview, 'profile': profile.to_dict(), 'title': title, 'budget': bound,
            'rate_per_million': rate, 'output_directory': f'{index + 1:04d}-translation',
            'selected_positions': len(preview['source_anchor']['positions']) if preview.get('source_anchor') else None}


def _review(title, rows):
    text(title, 'translation batch title', maximum=512)
    if not 1 <= len(rows) <= MAX_ITEMS or sum(row['characters'] for row in rows) > MAX_CHARACTERS:
        raise ValueError('Translation batch exceeds its document or character limit.')
    sources, workspaces = set(), set()
    for row in rows:
        key = _hash({name: row.get(name) for name in ('profile', 'source_sha256', 'source', 'target',
                    'glossary', 'source_anchor', 'source_metadata', 'source_topic')})
        if key in sources:
            raise ValueError('The same source/settings occur twice; export one accepted job more than once instead.')
        sources.add(key)
        if row.get('source_anchor') is not None:
            workspaces.add(row['source_anchor']['workspace'])
    if len(workspaces) > 1:
        raise ValueError('A source-bound batch stays in one explicitly selected library workspace.')
    estimates = [row['estimated_cost'] for row in rows]
    return {'schema': 'carr-translation-batch-review/1', 'title': title, 'items': rows,
        'documents': len(rows), 'characters': sum(row['characters'] for row in rows),
        'calls': sum(row['calls'] for row in rows),
        'budget': str(money(str(sum((money(row['budget']) for row in rows), Decimal(0))))),
        'estimated_cost': None if any(value is None for value in estimates) else
            str(sum((money(value) for value in estimates), Decimal(0))),
        'workspace': next(iter(workspaces), None), 'currency': 'USD',
        'preservation': 'separate plain-text derivatives; originals and quotation authority remain unchanged'}


def member_id(identity, review_hash, index):
    return sha256(f'{identity}:{review_hash}:{index}'.encode('ascii')).hexdigest()[:32]


def member_binding(job):
    """The child alone owns mutable status and receipts; all input fields bind."""
    return {key: value for key, value in job.items() if key not in ('updated', 'status', 'receipts')}


def _template(job):
    if (type(job) is not dict or not _JOB_FIELDS <= set(job) or set(job) - _JOB_FIELDS - _CONTEXT
            or job.get('schema') != 'carr-assistant-job/1' or job.get('kind') != 'translation/1'
            or type(job.get('id')) is not str or not _ID.fullmatch(job['id'])
            or job.get('status') != 'queued' or job.get('receipts') != []):
        raise ValueError('Invalid immutable translation child template.')
    text(job['created'], 'child creation time', maximum=128)
    text(job['title'], 'child title', maximum=512)
    parts = split_text(job['source_text'])
    if (job['chunks'] != list(parts)
            or job['source_sha256'] != sha256(job['source_text'].encode('utf-8')).hexdigest()):
        raise ValueError('The batch child source or chunks changed.')
    if type(job['profile']) is not dict or type(job['authorization']) is not dict:
        raise ValueError('Invalid approved batch provider or authorization.')
    try:
        profile = ProviderProfile(**job['profile'])
        approval = translation_authorization_type(profile)(**job['authorization'])
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid approved batch provider or authorization.') from exc
    if approval.currency != 'USD':
        raise ValueError('The reviewed batch reservation currency changed.')
    context = source_context(job['source_text'], job.get('source_anchor'),
                             job.get('source_metadata'), job.get('source_topic'))
    if job['source_language'] == job['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    _approved_requests(profile, parts, job['source_language'], job['target_language'],
        approval, job['per_call_cost'], job['glossary'], **(context or {}))
    return profile, approval, parts, context


def validate_batch(value, identity=None):
    """Pure bounded manifest validation, usable by passive typed backups."""
    fields = {'schema', 'kind', 'id', 'created', 'updated', 'title', 'review',
              'review_sha256', 'members', 'manifest_sha256'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr-assistant-job/1' or value['kind'] != KIND
            or type(value['id']) is not str or not _ID.fullmatch(value['id'])
            or identity is not None and value['id'] != identity
            or type(value['review_sha256']) is not str or not _HASH.fullmatch(value['review_sha256'])
            or type(value['members']) is not list or not 1 <= len(value['members']) <= MAX_ITEMS):
        raise ValueError('Invalid translation batch manifest.')
    for key in ('created', 'updated'):
        text(value[key], 'batch timestamp', maximum=128)
    frozen = {key: item for key, item in value.items() if key not in ('updated', 'manifest_sha256')}
    if value['manifest_sha256'] != _hash(frozen) or value['review_sha256'] != _hash(value['review']):
        raise ValueError('The reviewed translation batch manifest changed.')
    rows, characters, approvals = [], 0, set()
    for index, member in enumerate(value['members']):
        if type(member) is not dict or set(member) != {'job', 'rate_per_million', 'output_directory'}:
            raise ValueError('Invalid translation batch member.')
        job = member['job']
        profile, approval, parts, context = _template(job)
        if approval.id in approvals or job['id'] == value['id']:
            raise ValueError('Every batch document needs its own existing approval and child identity.')
        approvals.add(approval.id)
        characters += len(job['source_text'])
        if characters > MAX_CHARACTERS:
            raise ValueError('Translation batch exceeds its retained source limit.')
        if job['id'] != member_id(value['id'], value['review_sha256'], index):
            raise ValueError('A translation child no longer matches its reviewed batch position.')
        rate = member['rate_per_million']
        if rate is not None and (type(rate) is not str or rate != str(money(rate))):
            raise ValueError('Invalid reviewed translation rate.')
        preview = _preview_fields(profile, job['source_text'], parts,
            job['source_language'], job['target_language'], rate_per_million=rate,
            glossary=job['glossary'], context=context)
        row = _row(preview, profile, job['title'], approval.budget, rate, index)
        if member['output_directory'] != row['output_directory']:
            raise ValueError('A reviewed batch output name changed.')
        rows.append(row)
    if canonical(value['review']) != canonical(_review(value['title'], rows)):
        raise ValueError('The batch settings differ from their exact document approvals.')
    return value


def validate_member(manifest, index, job):
    template = manifest['members'][index]['job']
    if (type(job) is not dict or member_binding(job) != member_binding(template)
            or job.get('status') not in _STATES or type(job.get('receipts')) is not list
            or len(job['receipts']) > len(template['chunks'])
            or job['status'] == 'complete' and len(job['receipts']) != len(template['chunks'])):
        raise ValueError('A saved translation child differs from its reviewed batch template.')
    return job


def batch_summary(manifest, children):
    """Summarize supplied existing child checkpoints; no new execution state."""
    rows = []
    for index, child in enumerate(children):
        member = manifest['members'][index]
        if child is not None:
            validate_member(manifest, index, child)
        rows.append({'id': member['job']['id'], 'title': member['job']['title'],
            'status': 'preparing' if child is None else child['status'],
            'completed': 0 if child is None else len(child['receipts']),
            'total': len(member['job']['chunks']), 'output_directory': member['output_directory']})
    if len(rows) != len(manifest['members']):
        raise ValueError('Translation batch member inventory is incomplete.')
    states = {row['status'] for row in rows}
    state = ('attention' if 'attention' in states else 'preparing' if 'preparing' in states else
             'complete' if states == {'complete'} else 'paused' if 'paused' in states else
             'partial' if 'complete' in states else 'running' if 'running' in states else 'queued')
    return {'id': manifest['id'], 'title': manifest['title'], 'review_sha256': manifest['review_sha256'],
        'status': state, 'completed': sum(row['status'] == 'complete' for row in rows), 'total': len(rows),
        'completed_chunks': sum(row['completed'] for row in rows),
        'total_chunks': sum(row['total'] for row in rows), 'items': rows}


class TranslationBatchService:
    def __init__(self, service):
        if not isinstance(service, TranslationService):
            raise ValueError('Use the existing translation service and its provider gateway.')
        self.service, self.jobs = service, service.jobs

    def preview(self, items, *, title='Translation batch', cancelled=lambda: False):
        rows = []
        for index, item in enumerate(_inputs(items)):
            _stop(cancelled)
            profile = ProviderProfile(**item['profile'])
            preview = self.service.preview(profile, item['text'], item['source'], item['target'],
                glossary=item['glossary'], rate_per_million=item['rate_per_million'],
                cancelled=cancelled, **_context(item))
            rows.append(_row(preview, profile, item['title'], item['budget'], item['rate_per_million'], index))
        _stop(cancelled)
        review = _review(title, rows)
        return {**review, 'review_sha256': _hash(review)}

    def submit(self, items, *, title='Translation batch', review_sha256, approve_remote=False,
               approve_local=False, cancelled=lambda: False):
        frozen = _inputs(items)
        kinds = {item['profile']['adapter'] == 'libretranslate-local' for item in frozen}
        if False in kinds and approve_remote is not True:
            raise PermissionError('Explicitly review and approve every remote document before preparing a remote batch.')
        if True in kinds and approve_local is not True:
            raise PermissionError('Explicitly review and approve every local document before preparing a local batch.')
        reviewed = self.preview(frozen, title=title, cancelled=cancelled)
        if review_sha256 != reviewed['review_sha256']:
            raise PermissionError('The exact batch text, order, source, settings or reservations changed. Review again.')
        identity, members = uuid4().hex, []
        for index, item in enumerate(frozen):
            _stop(cancelled)
            profile = ProviderProfile(**item['profile'])
            approval, cost = translation_approval(profile, item['text'], item['source'], item['target'],
                item['budget'], glossary=item['glossary'], **_context(item))
            job = self.service._prepared_job(profile, item['text'], item['source'], item['target'],
                title=item['title'], authorization=approval, per_call_cost=cost, glossary=item['glossary'],
                cancelled=cancelled, **_context(item))
            job['id'] = member_id(identity, review_sha256, index)
            members.append({'job': _copy(job), 'rate_per_million': item['rate_per_million'],
                            'output_directory': reviewed['items'][index]['output_directory']})
        for member in members:
            self.service._validate_source(member['job'], cancelled=cancelled)
        _stop(cancelled)
        stamp = now()
        manifest = {'schema': 'carr-assistant-job/1', 'kind': KIND, 'id': identity,
            'created': stamp, 'updated': stamp, 'title': title,
            'review': {key: value for key, value in reviewed.items() if key != 'review_sha256'},
            'review_sha256': review_sha256, 'members': members}
        manifest['manifest_sha256'] = _hash({key: value for key, value in manifest.items() if key != 'updated'})
        validate_batch(manifest, identity)
        with self.jobs.lock(identity):
            self.jobs.save(manifest)
            try:
                self._prepare(manifest, cancelled=cancelled)
            except InterruptedError:
                # The immutable plan already exists. Later explicit preparation
                # uses its exact child IDs and approvals; it cannot bill now.
                pass
        return identity

    def _load(self, identity):
        return validate_batch(self.jobs.load(identity), identity)

    def _children(self, manifest):
        children = []
        for index, member in enumerate(manifest['members']):
            identity = member['job']['id']
            if not self.jobs.path(identity).exists():
                children.append(None)
                continue
            job = self.service._load(identity)
            children.append(validate_member(manifest, index, job))
        return children

    def _prepare(self, manifest, *, cancelled):
        for index, member in enumerate(manifest['members']):
            _stop(cancelled)
            identity = member['job']['id']
            with self.jobs.lock(identity):
                if self.jobs.path(identity).exists():
                    validate_member(manifest, index, self.service._load(identity))
                else:
                    self.service._validate_source(member['job'], cancelled=cancelled)
                    _stop(cancelled)
                    self.jobs.save(_copy(member['job']))

    def get_review(self, identity):
        """Read the exact retained approval, without renewing it or sending text."""
        manifest = self._load(identity)
        return {**_copy(manifest['review']), 'review_sha256': manifest['review_sha256']}

    def get(self, identity):
        manifest = self._load(identity)
        return batch_summary(manifest, self._children(manifest))

    def prepare(self, identity, *, cancelled=lambda: False):
        with self.jobs.lock(identity):
            manifest = self._load(identity)
            try:
                self._prepare(manifest, cancelled=cancelled)
            except InterruptedError:
                return {**batch_summary(manifest, self._children(manifest)), 'status': 'paused'}
            return batch_summary(manifest, self._children(manifest))

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None):
        with self.jobs.lock(identity):
            manifest = self._load(identity)
            try:
                self._prepare(manifest, cancelled=cancelled)
                children = self._children(manifest)
                for child in children:
                    _stop(cancelled)
                    self.service._validate_source(child, cancelled=cancelled)
                for index, child in enumerate(children):
                    _stop(cancelled)
                    if child['status'] == 'complete':
                        continue
                    before = sum(len(value['receipts']) for value in children[:index])
                    after = sum(len(value['receipts']) for value in children[index + 1:])
                    total = sum(len(value['chunks']) for value in children)

                    def report(done, _total, phase, before=before, after=after, total=total, index=index):
                        try:
                            progress(before + done + after, total,
                                f'Document {index + 1}/{len(children)}: {phase}')
                        except Exception:
                            pass

                    result = self.service.resume(child['id'], cancelled=cancelled, progress=report,
                        before_resume=lambda stored, index=index: validate_member(manifest, index, stored))
                    children[index] = validate_member(manifest, index, self.service._load(child['id']))
                    if result['status'] != 'complete':
                        return batch_summary(manifest, children)
                result = batch_summary(manifest, self._children(manifest))
                try:
                    progress(result['completed_chunks'], result['total_chunks'], 'Verified translation documents')
                except Exception:
                    pass
                return result
            except InterruptedError:
                return {**batch_summary(manifest, self._children(manifest)), 'status': 'paused'}

    def export(self, identity, output, *, cancelled=lambda: False):
        with self.jobs.lock(identity):
            manifest = self._load(identity)
            children = self._children(manifest)
            summary = batch_summary(manifest, children)
            if summary['status'] != 'complete':
                raise ValueError('Finish and verify every document before publishing the batch.')
            target = local_path(output, must_exist=False)
            local_path(target.parent)
            if target.exists():
                raise FileExistsError('Choose a new batch export directory.')
            for child in children:
                _stop(cancelled)
                self.service._validate_source(child, cancelled=cancelled)
            with TemporaryDirectory(dir=target.parent, prefix='.pending-translation-batch-') as staged:
                stage, outputs = Path(staged), []
                for member, child in zip(manifest['members'], children):
                    _stop(cancelled)
                    self.service._validate_source(child, cancelled=cancelled)
                    directory = stage / member['output_directory']
                    directory.mkdir(mode=0o700)
                    self.service._export_into(child, directory)
                    outputs.append({'job_id': child['id'], 'title': child['title'],
                        'directory': member['output_directory'],
                        'provenance_sha256': digest(directory / 'translation-provenance.json')})
                report = {'schema': 'carr-translation-batch-export/1', 'batch_id': identity,
                    'review_sha256': manifest['review_sha256'], 'title': manifest['title'],
                    'quotation_authority': False, 'source_is_immutable': True, 'items': outputs}
                atomic_json(stage / 'batch-provenance.json', report)
                for child in children:
                    _stop(cancelled)
                    self.service._validate_source(child, cancelled=cancelled)
                _stop(cancelled)
                publish_state(stage, target)
            return {'id': identity, 'status': 'complete', 'path': str(target), 'items': outputs}

    def list(self):
        values = []
        for identity in self.jobs.identities():
            job = self.jobs.load(identity)
            if job.get('kind') == 'translation/1':
                continue
            if job.get('kind') != KIND:
                raise ValueError('Unknown translation job kind.')
            values.append(self.get(identity))
        return tuple(values)
