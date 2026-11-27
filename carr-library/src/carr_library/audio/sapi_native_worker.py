"""Fixed file-framed Windows SAPI worker; no PowerShell or source-side imports.

Adapted from the canonical CARR native SAPI protocol (e3cf74c243d4), using the
already admitted pywin32 312 COM client. Only in-process SAPI classes are
constructed. See SAPI_NATIVE_PROVENANCE.md for source and dependency identities.
"""
from __future__ import annotations

from importlib.metadata import version
import json
import locale
import os
from pathlib import Path
import stat
import struct
import sys

_MAX_REQUEST = 128_000
_MAX_REPLY = 2_000_000
_MAX_VOICES = 4096
_CLASSES = frozenset(('SAPI.SpVoice', 'SAPI.SpFileStream', 'SAPI.SpAudioFormat'))


def _plain(value, label, maximum=4096, *, text=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 and (not text or char not in '\n\t\r') for char in value)
            or any(127 <= ord(char) < 160 for char in value)):
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate worker request field.')
        result[key] = value
    return result


def _regular(path, *, directory=False):
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or (not directory and value.st_nlink != 1)):
        raise ValueError('The native speech worker requires regular private staging paths.')
    return value


def _validate(data, directory):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('Unsupported native speech request.')
    operation = data.get('operation')
    if operation == 'voices':
        if set(data) != {'version', 'operation'}:
            raise ValueError('Unexpected native speech request fields.')
    elif operation == 'render':
        if set(data) != {'version', 'operation', 'token', 'id', 'name', 'text', 'output',
                         'rate', 'volume', 'punctuation'}:
            raise ValueError('Unexpected native speech request fields.')
        for field, maximum in (('token', 4096), ('id', 2048), ('name', 2048)):
            if not _plain(data[field], 'voice ' + field, maximum):
                raise ValueError('Select an installed native speech token.')
        if data['token'].rsplit('\\', 1)[-1] != data['id']:
            raise ValueError('The selected voice leaf and full token differ.')
        if not _plain(data['text'], 'speech text', 8000, text=True).strip():
            raise ValueError('Speech text must not be blank.')
        if type(data['rate']) is not int or not -10 <= data['rate'] <= 10:
            raise ValueError('Invalid native speech rate.')
        if type(data['volume']) is not int or not 0 <= data['volume'] <= 100:
            raise ValueError('Invalid native speech volume.')
        if type(data['punctuation']) is not bool:
            raise ValueError('Invalid native speech punctuation setting.')
        output = Path(_plain(data['output'], 'output path', 32_000))
        if (not output.is_absolute() or output.name != 'speech.wav'
                or output.parent.resolve() != directory.resolve()
                or os.path.lexists(output)):
            raise ValueError('The native speech output must be a new private staging WAV.')
    else:
        raise ValueError('Unsupported native speech operation.')
    return data


def _attribute(token, name, maximum):
    try:
        return _plain(str(token.GetAttribute(name)), 'voice attribute', maximum)
    except Exception:
        return ''


def _voice(token, default):
    identity = _plain(str(token.Id), 'voice token')
    leaf = identity.rsplit('\\', 1)[-1]
    # System.Speech uses the registry Name attribute, then the default value.
    name = _attribute(token, 'Name', 2048) or _plain(str(token.GetDescription(0)), 'voice name', 2048)
    raw_language = _attribute(token, 'Language', 256)
    try:
        language = locale.windows_locale[int(raw_language.split(';')[0].strip(), 16)].replace('_', '-')
    except (ValueError, KeyError):
        language = 'und'
    if not leaf or len(leaf) > 2048 or not name:
        raise ValueError('An installed SAPI token lacks its stable voice identity.')
    return {'id': leaf, 'token': identity, 'name': name, 'language': language,
            'version': 'sapi-native-1;pywin32-312', 'default': identity == default}


def _dispatch(name, pythoncom, dynamic):
    if name not in _CLASSES:
        raise ValueError('Only the fixed local SAPI classes are allowed.')
    # Construct a new in-process COM object directly; never attach to a running
    # object, invoke makepy, or request an out-of-process/local-server fallback.
    raw = pythoncom.CoCreateInstance(pythoncom.CLSIDFromProgID(name), None,
                                     pythoncom.CLSCTX_INPROC_SERVER, pythoncom.IID_IDispatch)
    return dynamic.Dispatch(raw, userName=name)


