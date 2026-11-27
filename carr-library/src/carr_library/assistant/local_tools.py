"""Bounded adapters for existing local reader, evidence, voice and audio services.

These functions run inside AssistantService's existing approved job and worker.
They create neither a command executor nor an independent queue or authority.
"""
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import canonical, text, local_path
from .state import regular

MAX_RESULT_BYTES = 2 * 1024**2
RULES = {
    'reader.read': ({'edition', 'revision', 'first', 'last'}, {'topic'}),
    'evidence.search': ({'query'}, {'field', 'tag', 'limit'}),
    'evidence.read': ({'topic', 'evidence'}, set()),
    'evidence.export': ({'topic', 'evidence_ids', 'name'}, {'format'}),
    'voice.list': (set(), {'language', 'limit'}),
    'audio.effects': (set(), set()),
    'audio.inspect': ({'path'}, set()),
    'audio.convert': ({'sources', 'name'}, {'format', 'settings'}),
}
WORKSPACE_TOOLS = frozenset({'reader.read', 'evidence.search', 'evidence.read', 'evidence.export'})
PUBLICATION_TOOLS = frozenset({'evidence.export', 'audio.convert'})


def _integer(value, label, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{label} must be an integer from {lower} to {upper}.')
    return value


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _bounded(value, scope):
    result = _plain(value)
    if len(canonical(result).encode('utf-8')) > min(MAX_RESULT_BYTES, scope.max_bytes):
        raise ValueError('The selected local result exceeds this plan’s byte budget; narrow the request.')
    return result


def _check(cancelled):
    if cancelled():
        raise InterruptedError('The local assistant operation was stopped.')


def _audio_settings(arguments):
    from ..audio.conversion_batch import _settings
    from ..audio.effect_presets import read_effect
    raw = arguments.get('settings', {})
    if (type(raw) is not dict or set(raw) - {
            'effects', 'sample_rate', 'channels', 'start', 'duration', 'bit_depth', 'bitrate'}
            or type(raw.get('effects', [])) is not list or len(raw.get('effects', [])) > 32):
        raise ValueError('Use only the existing typed numeric audio settings and at most 32 effects.')
    return _settings(**{**raw, 'effects': tuple(read_effect(item) for item in raw.get('effects', []))})


def check_arguments(step):
    """Pure shape checks, including for passive backups. No discovery or I/O."""
    required, optional = RULES[step.tool]
    args = step.arguments
    if not required <= set(args) or set(args) - required - optional:
        raise ValueError(f'Invalid arguments for {step.tool}.')
    for key in ('edition', 'revision', 'topic', 'evidence', 'name', 'path', 'query', 'tag', 'language', 'field', 'format'):
        if key in args:
            text(args[key], key, maximum=500 if key == 'query' else 32768)
    if step.tool == 'reader.read':
        first = _integer(args['first'], 'First reading position', 1, 10000)
        last = _integer(args['last'], 'Last reading position', first, 10000)
        if last - first + 1 > 128:
            raise ValueError('Choose at most 128 exact source reading positions.')
    if 'limit' in args:
        _integer(args['limit'], 'Result limit', 1, 500 if step.tool == 'voice.list' else 100)
    if step.tool == 'evidence.search' and args.get('field', 'both') not in ('quote', 'note', 'both'):
        raise ValueError('Search saved quotations, researcher notes, or both.')
    if step.tool == 'evidence.export':
        ids = args['evidence_ids']
        if (type(ids) is not list or not 1 <= len(ids) <= 32
                or any(type(item) is not str for item in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Choose 1–32 distinct saved evidence identities in an explicit order.')
        for item in ids:
            text(item, 'evidence identity', maximum=1024)
        if args.get('format', 'markdown') not in ('text', 'markdown', 'csv', 'html', 'docx', 'ris', 'bibtex'):
            raise ValueError('Choose an existing research export format.')
    if step.tool == 'audio.convert':
        from ..audio.conversion_batch import FORMATS, MAX_FILES
        rows = args['sources']
        if type(rows) is not list or not 1 <= len(rows) <= MAX_FILES:
            raise ValueError('Choose 1–64 explicit audio source identities.')
        seen = set()
        for row in rows:
            if type(row) is not dict or set(row) != {'path', 'sha256'}:
                raise ValueError('Each audio conversion input needs its path and reviewed SHA-256.')
            text(row['path'], 'audio input path', maximum=32768)
            if (type(row['sha256']) is not str or len(row['sha256']) != 64
                    or any(character not in '0123456789abcdef' for character in row['sha256'])):
                raise ValueError('Use the exact source SHA-256 returned by audio.inspect.')
            if row['path'] in seen:
                raise ValueError('The same selected audio pathname occurs twice.')
            seen.add(row['path'])
        if args.get('format', 'wav') not in FORMATS:
            raise ValueError('Choose an existing supported audio conversion format.')
        _audio_settings(args)


def validate_scope(step, scope, app):
    """Cheap existing-path checks; native/source work stays on the job worker."""
    args = step.arguments
    if step.tool == 'reader.read' and args['last'] - args['first'] + 1 > scope.max_files:
        raise ValueError('The selected reading range exceeds this plan’s item budget.')
    if step.tool == 'evidence.export':
        if len(args['evidence_ids']) > scope.max_files:
            raise ValueError('The selected evidence exceeds this plan’s item budget.')
    if step.tool in ('audio.inspect', 'audio.convert'):
        rows = args['sources'] if step.tool == 'audio.convert' else [{'path': args['path']}]
        if len(rows) > scope.max_files:
            raise ValueError('The selected audio exceeds this plan’s file budget.')
        total = 0
        for row in rows:
            path = scope.input(row['path'])
            total += regular(path).st_size
            if total > scope.max_bytes:
                raise ValueError('Selected audio bytes exceed this plan’s input budget.')
    if step.tool in PUBLICATION_TOOLS:
        destination = scope.output(args['name'])
        if destination.is_relative_to(app.root):
            raise PermissionError('Choose a separate output location outside private library data.')
        if os.path.lexists(destination):
            raise FileExistsError('This plan needs a new output name; existing contents were not read.')
        if not destination.parent.is_dir():
            raise ValueError('Choose an existing parent directory for this output.')
    if step.tool in ('audio.effects', 'audio.inspect', 'audio.convert') and (
            scope.max_bytes < 4096 or scope.max_seconds < .05):
        raise ValueError('Audio needs an explicit budget of at least 4096 bytes and 0.05 seconds.')


def _workspace(app, workspace, topic=None):
    from ..research.topic_tree import TopicTree
    with app.read(workspace) as db:
        if topic is not None:
            TopicTree(db, workspace)._visible(topic)


def _read_source(app, workspace, args, scope, cancelled):
    from ..reader.reading_desk import ReadingDesk
    from ..speech.source import read_source_range
    desk = ReadingDesk(app, workspace, args['edition'], topic=args.get('topic'), revision=args['revision'],
                       cancelled=cancelled, timeout=min(60, scope.max_seconds))
    selected = read_source_range(desk, args['first'], args['last'],
        guard=lambda: _workspace(app, workspace, args.get('topic')), cancelled=cancelled,
        max_characters=min(2_000_000, scope.max_bytes))
    # A later metadata/revision or retained-original change cannot turn this
    # completed read into a different edition under the approved identity.
    current = ReadingDesk(app, workspace, args['edition'], topic=args.get('topic'), revision=args['revision'],
                          cancelled=cancelled, timeout=min(60, scope.max_seconds))
    if current.identity != desk.identity or current.metadata != desk.metadata:
        raise ValueError('The selected source changed while the reader was working.')
    return _bounded({'text': selected.text, 'title': selected.title,
        'source_anchor': selected.reading_document().source_anchor, 'source_metadata': asdict(desk.metadata),
        'source_topic': args.get('topic'), 'source_sha256': sha256(selected.text.encode('utf-8')).hexdigest(),
        'warnings': selected.warnings, 'quotation_authority': False,
        'notice': 'Exact source reading positions; this read creates no quotation approval.'}, scope)


def _saved_rows(app, workspace, topic, identities, cancelled, *, render=None):
    """Use only explicit existing IDs within one authenticated read transaction."""
    from ..research.saved_evidence import SavedEvidenceStore
    from ..research.anchored_export import export_saved
    from ..research.topic_tree import TopicTree
    with app.read(workspace) as db:
        TopicTree(db, workspace)._visible(topic)
        load, metadata, _ = app._loaders(db, workspace, cancelled=cancelled)
        store = SavedEvidenceStore(db, workspace, load, metadata)
        rows, values, total = [], [], 0
        for identity in identities:
            _check(cancelled)
            row = store.get_saved(topic, identity, cancelled=cancelled)
            value = _plain(row)
            total += len(canonical(value).encode('utf-8'))
            if total > MAX_RESULT_BYTES:
                raise ValueError('Selected evidence exceeds the bounded result budget; choose fewer items.')
            rows.append(row)
            values.append(value)
        fingerprint = sha256(canonical(values).encode('utf-8')).hexdigest()
        payload = None if render is None else export_saved(workspace, topic, rows, load, format=render)
        _check(cancelled)
        return values, fingerprint, payload


@contextmanager
def _audio_runtime(app, scope, cancelled, *, output=None):
    from ..audio import AudioBudget
    from ..runtime_settings import audio_runtime
    runtime_root = app.root / 'runtime'
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    local_path(runtime_root)
    with TemporaryDirectory(prefix='assistant-audio-', dir=runtime_root) as temporary:
        budget = AudioBudget(timeout=min(scope.max_seconds, 365 * 86400), max_output_bytes=scope.max_bytes,
                             max_inputs=min(scope.max_files, 1024), threads=1)
        runtime = audio_runtime(output or temporary, state_root=app.root / 'state',
                                input_roots=scope.input_roots, budget=budget)
        yield runtime


@contextmanager
def _admission(app, scope, cancelled, identity):
    from ..operations import ResourceBroker, ResourceRequest
    broker = ResourceBroker.for_app(app.root)
    request = ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024**2)
    with broker.reserve(request, operation_id='assistant:' + identity, cancelled=cancelled,
                        timeout=min(60, scope.max_seconds)) as lease:
        _check(lease.cancelled)
        yield lease.cancelled


def _voice_list(app, scope, args, cancelled, identity):
    from ..runtime_settings import voice_registry
    with _admission(app, scope, cancelled, identity) as stopped:
        registry = voice_registry(app.root / 'state', cancelled=stopped)
        try:
            rows = [voice for voice in registry.voices()
                    if 'language' not in args or voice.language == args['language']]
            limit = min(args.get('limit', 100), scope.max_files)
            result = _bounded({'voices': rows[:limit], 'total': len(rows), 'truncated': len(rows) > limit,
                'notices': tuple(registry.notices),
                'notice': 'Actually configured local voice inventory; no synthesis, quality or language-coverage qualification.'}, scope)
            _check(stopped)
            return result
        finally:
            registry.close()


def _audio_read(app, scope, step, cancelled, identity):
    from ..audio.conversion_batch import _identity, MAX_SOURCE_BYTES
    from ..audio.dsp import effect_controls
    with _admission(app, scope, cancelled, identity) as stopped, _audio_runtime(app, scope, stopped) as runtime:
        if step.tool == 'audio.effects':
            return _bounded({'typed_controls': effect_controls(),
                'installed_runtime': runtime.capabilities(cancelled=stopped),
                'notice': 'Measured installed adapter capabilities; effect controls are bounded numeric parameters, never filter code.'}, scope)
        path = scope.input(step.arguments['path'])
        before = _identity(path, maximum=min(scope.max_bytes, MAX_SOURCE_BYTES), cancelled=stopped)
        info = runtime.inspect(path, cancelled=stopped)
        if _identity(path, maximum=min(scope.max_bytes, MAX_SOURCE_BYTES), cancelled=stopped) != before:
            raise ValueError('The selected audio changed during inspection.')
        return _bounded({'source': str(path), 'source_sha256': before['sha256'],
            'source_bytes': before['size_bytes'], 'audio': info.to_dict(),
            'notice': 'Measured existing local audio; no conversion or playback occurred.'}, scope)


def _export(app, workspace, scope, args, intent, persist, cancelled):
    from ..research.export_file import write_export, write_binary_export
    target = scope.output(args['name'])
    # This writer can report committed publication, but it has no cross-process
    # retry receipt for an unknown prior attempt. Never infer it from a path.
    if intent.get('export_attempt') is not None:
        raise PermissionError('A prior evidence export attempt needs explicit output review; it is never repeated or overwritten.')
    values, fingerprint, content = _saved_rows(app, workspace, args['topic'], args['evidence_ids'],
                                                cancelled, render=args.get('format', 'markdown'))
    _bounded({'evidence': values}, scope)
    raw = content.encode('utf-8') if isinstance(content, str) else content
    if type(raw) is not bytes or len(raw) > min(MAX_RESULT_BYTES, scope.max_bytes):
        raise ValueError('The rendered evidence exceeds this plan’s output budget.')
    def stop():
        _check(cancelled)
        _values, current, _ = _saved_rows(app, workspace, args['topic'], args['evidence_ids'], cancelled)
        if current != fingerprint:
            raise PermissionError('Selected evidence or notes changed before export publication.')
        return bool(cancelled())
    stop()
    intent['export_attempt'] = {'path': str(target), 'source_sha256': fingerprint,
        'output_sha256': sha256(raw).hexdigest(), 'size': len(raw)}
    persist()
    writer = write_export if isinstance(content, str) else write_binary_export
    receipt = writer(target, content, private_root=app.root, require_absent=True, cancelled=stop)
    intent['publication_committed'] = True
    persist()
    return {'artifacts': [str(target)], 'source_sha256': fingerprint,
        'selected_evidence': args['evidence_ids'], 'topic': args['topic'],
        'publication_committed': True, 'cleanup_warning': receipt.cleanup_warning,
        'notice': 'Existing authenticated quotations exported; the assistant created no evidence or approval.'}


def _convert(app, workspace, scope, args, intent, persist, cancelled, progress, identity):
    from ..audio.conversion_batch import AudioConversionBatch, _options
    destination = scope.output(args['name'])
    if destination.is_relative_to(app.root):
        raise PermissionError('Audio conversion cannot write into private library data.')
    settings = _audio_settings(args)
    def guard():
        if len(args['sources']) > scope.max_files:
            raise PermissionError('The conversion exceeds this plan’s file budget.')
        if workspace is not None:
            _workspace(app, workspace)
        total = 0
        for row in args['sources']:
            total += regular(scope.input(row['path'])).st_size
            if total > scope.max_bytes:
                raise PermissionError('Selected audio grew beyond this plan’s input byte budget.')
        if scope.output(args['name']) != destination:
            raise PermissionError('The selected audio output scope changed.')
    guard()
    with _audio_runtime(app, scope, cancelled, output=scope.output_root) as runtime:
        service = AudioConversionBatch(runtime, library_root=app.root, scope_guard=guard)
        reviewed = intent.get('conversion')
        if reviewed is None:
            reviewed = service.prepare([row['path'] for row in args['sources']], destination,
                format=args.get('format', 'wav'), cancelled=cancelled, **_options(settings))
            if [row['identity']['sha256'] for row in reviewed['sources']] != [row['sha256'] for row in args['sources']]:
                raise ValueError('The selected audio differs from its reviewed source hashes.')
            intent['conversion'] = reviewed
            persist()
        if (reviewed['directory'] != str(destination) or reviewed['format'] != args.get('format', 'wav')
                or reviewed['settings'] != settings
                or [(row['source'], row['identity']['sha256']) for row in reviewed['sources']]
                   != [(str(scope.input(row['path'])), row['sha256']) for row in args['sources']]):
            raise PermissionError('The conversion checkpoint differs from this plan’s explicit inputs and settings.')
        resuming = intent.get('conversion_attempted') is True and destination.exists()
        intent['conversion_attempted'] = True
        persist()
        result = service.run(reviewed, resume=resuming, cancelled=cancelled, progress=progress)
        if result['status'] != 'completed':
            if result['status'] in ('cancelled', 'timed_out') or cancelled():
                raise InterruptedError('Audio conversion stopped; explicit resume reuses verified per-file receipts.')
            raise RuntimeError('Audio conversion needs attention in its selected manifest; completed files are retained.')
        intent['publication_committed'] = True
        persist()
        return {'status': 'complete', 'directory': str(destination), 'conversion_plan_sha256': reviewed['id'],
            'artifacts': [str(destination / row['output']) for row in result['items']]
                         + [str(destination / 'conversion-manifest.json')],
            'publication_committed': True, 'warning': result['warning'],
            'notice': 'Existing local conversion runtime and resource broker; originals remain unchanged.'}


def execute(app, workspace, scope, step, intent, persist, cancelled, progress, identity):
    """Dispatch only the finite adapters above from the current authorized job."""
    from ..audio import AudioCancelled
    from ..operations import ResourceCancelled, ResourceTimeout
    _check(cancelled)
    try:
        if step.tool == 'reader.read':
            return _read_source(app, workspace, step.arguments, scope, cancelled)
        if step.tool == 'evidence.search':
            result = app.search_notebook(workspace, cancelled=cancelled,
                timeout=min(60, scope.max_seconds), **step.arguments)
            return _bounded({'results': result,
                'notice': 'Authenticated saved evidence and separately labelled researcher notes; no new quotation approval.'}, scope)
        if step.tool == 'evidence.read':
            values, fingerprint, _ = _saved_rows(app, workspace, step.arguments['topic'],
                [step.arguments['evidence']], cancelled)
            return _bounded({'evidence': values[0], 'source_sha256': fingerprint,
                'notice': 'Existing independently authenticated evidence; notes remain researcher text.'}, scope)
        if step.tool == 'evidence.export':
            return _export(app, workspace, scope, step.arguments, intent, persist, cancelled)
        if step.tool == 'voice.list':
            return _voice_list(app, scope, step.arguments, cancelled, identity)
        if step.tool in ('audio.effects', 'audio.inspect'):
            return _audio_read(app, scope, step, cancelled, identity)
        if step.tool == 'audio.convert':
            return _convert(app, workspace, scope, step.arguments, intent, persist, cancelled, progress, identity)
    except (AudioCancelled, ResourceCancelled, ResourceTimeout) as error:
        raise InterruptedError('The local operation stopped before accepting its result.') from error
    except (OSError, ValueError) as error:
        if cancelled() and not intent.get('publication_committed'):
            raise InterruptedError('Local tool stopped before completing its operation.') from error
        raise
    raise ValueError('Unsupported local assistant adapter.')


def verify_result(app, workspace, scope, step, result, cancelled):
    """Revalidate source authority for cached reading/evidence results."""
    if step.tool == 'reader.read':
        current = _read_source(app, workspace, step.arguments, scope, cancelled)
        def anchor(value):
            if type(value) is not dict or type(value.get('positions')) is not list:
                raise ValueError('Invalid completed reading source anchor.')
            copied = json.loads(canonical(value))
            for position in copied['positions']:
                position.pop('text_verified_at_capture', None)
            return copied
        if (result.get('text') != current['text'] or result.get('source_sha256') != current['source_sha256']
                or result.get('source_metadata') != current['source_metadata']
                or anchor(result.get('source_anchor')) != anchor(current['source_anchor'])
                or result.get('source_topic') != current['source_topic']
                or result.get('quotation_authority') is not False):
            raise ValueError('The completed reader result no longer matches its exact source.')
    elif step.tool in ('evidence.read', 'evidence.export'):
        ids = [step.arguments['evidence']] if step.tool == 'evidence.read' else step.arguments['evidence_ids']
        values, fingerprint, _ = _saved_rows(app, workspace, step.arguments['topic'], ids, cancelled)
        if result.get('source_sha256') != fingerprint or (
                step.tool == 'evidence.read' and result.get('evidence') != values[0]):
            raise ValueError('The selected saved evidence changed; no completed export was repeated.')
    elif step.tool == 'evidence.search':
        hits = result.get('results', {}).get('hits')
        if type(hits) is not list or len(hits) > step.arguments.get('limit', 50):
            raise ValueError('Invalid completed evidence search results.')
        for hit in hits:
            evidence = hit.get('evidence', {})
            values, _fingerprint, _ = _saved_rows(app, workspace, evidence['topic_id'],
                                                 [evidence['evidence_id']], cancelled)
            if evidence != values[0]:
                raise ValueError('A completed saved-evidence search hit changed.')
