"""Reviewed file derivatives through the existing converters and effect editor.

Only two fixed tools live here. Preparation uses an owned staging directory.
A persisted, bounded publication intent binds every final byte before either
output is moved. Resume reconciles those exact bytes; it never overwrites an
existing output or silently runs a different source revision.
"""
from hashlib import sha256
import math
import os
from pathlib import Path
import re

from .models import canonical, local_path, text

TOOLS = frozenset(('document.convert', 'audio.effects'))
SCHEMA = 'carr-assistant-publication/1'
RECEIPT_SCHEMA = 'carr-assistant-derived-receipt/1'
MAX_SOURCE = 64 * 1024 * 1024
MAX_SCRIPT = 4 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024
MAX_REPORT = 16 * 1024 * 1024
_HEX = re.compile('[0-9a-f]{64}')
_ID = re.compile('[0-9a-f]{32}')


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError('Review the exact source checksum before this file operation.')


def _name(value):
    from ..audio.paths import filename_component
    text(value, 'new derivative filename', maximum=220)
    filename_component(value)
    if (value in ('.', '..') or value.startswith('.') or any(c in value for c in '/\\:')
            or not value.casefold().endswith('.txt')):
        raise ValueError('Choose one new .txt filename inside the approved output directory.')
    filename_component(value + '.provenance.json')
    return value


def _index(value):
    if type(value) is not int or not 0 <= value < 16:
        raise ValueError('Choose a zero-based effect position between 0 and 15.')


def _effect(value):
    from ..audio.models import Effect
    if type(value) is not dict or set(value) != {'kind', 'parameters', 'enabled'}:
        raise ValueError('An effect needs its explicit kind, parameter pairs and enabled state.')
    return Effect.from_dict(value)


