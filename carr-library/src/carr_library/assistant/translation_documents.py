"""Explicit retained-document batches on the existing translation job store.

There is no batch daemon, extra database or second execution queue. Preparation
saves ordinary translation jobs; the host runs/resumes selected IDs serially.
Only ordered plain-text paragraphs are preserved, not a source file's layout.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
from html import escape
from pathlib import Path
from uuid import uuid4
import json
import re

from .models import canonical, local_path, text
from .profiles import ProfileStore
from .providers import money
from ..reader.reading_desk import ReadingDesk
from ..speech.source import read_source_range
from ..speech.reading import ReadingDocument
from ..speech._io import check_cancelled

MAX_DOCUMENTS = 64
MAX_CHARACTERS = 2_000_000
MAX_PARAGRAPHS = 2048


@dataclass(frozen=True)
class SelectedDocument:
    document: ReadingDocument
    topic: str | None = None


class TranslationBatchPreparedError(RuntimeError):
    def __init__(self, jobs):
        self.receipt = {'status': 'partially-prepared', 'jobs': list(jobs),
                        'provider_calls_started': False}
        super().__init__('Some translation jobs were saved before preparation stopped. '
                         'No provider request was started. Inspect these saved job IDs before preparing again.')


def paragraph_ranges(value):
    text(value, 'structured translation text', maximum=MAX_CHARACTERS)
    # Keep exact source bytes/line separators in their original ranges. Split
    # only explicit blank lines; never guess sentence or language boundaries.
    result, start = [], 0
    for match in re.finditer(r'\r?\n[ \t]*\r?\n|\r[ \t]*\r', value):
        end = match.end()
        if not value[start:end].strip():
            continue
        result.append((start, end))
        if len(result) > MAX_PARAGRAPHS:
            raise ValueError('Choose fewer paragraphs for one structured translation.')
        start = end
    if start < len(value):
        if value[start:].strip() or not result:
            result.append((start, len(value)))
        else:
            result[-1] = (result[-1][0], len(value))
    if not result or len(result) > MAX_PARAGRAPHS:
        raise ValueError('Choose a nonempty bounded document selection.')
    return tuple(result)


def structured_payload(job):
    """Derive ordered paragraph mapping only from authenticated job chunks."""
    from .translation import split_text, _assemble_translation
    if job.get('preserve_paragraphs') is not True:
        raise ValueError('This job did not preserve paragraph boundaries.')
    rows, index = [], 0
    for number, (first, last) in enumerate(paragraph_ranges(job['source_text']), 1):
        original = job['source_text'][first:last]
        chunks = split_text(original)
        receipts = job['receipts'][index:index + len(chunks)]
        if len(receipts) != len(chunks):
            raise ValueError('Finish every paragraph before structured export.')
        translated = _assemble_translation(chunks, receipts)
        positions = []
        if job.get('source_binding'):
            positions = [row['ordinal'] for row in job['source_binding']['anchor']['positions']
                         if row['start'] < last and row['end'] > first]
        rows.append({'paragraph': number, 'source_start': first, 'source_end': last,
            'source_sha256': sha256(original.encode('utf-8')).hexdigest(),
            'source_positions': positions, 'original': original, 'translation': translated,
            'receipt_indices': list(range(index, index + len(chunks)))})
        index += len(chunks)
    if index != len(job['chunks']) or index != len(job['receipts']):
        raise ValueError('The structured translation inventory is incomplete.')
    report = {'schema': 'carr-translation-structure/1', 'job_id': job['id'],
        'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
        'target_language': job['target_language'], 'paragraphs': rows,
        'preservation': 'ordered plain-text paragraphs; no document layout, table or image fidelity',
        'quotation_authority': False}
    original_language = 'und' if job['source_language'] == 'auto' else job['source_language']
    blocks = [
        '<!doctype html><html lang="' + escape(job['target_language'], quote=True) + '">',
        '<head><meta charset="utf-8"><title>' + escape(job['title']) + '</title></head><body>',
        '<h1>' + escape(job['title']) + '</h1>',
        '<p>Machine translation derivative. Paragraph order is retained; original document layout is not.</p>',
    ]
    for row in rows:
        blocks.extend([
            '<section><h2>Paragraph ' + str(row['paragraph']) + '</h2>',
            '<h3>Original</h3><pre lang="' + escape(original_language, quote=True) + '">'
                + escape(row['original']) + '</pre>',
            '<h3>Translation</h3><pre lang="' + escape(job['target_language'], quote=True) + '">'
                + escape(row['translation']) + '</pre></section>',
        ])
    blocks.append('</body></html>')
    return report, '\n'.join(blocks) + '\n'


def capture_document(library, workspace, edition, *, first=1, last=None, topic=None,
                     cancelled=lambda: False, guard=lambda: None, max_characters=MAX_CHARACTERS):
    guard()
    check_cancelled(cancelled)
    if type(max_characters) is not int or not 1 <= max_characters <= MAX_CHARACTERS:
        raise ValueError('Choose a positive remaining document-batch character budget.')
    desk = ReadingDesk(library, workspace, edition, topic=topic, cancelled=cancelled)
    last = len(desk.pages) if last is None else last
    selected = read_source_range(desk, first, last, guard=guard, cancelled=cancelled,
                                 max_characters=max_characters)
    guard()
    check_cancelled(cancelled)
    return SelectedDocument(selected.reading_document(), topic)


def _selections(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_DOCUMENTS:
        raise ValueError('Explicitly select between 1 and 64 retained documents or ranges.')
    total, seen = 0, set()
    for value in values:
        if (not isinstance(value, SelectedDocument)
                or not isinstance(value.document, ReadingDocument)
                or value.document.source_anchor is None
                or value.document.text != value.document.source_text):
            raise ValueError('Batch translation requires unedited retained-source selections.')
        document = value.document
        total += len(document.source_text)
        identity = canonical({'anchor': document.source_anchor, 'topic': value.topic,
                              'source': sha256(document.source_text.encode('utf-8')).hexdigest()})
        if identity in seen:
            raise ValueError('The same source range was selected twice.')
        seen.add(identity)
    if total > MAX_CHARACTERS:
        raise ValueError('The selected document batch exceeds 2,000,000 source characters.')
    return tuple(values)


def _partition_policies(values, source_partitions):
    if source_partitions is None:
        return tuple(None for _ in values)
    if (not isinstance(source_partitions, (tuple, list))
            or len(source_partitions) != len(values)):
        raise ValueError('Every selected source needs its exact reviewed partition policy.')
    from .translation_partition import partition_ranges
    result = []
    for value, partition in zip(values, source_partitions):
        if partition is not None:
            partition_ranges(value.document.source_text, partition)
            # Capture the exact primitive policy before any observer/guard can
            # mutate the caller's mapping between review and authorization.
            partition = json.loads(canonical(partition))
        result.append(partition)
    return tuple(result)


def review_documents(service, profile, values, source, target, budget, *, glossary=None,
                     rate_per_million=None, cancelled=lambda: False, guard=lambda: None,
                     source_partitions=None):
    values = _selections(values)
    partitions = _partition_policies(values, source_partitions)
    guard()
    check_cancelled(cancelled)
    if service.library is None:
        raise ValueError('Choose the original retained library before reviewing documents.')
    library_root = local_path(service.library.root)
    library_info = library_root.stat()
    bound = money(budget)
    if profile.local and bound != 0:
        raise ValueError('Choose a zero external cost bound for local translation.')
    rows = []
    for value, partition in zip(values, partitions):
        guard()
        check_cancelled(cancelled)
        document = value.document
        policy = ({'source_partition': partition} if partition is not None else {})
        preview = service.preview(profile, document.source_text, source, target,
            glossary=glossary, rate_per_million=rate_per_million,
            preserve_paragraphs=partition is None, **policy)
        rows.append({'title': document.title, 'source_anchor': document.source_anchor,
                     'reading_positions': len(document.source_anchor['positions']),
                     'topic': value.topic, **preview})
    estimate = sum((money(row['estimated_cost']) for row in rows
                    if row['estimated_cost'] is not None), Decimal(0))
    if estimate > bound:
        raise ValueError('The complete document-batch estimate exceeds its selected total budget.')
    calls = sum(row['calls'] for row in rows)
    for row in rows:
        row['allocated_budget'] = str((bound * row['calls'] / Decimal(calls)).quantize(
            Decimal('0.000000000001'), rounding=ROUND_DOWN))
    report = {'schema': 'carr-translation-batch-review/1', 'profile': profile.to_dict(),
        'library_root': str(library_root), 'root_identity': [library_info.st_dev, library_info.st_ino],
        'source_language': source, 'target_language': target, 'glossary': glossary,
        'budget': str(bound), 'rate_per_million': rate_per_million,
        'documents': rows, 'characters': sum(row['characters'] for row in rows),
        'calls': sum(row['calls'] for row in rows), 'provider_calls_started': False,
        'preservation': 'ordered plain-text paragraph derivatives; no file-layout guarantee'}
    return {**report, 'review_sha256': sha256(canonical(report).encode('utf-8')).hexdigest()}


def prepare_documents(service, profile, values, source, target, budget, *, approved_review,
                      glossary=None, rate_per_million=None, cancelled=lambda: False,
                      guard=lambda: None, source_partitions=None):
    from .translation import translation_approval
    values = _selections(values)
    partitions = _partition_policies(values, source_partitions)
    review = review_documents(service, profile, values, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=cancelled, guard=guard,
        source_partitions=partitions)
    if type(approved_review) is not str or approved_review != review['review_sha256']:
        raise PermissionError('Review and explicitly approve this exact document batch and total budget.')
    jobs = []
    try:
        for value, reviewed, partition in zip(values, review['documents'], partitions):
            guard()
            check_cancelled(cancelled)
            document = value.document
            policy = ({'source_partition': partition} if partition is not None else {})
            authorization, cost = translation_approval(profile, document.source_text, source, target,
                reviewed['allocated_budget'], glossary=glossary, source_bound=True,
                preserve_paragraphs=partition is None, **policy)
            identity = service.submit(profile, document.source_text, source, target,
                title=document.title, authorization=authorization, per_call_cost=cost,
                source_document=document, topic=value.topic, glossary=glossary,
                preserve_paragraphs=partition is None, cancelled=cancelled, guard=guard, **policy)
            jobs.append(identity)
    except Exception as error:
        if jobs:
            raise TranslationBatchPreparedError(jobs) from error
        raise
    return {'status': 'prepared', 'jobs': jobs, 'review_sha256': approved_review,
            'provider_calls_started': False, 'external_cost_bound': str(money(budget))}


class TranslationBatchRunError(RuntimeError):
    def __init__(self, identities, outcomes, *, paused=False):
        self.receipt = {'status': 'paused' if paused else 'attention', 'selected_jobs': list(identities),
                        'jobs': list(outcomes),
                        'note': 'Inspect each durable job; no automatic replay or new approval was made.'}
        super().__init__('The document batch stopped. Previously accepted job receipts remain saved.')


class TranslationBatchExportedError(RuntimeError):
    def __init__(self, identities, exports):
        self.receipt = {'status': 'partially-published', 'selected_jobs': list(identities),
                        'exports': list(exports)}
        super().__init__('Some translation outputs were published before the batch stopped. '
                         'Keep these receipts and inspect the remaining jobs before another export.')


def _jobs(service, identities):
    if (not isinstance(identities, (tuple, list)) or not 1 <= len(identities) <= MAX_DOCUMENTS
            or any(type(identity) is not str for identity in identities)
            or len(set(identities)) != len(identities) or service.library is None):
        raise ValueError('Choose distinct saved document jobs from this library.')
    jobs = [service.get(identity) for identity in identities]
    if any(job.get('kind') != 'translation/2' for job in jobs):
        raise ValueError('Only retained-source jobs can enter a document batch.')
    return jobs


def run_documents(service, identities, *, cancelled=lambda: False, guard=lambda: None,
                  progress=lambda *args: None):
    guard()
    check_cancelled(cancelled)
    jobs = _jobs(service, identities)
    from .profiles import ProviderProfile
    profiles = ProfileStore(service.library.root / 'state' / 'providers')
    outcomes = []
    try:
        for number, (identity, job) in enumerate(zip(identities, jobs)):
            guard()
            check_cancelled(cancelled)
            # Completed durable work remains readable after an account removal.
            if job['status'] == 'complete':
                outcomes.append({'id': identity, 'status': 'complete',
                                 'completed': len(job['receipts']), 'total': len(job['chunks'])})
                continue
            profile = ProviderProfile(**job['profile'])
            def current():
                guard()
                if profiles.get(profile.id).to_dict() != profile.to_dict():
                    raise PermissionError('The selected translation profile changed; review it before resuming.')
            current()
            # The selected saved profile determines the existing gateway mode.
            # No model/server acquisition and no alternate provider fallback.
            service.gateway.mode = 'LOCAL_ONLY' if profile.local else 'APPROVED_REMOTE'
            def report(done, total, phase):
                progress(number, len(jobs), 'Document %d/%d; %d/%d chunks: %s' %
                         (number + 1, len(jobs), done, total, phase))
            result = service.resume(identity, cancelled=cancelled, guard=current, progress=report)
            outcomes.append(result)
            if result['status'] != 'complete':
                break
    except Exception as error:
        if outcomes:
            raise TranslationBatchRunError(identities, outcomes, paused=isinstance(error, InterruptedError)) from error
        raise
    return {'status': 'complete' if len(outcomes) == len(jobs)
            and all(row['status'] == 'complete' for row in outcomes) else 'paused',
            'jobs': outcomes, 'selected_jobs': list(identities)}


def export_documents(service, identities, parent, *, cancelled=lambda: False, guard=lambda: None):
    from .translation import TranslationExportedError
    guard()
    check_cancelled(cancelled)
    _jobs(service, identities)
    selected = local_path(parent)
    if not selected.is_dir():
        raise ValueError('Choose an existing parent directory for new translation exports.')
    info = selected.stat()
    parent_identity = (info.st_dev, info.st_ino)
    exports = []
    try:
        for identity in identities:
            guard()
            check_cancelled(cancelled)
            actual = local_path(parent)
            info = actual.stat()
            if actual != selected or (info.st_dev, info.st_ino) != parent_identity:
                raise ValueError('The selected batch-export parent changed.')
            destination = selected / ('Translation-' + identity[:8] + '-' + uuid4().hex[:8])
            try:
                receipt = service.export(identity, destination, cancelled=cancelled, guard=guard)
            except TranslationExportedError as error:
                exports.append({'job': identity, **error.receipt,
                    'durability_confirmed': error.durability_confirmed, 'warning': str(error)})
                raise
            exports.append({'job': identity, **receipt})
    except Exception as error:
        if exports:
            raise TranslationBatchExportedError(identities, exports) from error
        raise
    return {'status': 'complete', 'exports': exports, 'selected_jobs': list(identities)}
