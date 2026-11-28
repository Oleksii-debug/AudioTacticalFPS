"""Library-scoped, explicit-preview bridge to an already-open Word document."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, gettempdir
import threading
import time
from uuid import uuid4

from ..audio.cleanup import pending_cleanup
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending
from ..audio.process import run_process
from ..durable_state import atomic_bytes, atomic_json, local_path, read_json_snapshot, regular
from ..operations import ResourceBroker, ResourceRequest
from ..operations.models import ResourceCancelled, ResourceTimeout
from ..citations.bibliography_workbench import BibliographyWorkbench, StylePreview, records_for_style
from ..citations.csl import CustomStyle
from .formatting import CitationCluster, CitationRequest, render_citations
from .word_records import (
    MAX_BYTES, MAX_CLUSTERS, SCHEMA, TAG_CITATION, TAG_BIBLIOGRAPHY,
    canonical, exact, fingerprint, identity, integer, require, text, digest,
    citation_tag, bibliography_tag, validate_capture, validate_interchange,
    validate_proposal, validate_binding, validate_metadata, mutation_allowed,
)

IS_WINDOWS = os.name == 'nt'
STAGING_BYTES = 192 * 1024**2


@dataclass(frozen=True)
class WordPreview:
    token: str
    document_name: str
    action: str
    explanation: str
    references_json: str
    expires_seconds: int
    _proposal_json: bytes = field(repr=False)
    _capture_json: bytes = field(default=b'', repr=False)


@dataclass(frozen=True)
class WordOutcome:
    status: str
    message: str
    session: str
    document_name: str = ''
    receipt: dict | None = None
    capture: dict | None = field(default=None, repr=False)
    warnings: tuple[str, ...] = ()

    @property
    def may_have_changed_word(self):
        return self.status in ('COMPLETED', 'UNCERTAIN')


class WordDecision:
    """One finite explicit approval, delivered to the existing serial worker."""
    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._approved = False
        self._closed = False

    def offer(self, token):
        digest(token)
        with self._lock:
            require(not self._closed and self._token is None, 'Word approval is no longer available.')
            self._token = token

    def approve(self, token):
        with self._lock:
            require(not self._closed and self._token == token and not self._approved,
                    'Review the current Word preview before applying it.')
            self._approved = True

    def state(self, token):
        with self._lock:
            return not self._closed and self._token == token and self._approved

    def close(self):
        with self._lock:
            self._closed = True

    @property
    def closed(self):
        with self._lock:
            return self._closed


def _intent(value):
    exact(value, ('action', 'citation_id', 'items'), 'Word intent')
    require(value['action'] in ('insert_citation', 'edit_citation', 'regenerate_bibliography'),
            'Choose a supported Word operation.')
    if value['action'] == 'edit_citation':
        identity(value['citation_id'], 'citation ID')
    else:
        require(value['citation_id'] is None, 'Unexpected citation edit target.')
    require(type(value['items']) is list and len(value['items']) <= 100, 'Too many citation items.')
    if value['action'] == 'regenerate_bibliography':
        require(not value['items'], 'Bibliography regeneration uses the reviewed document references.')
    else:
        require(bool(value['items']), 'Select at least one citation reference.')
    for item in value['items']:
        exact(item, ('reference_id', 'locator', 'label', 'locator_verified',
                     'prefix', 'suffix', 'suppress_author'), 'citation item')
        text(item['reference_id'], 'reference ID', 256)
        require(type(item['locator_verified']) is bool and type(item['suppress_author']) is bool,
                'Citation verification flags must be boolean.')
        text(item['label'], 'locator label', 40)
        for key in ('prefix', 'suffix'):
            text(item[key], key, 1000, empty=True)
        if item['locator'] is not None:
            text(item['locator'], 'verified locator', 200)
            require(item['locator_verified'], 'Verify the pinpoint against its source first.')
    return value


def _restore_items(cluster):
    return tuple(CitationRequest(item['id'], item.get('locator'), item.get('label', 'page'),
        proof['locator_verified'] is True if item.get('locator') is not None else False,
        item.get('prefix', ''), item.get('suffix', ''), bool(item.get('suppress-author', False)))
        for item, proof in zip(cluster['items'], cluster['locator_evidence'], strict=True))


class _Typography(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        styles = {'i': 'italic', 'em': 'italic', 'b': 'bold', 'strong': 'bold',
                  'u': 'underline', 'sup': 'superscript', 'sub': 'subscript'}
        if tag in styles:
            self.stack.append((tag, styles[tag]))
        elif tag == 'span':
            require(dict(attrs).get('style', '').replace(' ', '') in
                    ('font-variant:small-caps', 'font-variant:small-caps;'),
                    'Unsupported citation span typography.')
            self.stack.append((tag, 'small_caps'))
        elif tag == 'br':
            self.handle_data('\n')
        else:
            raise ValueError('The canonical citation markup contains unsupported presentation.')

    def handle_endtag(self, tag):
        require(self.stack and self.stack[-1][0] == tag, 'Unbalanced citation typography.')
        self.stack.pop()

    def handle_data(self, value):
        self.parts.append({'text': value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r'),
                           'styles': sorted({style for _, style in self.stack})})


def _runs(markup, expected):
    parser = _Typography()
    parser.feed(markup); parser.close()
    require(not parser.stack, 'Incomplete citation typography.')
    parts = parser.parts
    while parts and not parts[0]['text'].strip():
        parts.pop(0)
    while parts and not parts[-1]['text'].strip():
        parts.pop()
    if parts:
        parts[0]['text'] = parts[0]['text'].lstrip()
        parts[-1]['text'] = parts[-1]['text'].rstrip()
    require(''.join(item['text'] for item in parts) == expected, 'Citation typography changed the reviewed text.')
    return parts


def _word_text(value):
    return value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r')


def build_proposal(capture, intent, *, records, preview, directory, source_scope, cancelled):
    """Reuse the existing canonical CSL facade; no second citation processor."""
    validate_capture(capture)
    _intent(intent)
    metadata = capture['metadata']
    previous = metadata['interchange'] if metadata is not None else None
    controls = {row['tag']: row for row in capture['controls']}
    stored = list(previous['clusters']) if previous else []
    require(len(stored) < MAX_CLUSTERS or intent['action'] != 'insert_citation',
            'This document reached its bounded managed citation count.')
    ordered = sorted(stored, key=lambda row: controls[citation_tag(row['id'])]['anchor'])
    cluster_items = [(row['id'], _restore_items(row),
                      controls[citation_tag(row['id'])]['anchor']) for row in ordered]
    new_id = None
    if intent['action'] in ('insert_citation', 'edit_citation'):
        selected = tuple(CitationRequest(**item) for item in intent['items'])
        if intent['action'] == 'insert_citation':
            new_id = uuid4().hex
            point = capture['selection']['start']
            cluster_items.append((new_id, selected, point))
            cluster_items.sort(key=lambda item: item[2])
        else:
            require(any(row[0] == intent['citation_id'] for row in cluster_items),
                    'The selected managed citation is not in this Word document.')
            cluster_items = [(identifier, selected if identifier == intent['citation_id'] else items, anchor)
                             for identifier, items, anchor in cluster_items]
    document_id = metadata['document_id'] if metadata else uuid4().hex
    bibliography = metadata['bibliography'] if metadata else None
    insert_bibliography = intent['action'] == 'regenerate_bibliography' and bibliography is None
    mutation_allowed(capture, insertion=new_id is not None or insert_bibliography)
    selected_ids = {item.reference_id for _, items, _ in cluster_items for item in items}
    if not selected_ids:
        selected_ids = {record['id'] for record in records}
    by_id = {record['id']: record for record in records}
    require(selected_ids <= set(by_id),
            'Some document references are outside this current library selection; choose all required active references.')
    references = [record for record in records if record['id'] in selected_ids]
    style_path = None
    style = preview.style
    if type(style) is CustomStyle:
        require(sha256(style.source).hexdigest() == style.sha256, 'The captured custom style changed.')
        style_path = directory / 'custom-style.csl'
        atomic_bytes(style_path, style.source)
        style = 'apa'  # The explicit captured style_path is authoritative.
        note_style = preview.style.info.kind == 'note'
    else:
        from .formatting import available_styles
        normalized = 'chicago-notes' if style == 'chicago-note' else style
        info = next((item for item in available_styles() if item.id == normalized), None)
        require(info is not None, 'Choose the existing installed citation style.')
        note_style = info.kind == 'note'
    if previous and stored:
        require(note_style == (previous['style']['kind'] == 'note'),
                'Changing between endnote and in-text families needs a separate document workflow.')
    clusters = []
    new_anchor = capture['selection']['start'] if new_id else None
    new_note_index = (sum(note['anchor'] < new_anchor for note in capture['endnotes']) + 1
                      if new_id and note_style else None)
    for identifier, items, anchor in cluster_items:
        note_index = None
        if note_style:
            if identifier == new_id:
                note_index = new_note_index
            else:
                note_index = controls[citation_tag(identifier)]['note_index']
                require(note_index is not None, 'Existing citation has no native note binding.')
                if new_note_index is not None and note_index >= new_note_index:
                    note_index += 1
        clusters.append(CitationCluster(items, identifier, note_index))
    rendered = render_citations(references, style=style, style_path=style_path,
        locale=preview.locale, citations=clusters, cancelled=cancelled, timeout=30)
    data = rendered.interchange
    validate_interchange(data)
    operations = []
    for item in data['citations']:
        tag = citation_tag(item['cluster_id'])
        value = _word_text(item['text'])
        operations.append({'tag': tag, 'native_id': controls[tag]['native_id'] if tag in controls else None,
            'text': value, 'runs': _runs(item['html'], value), 'note': note_style,
            'note_index': item['note_index']})
    if bibliography is not None or intent['action'] == 'regenerate_bibliography':
        pieces, runs = [], []
        for item in data['bibliography']:
            if pieces:
                runs.append({'text': '\r\r', 'styles': []})
            value = _word_text(item['text'])
            pieces.append(value); runs.extend(_runs(item['html'], value))
        operations.append({'tag': bibliography_tag(document_id),
            'native_id': bibliography['native_id'] if bibliography else None,
            'text': '\r\r'.join(pieces), 'runs': runs, 'note': False, 'note_index': None})
    proposal = {'schema': 'resonance-word-proposal/1', 'session': capture['session'],
        'capture_sha256': fingerprint(capture), 'action': intent['action'], 'source_scope': source_scope,
        'document_id': document_id, 'revision': metadata['revision'] + 1 if metadata else 1,
        'interchange': data, 'operations': operations}
    validate_proposal(proposal, capture)
    return proposal


def completed_receipt(value, proposal):
    exact(value, ('schema', 'session', 'proposal_sha256', 'document_id', 'revision',
                  'metadata_sha256', 'bindings', 'warnings', 'saved_by_bridge'), 'Word completion receipt')
    require(value['schema'] == 'resonance-word-completed/1'
            and value['session'] == proposal['session']
            and value['proposal_sha256'] == fingerprint(proposal)
            and value['document_id'] == proposal['document_id']
            and type(value['revision']) is int and value['revision'] == proposal['revision']
            and value['saved_by_bridge'] is False, 'Word completion does not bind the approved operation.')
    require(type(value['bindings']) is list and len(value['bindings']) == len(proposal['operations']),
            'Word completed binding inventory is incomplete.')
    bindings = {}
    operations = {item['tag']: item for item in proposal['operations']}
    for binding in value['bindings']:
        require(type(binding) is dict and binding.get('tag') in operations, 'Unexpected completed Word range.')
        validate_binding(binding, binding['tag'])
        operation = operations[binding['tag']]
        require(binding['tag'] not in bindings
                and binding['text_sha256'] == sha256(operation['text'].encode('utf-8')).hexdigest()
                and binding['story'] == (3 if operation['note'] else 1),
                'Completed Word range differs from preview.')
        if operation['native_id'] is not None:
            require(binding['native_id'] == operation['native_id'], 'Completed native range changed.')
        bindings[binding['tag']] = binding
    data = proposal['interchange']
    metadata = {'schema': SCHEMA, 'document_id': proposal['document_id'],
        'revision': proposal['revision'], 'interchange': data,
        'bindings': [bindings[citation_tag(row['id'])] for row in data['clusters']],
        'bibliography': bindings.get(bibliography_tag(proposal['document_id'])),
        'source_scope': proposal['source_scope'], 'last_request': proposal['session']}
    validate_metadata(metadata)
    require(value['metadata_sha256'] == fingerprint(metadata), 'Completed Word metadata digest differs.')
    require(type(value['warnings']) is list and len(value['warnings']) <= 8, 'Invalid completion notices.')
    for warning in value['warnings']:
        text(warning, 'Word completion notice', 2000)
    return value


class WordBridge:
    """One finite held child, existing broker, and the caller's existing worker."""
    def __init__(self, workbench, preview, *, guard=lambda: None):
        require(isinstance(workbench, BibliographyWorkbench) and isinstance(preview, StylePreview),
                'Open Word from the current reviewed CSL workbench.')
        self.workbench, self.preview, self.guard = workbench, preview, guard
        self.library = workbench.controller.library
        self.workspace = workbench.controller.workspace
        self.root = local_path(self.library.root, exists=True)
        info = regular(self.root, directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.last_outcome = None

    def _source(self):
        self.guard()
        require(self.workbench.controller.library is self.library
                and self.workbench.controller.workspace == self.workspace
                and Path(self.library.root).absolute() == self.root, 'The Word source library changed.')
        info = regular(local_path(self.root, exists=True), directory=True)
        require((info.st_dev, info.st_ino) == self.root_identity, 'The selected source library was replaced.')
        snapshot = self.workbench.check(self.preview)
        self.guard()
        return snapshot

    def run(self, intent=None, *, decision=None, on_preview=None, cancelled=lambda: False, timeout=180):
        require(IS_WINDOWS, 'Word integration requires Windows and an already-open compatible Word document.')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 1 <= timeout <= 300,
                'Word needs a finite total deadline.')
        mutate = intent is not None
        if mutate:
            _intent(intent)
            require(isinstance(decision, WordDecision) and callable(on_preview),
                    'Word changes require an explicit preview and approval owner.')
        snapshot = self._source()
        records, _ = records_for_style(snapshot, name_mode=self.preview.name_mode, preserve_literal_text=True)
        source_scope = {'workspace': self.workspace, 'snapshot_sha256': sha256(snapshot.encode('utf-8')).hexdigest(),
                        'name_mode': self.preview.name_mode}
        session = uuid4().hex
        deadline = time.monotonic() + timeout
        temporary_root = local_path(gettempdir(), exists=True)
        info = regular(temporary_root, directory=True)
        temporary_identity = info.st_dev, info.st_ino
        state = {'capture': None, 'proposal': None, 'preview': None, 'approved': False,
                 'granted': False, 'grant_attempted': False, 'receipt': None}
        warnings, process_error, outcome = [], None, None
        stage = None

        def before_commit():
            if cancelled() or (decision is not None and decision.closed):
                raise InterruptedError('Word operation cancelled before a confirmed completion.')
            if time.monotonic() >= deadline:
                raise TimeoutError('Word session expired; inspect and review again.')
            require(self._source() == snapshot, 'References changed after the Word preview.')
            current = regular(local_path(temporary_root, exists=True), directory=True)
            require((current.st_dev, current.st_ino) == temporary_identity, 'Word temporary parent changed.')
            return False

        def read(name):
            return read_json_snapshot(stage / name, max_bytes=MAX_BYTES)[0]

        before_commit()
        broker = ResourceBroker.for_app(self.root, cancelled=before_commit)
        request = ResourceRequest(cpu_slots=1, memory_bytes=1536 * 1024**2,
            disk_bytes=STAGING_BYTES + 20 * 1024**2, disk_path=temporary_root)
        try:
            with broker.reserve(request, operation_id='word:' + session,
                                cancelled=before_commit, timeout=min(30, timeout), run_timeout=timeout) as lease:
                before_commit(); lease.checkpoint()
                temporary = TemporaryDirectory(prefix='resonance-word-', dir=temporary_root)
                stage = local_path(temporary.name, exists=True)
                info = regular(stage, directory=True)
                stage_identity = info.st_dev, info.st_ino
                document_name = ''
                try:
                    request_value = {'schema': 'resonance-word-request/1', 'session': session,
                        'mode': 'mutate' if mutate else 'inspect',
                        'staging': {'path': str(stage), 'device': info.st_dev, 'inode': info.st_ino},
                        'timeout': max(1, deadline - time.monotonic()), 'preview_seconds': 60}
                    atomic_json(stage / 'request.json', request_value)
                    _, request_sha = read_json_snapshot(stage / 'request.json', max_bytes=MAX_BYTES)

                    def poll():
                        nonlocal document_name
                        actual = regular(local_path(stage, exists=True), directory=True)
                        require((actual.st_dev, actual.st_ino) == stage_identity, 'Word staging was replaced.')
                        if state['proposal'] is not None and os.path.lexists(stage / 'receipt.json'):
                            state['receipt'] = completed_receipt(read('receipt.json'), state['proposal'])
                            return False  # Completed native mutation wins over late cancellation.
                        before_commit(); lease.checkpoint()
                        if state['capture'] is None and os.path.lexists(stage / 'capture.json'):
                            capture = validate_capture(read('capture.json'), session)
                            state['capture'] = capture
                            document_name = capture['document']['name']
                            if mutate:
                                proposal = build_proposal(capture, intent, records=records,
                                    preview=self.preview, directory=stage, source_scope=source_scope,
                                    cancelled=before_commit)
                                before_commit(); lease.checkpoint()
                                state['proposal'] = proposal
                                token = fingerprint(proposal)
                                view = WordPreview(token, document_name, intent['action'],
                                    'Already-open Word document: ' + document_name + '\n'
                                    + 'Document identity: ' + capture['document']['full_name'] + '\n'
                                    + 'Review every managed range below. Word is not saved or closed by this bridge.\n\n'
                                    + '\n\n'.join(operation['text'] for operation in proposal['operations'])
                                    + '\n\nExisting foreign fields and legacy citation metadata remain unchanged. '
                                    + 'Approval expires after 60 seconds. No automatic retry follows uncertainty.',
                                    canonical(proposal['interchange']['references']).decode('ascii'), 60,
                                    canonical(proposal), canonical(capture))
                                state['preview'] = view
                                atomic_json(stage / 'proposal.json', proposal)
                                decision.offer(token)
                                on_preview(view)
                        if mutate and state['preview'] is not None and not state['approved']:
                            if decision.state(state['preview'].token):
                                before_commit(); lease.checkpoint()
                                atomic_json(stage / 'approved.json', {'schema': 'resonance-word-approval/1',
                                    'session': session, 'proposal_sha256': state['preview'].token})
                                state['approved'] = True
                        if mutate and state['approved'] and not state['granted'] and os.path.lexists(stage / 'ready.json'):
                            ready = read('ready.json')
                            require(ready == {'schema': 'resonance-word-ready/1', 'session': session,
                                'proposal_sha256': state['preview'].token,
                                'capture_sha256': fingerprint(state['capture'])}, 'Word-ready snapshot changed.')
                            before_commit(); lease.checkpoint()
                            state['grant_attempted'] = True
                            atomic_json(stage / 'grant.json', {'schema': 'resonance-word-grant/1',
                                'session': session, 'ready_sha256': fingerprint(ready)})
                            state['granted'] = True
                        return False

                    environment = os.environ.copy()
                    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__'):
                        environment.pop(name, None)
                    environment['PYTHONDONTWRITEBYTECODE'] = '1'
                    worker = Path(__file__).with_name('word_worker.py')
                    try:
                        result = run_process([str(Path(sys.executable).absolute()), '-I', '-B', str(worker), request_sha],
                            cwd=stage, env=environment, cancelled=poll,
                            budget=AudioBudget(timeout=max(1, deadline - time.monotonic()),
                                max_output_bytes=STAGING_BYTES, max_log_bytes=2 * 1024**2,
                                reserve_disk_bytes=16 * 1024**2, max_inputs=1, threads=1, max_staging_files=16),
                            watch=(stage,), owned_windows_job=os.name == 'nt', allow_failure=True)
                        if result.returncode:
                            process_error = RuntimeError('The owned Word process did not complete normally.')
                    except BaseException as error:
                        process_error = error
                        if isinstance(error, AudioCleanupPending):
                            try:
                                lease.settle_cleanup(error)
                            except (KeyboardInterrupt, SystemExit) as deferred:
                                # ResourceLease raises these only after native
                                # quiescence. Read the immutable receipt before
                                # classifying or deleting private transport.
                                process_error = deferred
                            warnings.append('The native process needed retained cleanup before its result could be returned.')
                    # Always read completion after native quiescence, including a
                    # cancellation raised immediately after Word finished.
                    try:
                        actual = regular(local_path(stage, exists=True), directory=True)
                        require((actual.st_dev, actual.st_ino) == stage_identity, 'Word staging identity changed.')
                        if mutate and state['proposal'] is not None and os.path.lexists(stage / 'receipt.json'):
                            observed = completed_receipt(read('receipt.json'), state['proposal'])
                            if state['receipt'] is not None:
                                require(observed == state['receipt'], 'The already-observed completion receipt changed.')
                            state['receipt'] = observed
                    except BaseException as error:
                        process_error = error
                    if state['receipt'] is not None:
                        receipt = state['receipt']
                        if process_error is not None:
                            warnings.append('Word completed the reviewed change before a later process or cancellation notice.')
                        outcome = WordOutcome('COMPLETED',
                            'Reviewed changes are present in Word. The bridge did not save the document.',
                            session, document_name, receipt, warnings=tuple((*receipt['warnings'], *warnings)))
                    elif state['grant_attempted'] or os.path.lexists(stage / 'begun.json'):
                        outcome = WordOutcome('UNCERTAIN',
                            'Word may contain some or all reviewed changes. Inspect Word and its Undo history; do not retry automatically.',
                            session, document_name, warnings=tuple(warnings))
                    elif not mutate and os.path.lexists(stage / 'capture.json') and process_error is None:
                        capture = validate_capture(read('capture.json'), session)
                        before_commit()
                        outcome = WordOutcome('INSPECTED', 'The already-open document was inspected without modification.',
                                              session, capture['document']['name'], capture=capture)
                    else:
                        if process_error is not None:
                            raise process_error
                        raise ValueError('Word did not produce a complete, current inspection or explicit preview result.')
                finally:
                    try:
                        temporary.cleanup()
                    except OSError:
                        def settle_stage():
                            try:
                                temporary.cleanup()
                                return True
                            except OSError:
                                return False
                        pending = pending_cleanup(settle_stage, message='Retaining private Word transport until cleanup is confirmed.')
                        lease.settle_cleanup(pending)
                        if outcome is not None:
                            outcome = WordOutcome(outcome.status, outcome.message, outcome.session, outcome.document_name,
                                outcome.receipt, outcome.capture, (*outcome.warnings, 'Private staging cleanup required a retained retry.'))
                self.last_outcome = outcome
                return outcome
        except BaseException as error:
            if outcome is not None and outcome.status in ('COMPLETED', 'UNCERTAIN'):
                # The native owner is already quiescent here. A lease-record
                # release or retained filesystem cleanup notice cannot erase
                # a verified mutation receipt or make uncertainty retryable.
                outcome = WordOutcome(outcome.status, outcome.message, outcome.session, outcome.document_name,
                    outcome.receipt, outcome.capture,
                    (*outcome.warnings, 'Post-operation resource cleanup needs attention; the Word outcome is unchanged.'))
                self.last_outcome = outcome
                return outcome
            if isinstance(error, (ResourceCancelled, AudioCancelled)):
                raise InterruptedError('Word operation cancelled before a confirmed native completion.') from error
            if isinstance(error, ResourceTimeout):
                raise TimeoutError('Word resource admission or execution expired.') from error
            raise
        finally:
            if decision is not None:
                decision.close()