def validate_arguments(step):
    if getattr(step, 'tool', None) not in TOOLS or type(getattr(step, 'arguments', None)) is not dict:
        raise ValueError('Choose one typed document conversion or effect edit.')
    args = step.arguments
    required = {'path', 'source_sha256', 'name'}
    if step.tool == 'audio.effects':
        required.update(('target', 'changes'))
    if set(args) != required:
        raise ValueError('Derivative arguments differ from the reviewed operation.')
    text(args['path'], 'selected source file', maximum=32768)
    _digest(args['source_sha256'])
    _name(args['name'])
    if step.tool == 'audio.effects':
        if args['target'] is not None:
            text(args['target'], 'selected audio role', maximum=80)
        changes = args['changes']
        if type(changes) is not list or not 1 <= len(changes) <= 32:
            raise ValueError('Review between one and 32 explicit effect edits.')
        fields = {
            'add': {'effect'}, 'edit': {'index', 'effect'},
            'move': {'index', 'destination'}, 'bypass': {'index'},
            'remove': {'index'}, 'set-chain': {'effects'}, 'master-mix': {'value'},
        }
        for change in changes:
            if type(change) is not dict or type(change.get('op')) is not str or change['op'] not in fields:
                raise ValueError('Unknown effect-edit operation.')
            if set(change) != {'op'} | fields[change['op']]:
                raise ValueError('Effect-edit fields differ from the selected operation.')
            for key in ('index', 'destination'):
                if key in change:
                    _index(change[key])
            if 'effect' in change:
                _effect(change['effect'])
            if 'effects' in change:
                if type(change['effects']) is not list or len(change['effects']) > 16:
                    raise ValueError('Choose at most sixteen effects in a chain.')
                for effect in change['effects']:
                    _effect(effect)
            if change['op'] == 'master-mix':
                value = change['value']
                if (args['target'] is not None or type(value) not in (int, float)
                        or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError('Master dry/wet mix requires the master target and a value from zero to one.')
    return step


def validate_saved_scope(step, scope, data_dir):
    # Reuse the checkpoint's pure path grammar. This opens no saved paths.
    from .validation import _path
    validate_arguments(step)
    source = _path(step.arguments['path'])
    if not any(type(source) is type(_path(root)) and source.is_relative_to(_path(root))
               for root in scope['input_roots']):
        raise ValueError('The derivative source is outside its approved input scope.')
    if scope['output_root'] is None or scope['max_files'] < 2:
        raise ValueError('A derivative needs an approved output folder and a two-file budget.')
    output = _path(scope['output_root'])
    data = _path(data_dir)
    if type(output) is type(data) and output.is_relative_to(data):
        raise ValueError('Derived outputs must remain outside the private library.')
    return step


def validate_intent(step, intent, scope):
    if type(intent) is not dict or set(intent) != {'derived_publication'}:
        raise ValueError('The derivative has no exact prepared publication intent.')
    value = intent['derived_publication']
    if (type(value) is not dict or set(value) != {
            'schema', 'operation_id', 'tool', 'source_sha256', 'files'}
            or value['schema'] != SCHEMA or value['tool'] != step.tool
            or value['source_sha256'] != step.arguments['source_sha256']
            or type(value['operation_id']) is not str or not _ID.fullmatch(value['operation_id'])):
        raise ValueError('The prepared publication differs from its reviewed operation.')
    names = [step.arguments['name'], step.arguments['name'] + '.provenance.json']
    files = value['files']
    if type(files) is not list or len(files) != 2 or scope['max_files'] < 2:
        raise ValueError('The prepared derivative needs exactly its text and provenance files.')
    total = 0
    for row, name, limit in zip(files, names, (MAX_TEXT, MAX_REPORT)):
        if (type(row) is not dict or set(row) != {'name', 'size', 'sha256'} or row['name'] != name
                or type(row['size']) is not int or not 1 <= row['size'] <= limit):
            raise ValueError('Prepared derivative bytes differ from their bounded output names.')
        _digest(row['sha256'])
        total += row['size']
    if total > scope['max_bytes']:
        raise ValueError('The complete derivative pair exceeds its approved byte budget.')
    return value


def _receipt(step, value, warning=''):
    return {'schema': RECEIPT_SCHEMA, 'operation_id': value['operation_id'],
            'tool': step.tool, 'source_sha256': value['source_sha256'],
            'output_sha256': value['files'][0]['sha256'],
            'report_sha256': value['files'][1]['sha256'],
            'original_modified': False, 'verified_for_quotation': False,
            'cleanup_warning': warning}


def validate_result(step, result, scope, intent):
    from .validation import _path
    value = validate_intent(step, intent, scope)
    if type(result) is not dict or set(result) != {'artifacts', 'derived_receipt'}:
        raise ValueError('A derivative result needs its exact file records and typed receipt.')
    receipt = result['derived_receipt']
    if type(receipt) is not dict:
        raise ValueError('Invalid derivative completion receipt.')
    warning = receipt.get('cleanup_warning')
    text(warning, 'derivative cleanup warning', maximum=2000, empty=True)
    if (receipt.get('original_modified') is not False
            or receipt.get('verified_for_quotation') is not False
            or receipt != _receipt(step, value, warning)):
        raise ValueError('The derivative receipt does not match its prepared bytes.')
    expected = [{'path': str(_path(scope['output_root']) / row['name']),
                 'size': row['size'], 'sha256': row['sha256']} for row in value['files']]
    if result['artifacts'] != expected:
        raise ValueError('The completed derivative artifacts differ from their prepared pair.')
    return result


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Assistant file preparation stopped; completed outputs are preserved.')


def _read(path, maximum, cancelled, *, retain=False):
    from ..audio.jobs import regular_file, verified_reader
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise ValueError('Choose a nonempty, regular source within the selected byte budget, without hard-link aliases.')
    digest, parts, count = sha256(), [], 0
    with verified_reader(path) as stream:
        while block := stream.read(1024 * 1024):
            _check(cancelled)
            count += len(block)
            if count > maximum:
                raise ValueError('The file grew beyond its selected byte budget.')
            digest.update(block)
            if retain:
                parts.append(block)
    _check(cancelled)
    if count != info.st_size:
        raise ValueError('The selected file changed while its bytes were read.')
    return {'size': count, 'sha256': digest.hexdigest()}, b''.join(parts) if retain else None


def capture_source(path, scope, *, maximum=MAX_SOURCE, cancelled=lambda: False):
    source = scope.input(path)
    record, _ = _read(source, min(maximum, scope.max_bytes), cancelled)
    return source, record


def _edit_script(raw, args):
    from ..audio.effect_editing import RoleEffectEditor
    editor = RoleEffectEditor(raw.decode('utf-8', errors='strict'))
    target = args['target']
    editor.effects(target)  # Require an actual current role or the master.
    for change in args['changes']:
        operation = change['op']
        if operation == 'add':
            editor.add(target, _effect(change['effect']))
        elif operation == 'edit':
            editor.edit(target, change['index'], _effect(change['effect']))
        elif operation == 'move':
            editor.move(target, change['index'], change['destination'])
        elif operation == 'bypass':
            editor.bypass(target, change['index'])
        elif operation == 'remove':
            editor.remove(target, change['index'])
        elif operation == 'set-chain':
            editor.set_chain(target, tuple(_effect(value) for value in change['effects']))
        else:
            editor.set_master_mix(change['value'])
    return editor.script().encode('utf-8')


def validate_live(step, scope, data_dir, *, cancelled=lambda: False):
    validate_saved_scope(step, scope.to_dict(), str(data_dir))
    source = scope.input(step.arguments['path'])
    record, raw = _read(source, min(MAX_SCRIPT if step.tool == 'audio.effects' else MAX_SOURCE,
                                   scope.max_bytes), cancelled, retain=step.tool == 'audio.effects')
    if record['sha256'] != step.arguments['source_sha256']:
        raise PermissionError('The selected source changed after review; prepare and approve a new plan.')
    for name in (step.arguments['name'], step.arguments['name'] + '.provenance.json'):
        if os.path.lexists(scope.output(name)):
            raise FileExistsError('The new derivative destination already exists; choose another name.')
    if step.tool == 'audio.effects':
        _edit_script(raw, step.arguments)
    else:
        from ..conversion.capabilities import CONVERSION_SUFFIXES
        if source.suffix.casefold() not in CONVERSION_SUFFIXES:
            raise ValueError('This file is not supported by the current reading converter.')
    return source, record, raw


def _file_record(path, cancelled, *, maximum=MAX_TEXT):
    record, _ = _read(path, maximum, cancelled)
    return {'name': path.name, **record}


def _matches(path, row, cancelled):
    record, _ = _read(path, max(1, row['size']), cancelled)
    if record != {'size': row['size'], 'sha256': row['sha256']}:
        raise PermissionError('A prepared or published derivative changed; its bytes were preserved for review.')


def _write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _cleanup(stage, identity, owned):
    """Remove only exact owned stages; preserve unexpected files for review."""
    from ..audio.jobs import checked_directory, file_identity, regular_file
    current = checked_directory(stage).stat()
    if (current.st_dev, current.st_ino) != identity:
        raise ValueError('The owned preparation directory changed.')
    for row, stamp in owned:
        path = stage / row['name']
        if not os.path.lexists(path):
            continue
        if file_identity(regular_file(path)) != stamp:
            raise ValueError('A staged file changed before cleanup.')
        _matches(path, row, lambda: False)
        path.unlink()
    stage.rmdir()


def execute(step, scope, data_dir, intent, persist, *, operation_id,
            cancelled=lambda: False, progress=lambda message: None):
    from ..audio.jobs import checked_directory, file_identity, regular_file, publish_noreplace
    validate_saved_scope(step, scope.to_dict(), str(data_dir))
    if type(operation_id) is not str or not _ID.fullmatch(operation_id):
        raise ValueError('The assistant operation identity is invalid.')
    root = checked_directory(local_path(scope.output_root))
    root_info = root.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    stage = root / ('.assistant-derived-' + operation_id)
    scope_dict = scope.to_dict()
    if not intent:
        source, source_record, raw = validate_live(step, scope, data_dir, cancelled=cancelled)
        _check(cancelled)
        stage.mkdir(mode=0o700)  # Existing, unowned staging is never adopted.
        stage_info = checked_directory(stage).stat()
        stage_identity = (stage_info.st_dev, stage_info.st_ino)
        owned = []
        try:
            output = stage / step.arguments['name']
            report = stage / (step.arguments['name'] + '.provenance.json')
            if step.tool == 'document.convert':
                from ..conversion import ConversionBudget, ConversionService
                from ..operations import ResourceBroker
                budget = ConversionBudget(max_source_bytes=min(MAX_SOURCE, scope.max_bytes),
                    max_text_bytes=min(MAX_TEXT, scope.max_bytes),
                    timeout=max(.1, min(120.0, scope.max_seconds)))
                progress('Preparing reading text with the existing isolated document converter.')
                converted = ConversionService(stage, input_roots=(source,), budget=budget).extract(
                    source, output, cancelled=cancelled,
                    broker=ResourceBroker.for_app(data_dir, cancelled=cancelled))
                if (converted.path != output or converted.report_path != report
                        or converted.report['source_sha256'] != step.arguments['source_sha256']):
                    raise PermissionError('The converted derivative does not match the approved source.')
            else:
                progress('Preparing the reviewed role or master effect edits.')
                updated = _edit_script(raw, step.arguments)
                if not 0 < len(updated) <= min(MAX_TEXT, scope.max_bytes):
                    raise ValueError('The edited script exceeds its approved byte budget.')
                _write(output, updated)
                row = _file_record(output, lambda: False)
                owned.append((row, file_identity(regular_file(output))))
                report_value = {'schema': 'carr-audio-effect-derivative/1',
                    'operation_id': operation_id, 'source_name': source.name,
                    'source_sha256': source_record['sha256'], 'source_size': source_record['size'],
                    'text_sha256': sha256(updated).hexdigest(), 'text_size': len(updated),
                    'target': step.arguments['target'], 'changes': step.arguments['changes'],
                    'original_modified': False, 'spoken_text_modified': False,
                    'verified_for_quotation': False, 'rendered_audio': False}
                _write(report, (canonical(report_value) + '\n').encode('utf-8'))
            files = [_file_record(output, lambda: False),
                     _file_record(report, lambda: False, maximum=MAX_REPORT)]
            owned = [(row, file_identity(regular_file(stage / row['name']))) for row in files]
            value = {'schema': SCHEMA, 'operation_id': operation_id, 'tool': step.tool,
                     'source_sha256': step.arguments['source_sha256'], 'files': files}
            validate_intent(step, {'derived_publication': value}, scope_dict)
            # Recheck the selected source and all final names before committing
            # publication authority. The original is never opened for writing.
            _, current = capture_source(source, scope, cancelled=cancelled)
            if current != source_record:
                raise PermissionError('The source changed during preparation; review the current file again.')
            for row in files:
                if os.path.lexists(scope.output(row['name'])):
                    raise FileExistsError('A derivative destination appeared during preparation.')
            _check(cancelled)
            intent['derived_publication'] = value
            persist()  # No final output exists before its exact intent is durable.
        except BaseException:
            if not intent:
                try:
                    _cleanup(stage, stage_identity, owned)
                except (OSError, ValueError, RuntimeError):
                    pass  # Unexpected staging is preserved, never recursively removed.
            raise
    value = validate_intent(step, intent, scope_dict)
    if value['operation_id'] != operation_id:
        raise PermissionError('Prepared publication belongs to another assistant operation.')
    actual_root = checked_directory(root).stat()
    if (actual_root.st_dev, actual_root.st_ino) != root_identity:
        raise PermissionError('The selected output directory changed.')
    # Reconcile only the exact approved names and bytes. A crash after the first
    # move leaves a resumable pair, with the provenance report published last.
    pending = []
    for row in value['files']:
        target = scope.output(row['name'])
        if os.path.lexists(target):
            _matches(target, row, cancelled)
        else:
            checked_directory(stage)
            staged = local_path(stage / row['name'])
            _matches(staged, row, cancelled)
            pending.append((staged, target))
    for staged, target in pending:
        _check(cancelled)
        publish_noreplace(staged, target, cancelled=cancelled)
    # Both files now exist: late cancellation cannot turn a completed pair
    # into a cancelled result. A changed artifact still fails verification.
    for row in value['files']:
        _matches(scope.output(row['name']), row, lambda: False)
    warning = ''
    if os.path.lexists(stage):
        try:
            checked_directory(stage)
            stage.rmdir()
        except (OSError, ValueError, RuntimeError):
            warning = 'The derivative is complete; its preparation directory remains for review.'
    result = {'artifacts': [{'path': str(scope.output(row['name'])), 'size': row['size'],
                             'sha256': row['sha256']} for row in value['files']],
              'derived_receipt': _receipt(step, value, warning)}
    return validate_result(step, result, scope_dict, intent)