def _perform(data, phase, pythoncom, dynamic):
    phase[0] = 'creating-speaker'
    speaker = _dispatch('SAPI.SpVoice', pythoncom, dynamic)
    phase[0] = 'enumerating-voices'
    tokens = speaker.GetVoices('', '')
    count = int(tokens.Count)
    if not 0 <= count <= _MAX_VOICES:
        raise ValueError('The native voice inventory exceeds its budget.')
    default = str(speaker.Voice.Id) if count else ''
    rows, selected, seen = [], None, set()
    for index in range(count):
        token = tokens.Item(index)
        row = _voice(token, default)
        if row['id'] in seen:
            raise ValueError('Installed SAPI token leaves are ambiguous; no voice was rebound.')
        seen.add(row['id'])
        rows.append(row)
        if data['operation'] == 'render' and row['token'] == data['token']:
            if row['id'] != data['id'] or row['name'] != data['name']:
                raise ValueError('The selected installed SAPI voice identity has changed.')
            selected = token
    identity = {'version': 1, 'architecture': str(struct.calcsize('P') * 8),
                'adapter': 'native-pywin32', 'client_version': '312'}
    if data['operation'] == 'voices':
        return {**identity, 'voices': rows}
    if selected is None:
        raise ValueError('The selected installed SAPI voice is no longer available.')
    phase[0] = 'selecting-voice'
    speaker.Voice = selected
    if str(speaker.Voice.Id) != data['token']:
        raise ValueError('SAPI did not select the requested installed token.')
    speaker.Rate, speaker.Volume = data['rate'], data['volume']
    phase[0] = 'creating-audio-stream'
    stream = _dispatch('SAPI.SpFileStream', pythoncom, dynamic)
    format_object = _dispatch('SAPI.SpAudioFormat', pythoncom, dynamic)
    format_object.Type = 22  # SAFT22kHz16BitMono; actual PCM, no device playback.
    stream.Format = format_object
    opened = False
    try:
        phase[0] = 'opening-audio-stream'
        stream.Open(data['output'], 3, False)
        opened = True
        speaker.AllowAudioOutputFormatChangesOnNextSet = False
        speaker.AudioOutputStream = stream
        phase[0] = 'synthesizing'
        # SVSFIsNotXML=16; optional SVSFNLPSpeakPunc=64. No filenames or SSML.
        speaker.Speak(data['text'], 16 | (64 if data['punctuation'] else 0))
        phase[0] = 'closing-audio-stream'
    finally:
        if opened:
            stream.Close()
    return {**identity, 'status': 'rendered',
            'token': data['token'], 'id': data['id'], 'name': data['name']}


def _operate(data, phase):
    if os.name != 'nt':
        raise RuntimeError('Native SAPI requires Windows.')
    phase[0] = 'loading-com-client'
    if version('pywin32') != '312':
        raise RuntimeError('Native SAPI requires the reviewed pywin32 312 client.')
    import pythoncom
    from win32com.client import dynamic
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        return _perform(data, phase, pythoncom, dynamic)
    finally:
        pythoncom.CoUninitialize()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        return 2
    request, reply = (Path(item).absolute() for item in arguments)
    try:
        if (request.name != 'request.json' or reply.name != 'reply.json'
                or request.parent != reply.parent or os.path.lexists(reply)
                or str(request).startswith(('//', '\\\\'))):
            return 2
        for parent in reversed((request.parent, *request.parent.parents)):
            _regular(parent, directory=True)
        if _regular(request).st_size > _MAX_REQUEST:
            return 2
    except (OSError, ValueError):
        return 2
    phase = ['validating-request']
    try:
        raw = request.read_bytes()
        if len(raw) > _MAX_REQUEST:
            raise ValueError('Speech request budget exceeded.')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        result = _operate(_validate(data, request.parent), phase)
        code = 0
    except Exception as error:
        # Never surface a COM description containing source text or user paths.
        result = {'version': 1, 'status': 'failed', 'phase': phase[0]}
        hresult = getattr(error, 'hresult', None)
        if type(hresult) is int:
            result['hresult'] = hresult & 0xffffffff
        code = 1
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _MAX_REPLY:
        return 2
    try:
        with reply.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        return 2
    return code


if __name__ == '__main__':
    raise SystemExit(main())
