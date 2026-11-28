"""Bounded document batches over the existing translation jobs and provider ledger.

The batch manifest contains immutable child templates, not a second execution
queue. Every request, accepted chunk and uncertain attempt belongs to the unchanged
translation/1 or source-bound translation/2 child. Review and preparation never contact a provider.
"""
from decimal import Decimal
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from .models import canonical, local_path, text
from .profiles import ProviderProfile
from .providers import money, translation_authorization_type
from .state import JobStore, now
from .translation import (TranslationService, _approved_requests, _preview_fields, _source_job_id,
                          split_text, translation_approval)
from .translation_source import source_context, validate_source
from ..speech.reading import ReadingDocument
from ..durable_state import read_json, MAX_JSON_BYTES
from ..ingest.writer_guard import exclusive_writer
from contextlib import nullcontext
from ..research.export_file import capture_export_destination, write_binary_export

KIND = 'translation-batch/1'
MAX_ITEMS = 32
MAX_CHARACTERS = 2_000_000
MAX_BATCHES = 256
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
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
        value = text(item['text'], 'batch source text', maximum=MAX_CHARACTERS)
        characters += len(value)
        if characters > MAX_CHARACTERS:
            raise ValueError('The batch exceeds the two-million-character review limit.')
        title = text(item.get('title', 'Translation'), 'batch item title', maximum=512)
        rate = item.get('rate_per_million')
        if profile.local and money(item['budget']) != 0:
            raise ValueError('Local translation requires an explicit zero external cost bound.')
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
    return UUID(hex=sha256(f'{identity}:{review_hash}:{index}'.encode('ascii')).hexdigest()[:32], version=4).hex


def member_binding(job):
    """The child alone owns mutable status and receipts; all input fields bind."""
    return {key: value for key, value in job.items() if key not in ('updated', 'status', 'receipts')}


