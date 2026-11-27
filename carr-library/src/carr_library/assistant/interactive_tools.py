"""Pure contracts for explicitly reviewed native reader/player commands.

Saved receipts are inert observations, never authority to reconnect a window or
replay an interactive command. This module opens no files and imports no UI,
voice runtime, native player or model loader.
"""
import math
import re

TOOLS = frozenset(('reader.control', 'player.control'))
TARGET_SCHEMA = 'carr-native-reader-target/1'
RECEIPT_SCHEMA = 'carr-interactive-receipt/1'
_HEX = re.compile('[0-9a-f]{64}')
_ID = re.compile('[0-9a-f]{32}')
_LOCATION = {'workspace', 'edition', 'revision', 'kind', 'index', 'text_sha256', 'asset_sha256'}
_SETTINGS = {'voice', 'language', 'rate', 'pitch', 'volume', 'playback_tempo', 'playback_pitch'}
_PCM = {'sha256', 'frames', 'sample_rate', 'start', 'end'}
_EFFECTS = {
    ('reader.control', 'select'): 'selection_applied',
    ('reader.control', 'prepare'): 'utterance_prepared',
    ('reader.control', 'read'): 'playback_started',
    ('reader.control', 'stop'): 'reader_stopped',
    ('player.control', 'play'): 'playback_started',
    ('player.control', 'pause'): 'playback_paused',
    ('player.control', 'resume'): 'playback_resumed',
    ('player.control', 'seek'): 'playback_seeked',
    ('player.control', 'stop'): 'playback_stopped',
}


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)):
        raise ValueError('Interactive command text must be bounded plain Unicode.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError('Interactive command text contains invalid Unicode.') from None


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Interactive command integer is outside its explicit bound.')


def _number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Interactive command number is outside its explicit bound.')


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError('Interactive commands require exact source and PCM digests.')


def validate_target(value):
    if (type(value) is not dict or set(value) != {'schema', 'session_id', 'location', 'source_chars'}
            or value['schema'] != TARGET_SCHEMA or type(value['session_id']) is not str
            or not _ID.fullmatch(value['session_id'])):
        raise ValueError('Choose one exact currently open source reader.')
    _integer(value['source_chars'], 1, 2_000_000)
    location = value['location']
    if type(location) is not dict or set(location) != _LOCATION:
        raise ValueError('The interactive target needs its complete immutable source location.')
    for key in ('workspace', 'edition', 'revision'):
        _text(location[key], 512)
    if location['kind'] not in ('physical_page', 'logical_segment'):
        raise ValueError('Invalid interactive source locator.')
    _integer(location['index'], 1, 1_000_000)
    for key in ('text_sha256', 'asset_sha256'):
        _digest(location[key])
    return value


def validate_settings(value):
    if type(value) is not dict or set(value) != _SETTINGS:
        raise ValueError('Capture the exact selected voice and independent playback settings.')
    _text(value['voice'], 2048)
    if ':' not in value['voice']:
        raise ValueError('Select an enumerated local voice.')
    _text(value['language'], 64)
    for name, low, high in (('rate', -10, 10), ('pitch', -12, 12), ('volume', 0, 1),
                           ('playback_tempo', .25, 4), ('playback_pitch', -12, 12)):
        _number(value[name], low, high)
    return value


def validate_pcm(value, target):
    if type(value) is not dict or set(value) != _PCM:
        raise ValueError('Select one exact prepared utterance for player control.')
    _digest(value['sha256'])
    _integer(value['frames'], 1, 2**63 - 1)
    _integer(value['sample_rate'], 1, 384000)
    _integer(value['start'], 0, target['source_chars'])
    _integer(value['end'], value['start'] + 1, target['source_chars'])
    return value


def validate_arguments(step):
    if getattr(step, 'tool', None) not in TOOLS or type(getattr(step, 'arguments', None)) is not dict:
        raise ValueError('An allowed typed interactive command is required.')
    value = step.arguments
    action = value.get('action')
    if type(action) is not str or (step.tool, action) not in _EFFECTS:
        raise ValueError('The selected native reader/player action is not supported.')
    expected = {'target', 'action'}
    if step.tool == 'reader.control':
        if action in ('select', 'prepare', 'read'):
            expected.update(('start', 'end'))
        if action in ('prepare', 'read'):
            expected.add('settings')
    else:
        expected.add('pcm')
        if action == 'seek':
            expected.add('seconds')
    if set(value) != expected:
        raise ValueError('Interactive command fields differ from the selected action.')
    target = validate_target(value['target'])
    if 'start' in value:
        _integer(value['start'], 0, target['source_chars'])
        _integer(value['end'], value['start'] + (action != 'select'), target['source_chars'])
    if 'settings' in value:
        validate_settings(value['settings'])
    if 'pcm' in value:
        validate_pcm(value['pcm'], target)
    if 'seconds' in value:
        _number(value['seconds'], 0, value['pcm']['frames'] / value['pcm']['sample_rate'])
    return step


def validate_saved_scope(step, scope_dict, data_dir):
    validate_arguments(step)
    if (type(scope_dict) is not dict or type(scope_dict.get('workspace_id')) is not str
            or not scope_dict['workspace_id']
            or scope_dict['workspace_id'] != step.arguments['target']['location']['workspace']):
        raise ValueError('The native command must remain in its explicitly selected current workspace.')
    # data_dir is validated by the existing job validator. No saved path is
    # opened, resolved or granted new authority here.
    return step


def validate_result(step, result, scope_dict, intent):
    validate_saved_scope(step, scope_dict, None)
    if (type(intent) is not dict or set(intent) != {'interactive_dispatch'}
            or type(intent['interactive_dispatch']) is not str
            or not _ID.fullmatch(intent['interactive_dispatch'])):
        raise ValueError('An interactive receipt needs its original one-time dispatch intent.')
    if (type(result) is not dict or set(result) != {'artifacts', 'interactive_receipt'}
            or type(result['artifacts']) is not list or result['artifacts']):
        raise ValueError('Native interactive controls cannot claim produced file artifacts.')
    row = result['interactive_receipt']
    keys = {'schema', 'operation_id', 'tool', 'action', 'target', 'effect', 'observation'}
    if (type(row) is not dict or set(row) != keys or row['schema'] != RECEIPT_SCHEMA
            or row['operation_id'] != intent['interactive_dispatch'] or row['tool'] != step.tool
            or row['action'] != step.arguments['action'] or row['target'] != step.arguments['target']
            or row['effect'] != _EFFECTS[(step.tool, step.arguments['action'])]):
        raise ValueError('The interactive receipt differs from the exact reviewed command.')
    value, args = row['observation'], step.arguments
    if type(value) is not dict or set(value) != {'selection', 'pcm', 'playback', 'settings'}:
        raise ValueError('An interactive receipt needs exact bounded observations.')
    selection = value['selection']
    if type(selection) is not list or len(selection) != 2:
        raise ValueError('An interactive receipt needs the actual selected source range.')
    _integer(selection[0], 0, args['target']['source_chars'])
    _integer(selection[1], selection[0], args['target']['source_chars'])
    action = args['action']
    if value['pcm'] is not None:
        validate_pcm(value['pcm'], args['target'])
    if value['settings'] is not None:
        validate_settings(value['settings'])
    if value['settings'] != args.get('settings'):
        raise ValueError('The prepared speech settings differ from the approved snapshot.')
    playback = value['playback']
    if playback is not None:
        if (type(playback) is not dict or set(playback) != {
                'state', 'position_frames', 'total_frames', 'sample_rate', 'position_accuracy'}
                or playback['state'] not in ('starting', 'playing', 'paused', 'stopped', 'completed')
                or playback['position_accuracy'] not in ('not_started', 'driver_reported', 'completed_buffers')):
            raise ValueError('Interactive playback needs an actual bounded native observation.')
        _integer(playback['total_frames'], 1, 2**63 - 1)
        _integer(playback['position_frames'], 0, playback['total_frames'])
        _integer(playback['sample_rate'], 1, 384000)
        if value['pcm'] is None or any(playback[key] != value['pcm'][other] for key, other in
                (('total_frames', 'frames'), ('sample_rate', 'sample_rate'))):
            raise ValueError('The observed player does not contain the selected prepared PCM.')
    if step.tool == 'reader.control':
        if action == 'select' and (selection != [args['start'], args['end']] or value['pcm'] is not None
                                   or playback is not None):
            raise ValueError('The exact requested source selection was not applied.')
        if action in ('prepare', 'read'):
            pcm = value['pcm']
            if (pcm is None or pcm['start'] < args['start'] or pcm['end'] > args['end']
                    or selection != [pcm['start'], pcm['end']]):
                raise ValueError('The prepared utterance differs from the reviewed source range.')
            if action == 'prepare' and playback is not None:
                raise ValueError('Prepare cannot silently start playback.')
    elif action != 'stop' and value['pcm'] != args['pcm']:
        raise ValueError('Player control acted on different prepared PCM.')
    if action == 'stop' and (value['pcm'] is not None or playback is not None):
        raise ValueError('Stop must settle the owned reader and release its prepared PCM.')
    if (step.tool == 'reader.control' and action == 'read' or step.tool == 'player.control' and action in ('play', 'resume')):
        if playback is None or playback['state'] not in ('starting', 'playing', 'completed'):
            raise ValueError('Playback start/resume was not actually observed.')
    if step.tool == 'player.control' and action == 'pause' and (playback is None or playback['state'] != 'paused'):
        raise ValueError('The selected player did not reach its paused state.')
    if step.tool == 'player.control' and action == 'seek' and (playback is None or
            abs(playback['position_frames'] / playback['sample_rate'] - args['seconds']) > .25):
        raise ValueError('The selected native player did not report its sought position.')
    return result