def _template(job, metadata):
    if (type(job) is not dict or not _JOB_FIELDS <= set(job)
            or set(job) - _JOB_FIELDS - {'source_binding'}
            or job.get('schema') != 'carr-assistant-job/1'
            or job.get('kind') not in ('translation/1', 'translation/2')
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
    context = None
    if job['kind'] == 'translation/1':
        if 'source_binding' in job or UUID(job['id']).version != 4 or metadata is not None:
            raise ValueError('A source-bound translation cannot use the pasted-text job format.')
    else:
        binding = job.get('source_binding')
        if (type(binding) is not dict
                or set(binding) != {'schema', 'library_root', 'root_identity', 'topic', 'anchor'}
                or binding['schema'] != 'carr-translation-source/1'
                or type(binding['library_root']) is not str
                or not 1 <= len(binding['library_root']) <= 32768
                or chr(0) in binding['library_root']
                or binding['library_root'].startswith(('//', chr(92) * 2))
                or not any(path.is_absolute() for path in (
                    PurePosixPath(binding['library_root']), PureWindowsPath(binding['library_root'])))
                or any('..' in path.parts for path in (
                    PurePosixPath(binding['library_root']), PureWindowsPath(binding['library_root'])))
                or type(binding['root_identity']) is not list or len(binding['root_identity']) != 2
                or any(type(part) is not int or part < 0 for part in binding['root_identity'])
                or job['id'] != _source_job_id(job)):
            raise ValueError('Invalid retained translation source identity.')
        context = source_context(job['source_text'], binding['anchor'], metadata, binding['topic'])
        positions = context['source_anchor']['positions']
        if [row['ordinal'] for row in positions] != list(
                range(positions[0]['ordinal'], positions[-1]['ordinal'] + 1)):
            raise ValueError('The translation source range is not contiguous.')
    if job['source_language'] == job['target_language']:
        raise ValueError('Translation needs different source and target languages.')
    _approved_requests(profile, parts, job['source_language'], job['target_language'],
        approval, job['per_call_cost'], glossary=job['glossary'],
        data_class='selected-script' if context else 'selected-excerpt')
    TranslationService._admission_budget(job)
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
        if type(member) is not dict or set(member) != {'job', 'rate_per_million', 'output_directory', 'source_metadata'}:
            raise ValueError('Invalid translation batch member.')
        job = member['job']
        profile, approval, parts, context = _template(job, member['source_metadata'])
        if approval.id in approvals or job['id'] == value['id']:
            raise ValueError('Every batch document needs its own existing approval and child identity.')
        approvals.add(approval.id)
        characters += len(job['source_text'])
        if characters > MAX_CHARACTERS:
            raise ValueError('Translation batch exceeds its retained source limit.')
        if (job['kind'] == 'translation/1'
                and job['id'] != member_id(value['id'], value['review_sha256'], index)):
            raise ValueError('A translation child no longer matches its reviewed batch position.')
        rate = member['rate_per_million']
        if rate is not None and (type(rate) is not str or rate != str(money(rate))):
            raise ValueError('Invalid reviewed translation rate.')
        preview = _preview_fields(profile, job['source_text'], parts,
            job['source_language'], job['target_language'], rate_per_million=rate,
            glossary=job['glossary'])
        preview.update(context or {})
        row = _row(preview, profile, job['title'], approval.budget, rate, index)
        if member['output_directory'] != row['output_directory']:
            raise ValueError('A reviewed batch output name changed.')
        rows.append(row)
    if canonical(value['review']) != canonical(_review(value['title'], rows)):
        raise ValueError('The batch settings differ from their exact document approvals.')
    if len((canonical(value) + chr(10)).encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('The immutable translation batch exceeds the checkpoint byte budget.')
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



def _present(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    return True


class TranslationBatchService:
    """Reviewed grouping only; execution and accepted receipts stay in children."""
    def __init__(self, service):
        if not isinstance(service, TranslationService):
            raise ValueError('Use the existing translation service and its provider gateway.')
        self.service = service
        self.jobs = JobStore(service.directory / 'batches')

    def _source(self, member, job, *, remote=False, cancelled=lambda: False, guard=lambda: None):
        desk = self.service._source_context(job, remote=remote, cancelled=cancelled, guard=guard)
        if desk is not None:
            binding = job['source_binding']
            validate_source(self.service.library, job['source_text'], binding['anchor'],
                member['source_metadata'], binding['topic'], cancelled=cancelled, guard=guard)
        return desk

    def preview(self, items, *, title='Translation batch', cancelled=lambda: False, guard=lambda: None):
        rows = []
        for index, item in enumerate(_inputs(items)):
            _stop(cancelled)
            guard()
            profile = ProviderProfile(**item['profile'])
            context = _context(item)
            if context:
                validate_source(self.service.library, item['text'], context['source_anchor'],
                    context['source_metadata'], context['source_topic'], cancelled=cancelled, guard=guard)
            preview = self.service.preview(profile, item['text'], item['source'], item['target'],
                glossary=item['glossary'], rate_per_million=item['rate_per_million'])
            preview.update(context)
            rows.append(_row(preview, profile, item['title'], item['budget'], item['rate_per_million'], index))
        _stop(cancelled)
        guard()
        review = _review(title, rows)
        return {**review, 'review_sha256': _hash(review)}

    def submit(self, items, *, title='Translation batch', review_sha256, approve_remote=False,
               approve_local=False, cancelled=lambda: False, guard=lambda: None):
        frozen = _inputs(items)
        if any(not item['profile']['local'] for item in frozen) and approve_remote is not True:
            raise PermissionError('Explicitly review and approve every remote document before preparing this batch.')
        if any(item['profile']['local'] for item in frozen) and approve_local is not True:
            raise PermissionError('Explicitly review and approve every local document before preparing this batch.')
        reviewed = self.preview(frozen, title=title, cancelled=cancelled, guard=guard)
        if review_sha256 != reviewed['review_sha256']:
            raise PermissionError('The exact batch text, order, source, settings or reservations changed. Review again.')
        if len(self._identities(cancelled=cancelled)) >= MAX_BATCHES:
            raise ValueError('The translation batch inventory has reached its 256-record limit.')
        identity, members = uuid4().hex, []
        for index, item in enumerate(frozen):
            _stop(cancelled)
            guard()
            profile = ProviderProfile(**item['profile'])
            context = _context(item)
            approval, cost = translation_approval(profile, item['text'], item['source'], item['target'],
                item['budget'], glossary=item['glossary'], source_bound=bool(context))
            document = (ReadingDocument(item['text'], title=item['title'],
                        source_anchor=context['source_anchor']) if context else None)
            job = self.service._prepared_job(profile, item['text'], item['source'], item['target'],
                title=item['title'], authorization=approval, per_call_cost=cost, glossary=item['glossary'],
                source_document=document, topic=context.get('source_topic'),
                cancelled=cancelled, guard=guard)
            if job['kind'] == 'translation/1':
                job['id'] = member_id(identity, review_sha256, index)
            members.append({'job': _copy(job), 'rate_per_million': item['rate_per_million'],
                            'source_metadata': context.get('source_metadata'),
                            'output_directory': reviewed['items'][index]['output_directory']})
        for member in members:
            self._source(member, member['job'], cancelled=cancelled, guard=guard)
        _stop(cancelled)
        guard()
        stamp = now()
        manifest = {'schema': 'carr-assistant-job/1', 'kind': KIND, 'id': identity,
            'created': stamp, 'updated': stamp, 'title': title,
            'review': {key: value for key, value in reviewed.items() if key != 'review_sha256'},
            'review_sha256': review_sha256, 'members': members}
        manifest['manifest_sha256'] = _hash({key: value for key, value in manifest.items() if key != 'updated'})
        validate_batch(manifest, identity)
        from ..durable_state import atomic_json
        with self.jobs.lock(identity):
            # A batch is immutable. Ordinary child JobStore files own all work.
            atomic_json(self.jobs.path(identity), manifest)
            self.jobs.control(identity, 'pause')
            try:
                self._prepare(manifest, cancelled=cancelled, guard=guard)
            except InterruptedError:
                # Preparation sent no text. Retained templates identify every
                # missing child for later explicit preparation, without rebilling.
                pass
        return identity

    def _load(self, identity):
        return validate_batch(read_json(self.jobs.path(identity), maximum=MAX_JSON_BYTES), identity)

    def _command(self, identity):
        path = self.jobs.path(identity).with_suffix('.control.json')
        if not _present(path):
            return 'pause'
        value = read_json(path, maximum=1024)
        if type(value) is not dict or set(value) != {'command'} or value['command'] not in ('run', 'pause', 'cancel'):
            raise ValueError('Invalid translation batch control.')
        return value['command']

    def pause(self, identity):
        self._load(identity)
        self.jobs.control(identity, 'pause')

    def _children(self, manifest):
        children = []
        for index, member in enumerate(manifest['members']):
            identity = member['job']['id']
            if not _present(self.service.jobs.path(identity)):
                children.append(None)
                continue
            job = self.service._load(identity)
            children.append(validate_member(manifest, index, job))
        return children

    def _prepare(self, manifest, *, cancelled, guard):
        for index, member in enumerate(manifest['members']):
            _stop(cancelled)
            guard()
            identity = member['job']['id']
            with self.service.jobs.lock(identity):
                if _present(self.service.jobs.path(identity)):
                    validate_member(manifest, index, self.service._load(identity))
                else:
                    job = member['job']
                    desk = self._source(member, job, cancelled=cancelled, guard=guard)
                    with exclusive_writer(self.service.library.root) if desk is not None else nullcontext():
                        self.service._source_current(desk, cancelled=cancelled, guard=guard)
                        _stop(cancelled)
                        self.service.jobs.save(_copy(job))

    def get_review(self, identity):
        """Read the retained approval without renewing it or sending any text."""
        manifest = self._load(identity)
        return {**_copy(manifest['review']), 'review_sha256': manifest['review_sha256']}

    def get(self, identity):
        manifest = self._load(identity)
        result = batch_summary(manifest, self._children(manifest))
        return {**result, 'control': self._command(identity)}

    def prepare(self, identity, *, cancelled=lambda: False, guard=lambda: None):
        with self.jobs.lock(identity):
            manifest = self._load(identity)
            try:
                self._prepare(manifest, cancelled=cancelled, guard=guard)
            except InterruptedError:
                return {**batch_summary(manifest, self._children(manifest)), 'status': 'paused'}
            return batch_summary(manifest, self._children(manifest))

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None,
               guard=lambda: None):
        # This method is an explicit user action. No constructor, get, list,
        # restore or prepare path calls it or changes a saved pause into run.
        completed = None
        try:
            with self.jobs.lock(identity):
                manifest = self._load(identity)
                initial = batch_summary(manifest, self._children(manifest))
                if initial['status'] == 'complete':
                    completed = initial
                    return initial
                _stop(cancelled)
                guard()
                self.jobs.control(identity, 'run')
                def stopped():
                    return cancelled() or self._command(identity) != 'run'
                try:
                    self._prepare(manifest, cancelled=stopped, guard=guard)
                    children = self._children(manifest)
                    # Revalidate the last source before sending the first one.
                    for member, child in zip(manifest['members'], children):
                        _stop(stopped)
                        self._source(member, child, remote=child['status'] != 'complete',
                                     cancelled=stopped, guard=guard)
                    for index, child in enumerate(children):
                        if child['status'] == 'complete':
                            continue
                        _stop(stopped)
                        before = sum(len(value['receipts']) for value in children[:index])
                        after = sum(len(value['receipts']) for value in children[index + 1:])
                        total = sum(len(value['chunks']) for value in children)

                        def report(done, _total, phase, before=before, after=after, total=total, index=index):
                            try:
                                progress(before + done + after, total,
                                    f'Document {index + 1}/{len(children)}: {phase}')
                            except (Exception, KeyboardInterrupt):
                                # The accepted child's receipt is already saved.
                                # A display observer cannot revoke that evidence.
                                pass

                        result = self.service.resume(child['id'], cancelled=stopped, progress=report,
                            guard=guard,
                            before_resume=lambda stored, index=index: validate_member(manifest, index, stored))
                        children[index] = validate_member(manifest, index, self.service._load(child['id']))
                        if result['status'] != 'complete':
                            return batch_summary(manifest, children)
                    completed = batch_summary(manifest, self._children(manifest))
                    try:
                        progress(completed['completed_chunks'], completed['total_chunks'],
                                 'Verified translation documents')
                    except (Exception, KeyboardInterrupt):
                        pass
                except InterruptedError:
                    snapshot = batch_summary(manifest, self._children(manifest))
                    if snapshot['status'] == 'complete':
                        completed = snapshot
                    else:
                        self.jobs.control(identity, 'pause')
                        return {**snapshot, 'status': 'paused'}
        except (Exception, KeyboardInterrupt):
            if completed is None or completed['status'] != 'complete':
                raise
            # Only independently verified accepted children establish success;
            # an owner-release problem remains a separate visible notice.
            checked = batch_summary(self._load(identity), self._children(self._load(identity)))
            if checked['status'] != 'complete':
                raise
            completed = {**checked, 'cleanup_warning':
                'All child receipts are verified; closing the batch owner needs attention.'}
        return completed

    def export(self, identity, output, *, cancelled=lambda: False, guard=lambda: None, approval=None):
        """Publish one ZIP using the existing exact-byte export receipt writer.

        Child exports are unchanged. All temporary directory cleanup occurs
        before the ZIP publication, so it cannot revoke a saved package.
        """
        from ..backup_sidecars import read_regular
        completed = []
        try:
            with self.jobs.lock(identity):
                self._export_locked(identity, output, cancelled=cancelled, guard=guard,
                                    approval=approval, completed=completed)
        except (Exception, KeyboardInterrupt):
            if not completed:
                raise
            completed[0]['cleanup_warning'] = (
                (completed[0].get('cleanup_warning', '') + ' ').lstrip()
                + 'The package was saved; closing the batch owner needs attention.')
        return completed[0]

    def _export_locked(self, identity, output, *, cancelled, guard, approval, completed):
        from ..backup_sidecars import read_regular
        manifest = self._load(identity)
        children = self._children(manifest)
        if batch_summary(manifest, children)['status'] != 'complete':
            raise ValueError('Finish and verify every document before publishing the batch.')
        if sum(len(row['text']) for child in children for row in child['receipts']) > MAX_CHARACTERS:
            raise ValueError('The accepted batch translation exceeds the two-million-character package limit; '
                             'child receipts remain available for individual export.')
        target = Path(output).absolute()
        if target.suffix.lower() != '.zip':
            raise ValueError('Choose a new .zip plain-text translation package.')
        private = self.service.library.root if self.service.library is not None else self.service.directory
        approval = approval or capture_export_destination(target, private_root=private, cancelled=cancelled)
        if not approval.require_absent:
            raise FileExistsError('Choose a new translation package; replacement is not authorized.')
        expected = capture_export_destination(target, private_root=private, cancelled=cancelled)
        if approval != expected:
            raise ValueError('The export destination changed since review.')
        outputs, size = [], 0
        stream = io.BytesIO()
        with TemporaryDirectory(dir=target.parent, prefix='.pending-translation-batch-') as staged:
            stage = Path(staged)
            with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
                for member, child in zip(manifest['members'], children):
                    _stop(cancelled)
                    self._source(member, child, cancelled=cancelled, guard=guard)
                    directory = stage / member['output_directory']
                    self.service.export(child['id'], directory, cancelled=cancelled, guard=guard)
                    hashes = {}
                    for name in ('translation.reading.json', 'translated.txt', 'original.txt',
                                 'bilingual.txt', 'translation-provenance.json'):
                        _stop(cancelled)
                        guard()
                        raw = read_regular(directory / name, MAX_JSON_BYTES, empty=name.endswith('.txt'))
                        size += len(raw)
                        if size > MAX_PACKAGE_BYTES:
                            raise ValueError('The plain-text translation package exceeds its 64 MiB export budget.')
                        hashes[name] = sha256(raw).hexdigest()
                        archive.writestr(member['output_directory'] + '/' + name, raw)
                    outputs.append({'job_id': child['id'], 'title': child['title'],
                        'directory': member['output_directory'],
                        'provenance_sha256': hashes['translation-provenance.json'], 'files': hashes})
                report = {'schema': 'carr-translation-batch-export/1', 'batch_id': identity,
                    'review_sha256': manifest['review_sha256'], 'title': manifest['title'],
                    'quotation_authority': False, 'source_is_immutable': True, 'items': outputs,
                    'format': 'ZIP of plain-text and reading derivatives; document layout is not retained'}
                archive.writestr('batch-provenance.json', (canonical(report) + '\n').encode('utf-8'))
            payload = stream.getvalue()
            if len(payload) > MAX_PACKAGE_BYTES:
                raise ValueError('The translation ZIP exceeds its 64 MiB export budget.')
        # Full source validation and owner cleanup precede the one commit.
        desks = [self._source(member, child, cancelled=cancelled, guard=guard)
                 for member, child in zip(manifest['members'], children)]
        def stopped():
            if cancelled():
                return True
            for desk in desks:
                self.service._source_current(desk, cancelled=cancelled, guard=guard)
            return False
        result = {'id': identity, 'status': 'complete', 'path': str(target), 'items': outputs,
                  'sha256': sha256(payload).hexdigest()}
        receipt = write_binary_export(target, payload, private_root=private, cancelled=stopped,
                                      require_absent=True, approval=approval)
        if receipt.cleanup_warning:
            result['cleanup_warning'] = receipt.cleanup_warning
        completed.append(result)

    def _identities(self, *, cancelled=lambda: False):
        identities, seen = [], 0
        with os.scandir(self.jobs.directory) as entries:
            for entry in entries:
                _stop(cancelled)
                seen += 1
                if seen > MAX_BATCHES * 4:
                    raise ValueError('Translation batch inventory exceeds its bounded entry budget.')
                name = entry.name
                if re.fullmatch(r'[0-9a-f]{32}\.json', name):
                    identities.append(name[:-5])
                    if len(identities) > MAX_BATCHES:
                        raise ValueError('Translation batch inventory exceeds its record budget.')
                elif not (re.fullmatch(r'[0-9a-f]{32}\.(?:control\.json|lock)', name)
                          or name.startswith('.pending-')
                          or re.fullmatch(r'\.[0-9a-f]{32}(?:\.control)?\.json\.[0-9a-f]{32}\.tmp', name)):
                    raise ValueError('Unrecognized entry in the typed translation batch inventory.')
        return tuple(sorted(identities))

    def list(self, *, cancelled=lambda: False):
        values = []
        for identity in self._identities(cancelled=cancelled):
            _stop(cancelled)
            values.append(self.get(identity))
        return tuple(values)
