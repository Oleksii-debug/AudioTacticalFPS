"""Isolated Windows SAPI 5 worker using the reviewed comtypes COM client.

Only fixed local SAPI classes and operations are accepted. Source text is a
literal BSTR with SVSFIsNotXML; it is never Python, PowerShell, SSML or a shell
argument. File request/reply framing also works in a windowed frozen EXE.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import struct
import sys

_MAX_REQUEST = 128_000
_MAX_REPLY = 2_000_000
_MAX_VOICES = 1000


def _plain(value, label, maximum=4096):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 and c not in '\n\t\r' for c in value)):
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
        if set(data) != {'version', 'operation', 'token', 'text', 'output', 'rate', 'volume', 'punctuation'}:
            raise ValueError('Unexpected native speech request fields.')
        _plain(data['token'], 'voice token')
        if not data['token']:
            raise ValueError('Select an installed native speech token.')
        if not _plain(data['text'], 'speech text', 24_000).strip():
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


def _operate(data, phase):
    if os.name != 'nt':
        raise RuntimeError('Native SAPI requires Windows.')
    phase[0] = 'loading-com-client'
    import comtypes
    import comtypes.client
    # Dynamic dispatch needs no generated typelib wrapper or writable app code.
    comtypes.client.gen_dir = None
    phase[0] = 'creating-speaker'
    speaker = comtypes.client.CreateObject('SAPI.SpVoice', dynamic=True,
                                           clsctx=comtypes.CLSCTX_INPROC_SERVER)
    phase[0] = 'enumerating-voices'
    tokens = speaker.GetVoices('', '')
    count = int(tokens.Count)
    if not 0 <= count <= _MAX_VOICES:
        raise ValueError('The native voice inventory exceeds its budget.')
    if data['operation'] == 'voices':
        voices = []
        for index in range(count):
            token = tokens.Item(index)
            voices.append({'token': _plain(str(token.Id), 'voice token'),
                'name': _plain(str(token.GetDescription(0)), 'voice name', 2048),
                'language': _attribute(token, 'Language', 256),
                'vendor': _attribute(token, 'Vendor', 512)})
        return {'version': 1, 'architecture': str(struct.calcsize('P') * 8),
                'adapter': 'native-comtypes', 'voices': voices}
    selected = None
    for index in range(count):
        token = tokens.Item(index)
        if token.Id == data['token']:
            selected = token
            break
    if selected is None:
        raise ValueError('The selected installed SAPI voice is no longer available.')
    phase[0] = 'selecting-voice'
    speaker.Voice = selected
    speaker.Rate = data['rate']
    speaker.Volume = data['volume']
    phase[0] = 'creating-audio-stream'
    stream = comtypes.client.CreateObject('SAPI.SpFileStream', dynamic=True,
                                          clsctx=comtypes.CLSCTX_INPROC_SERVER)
    format_object = comtypes.client.CreateObject('SAPI.SpAudioFormat', dynamic=True,
                                                  clsctx=comtypes.CLSCTX_INPROC_SERVER)
    # SAFT22kHz16BitMono = 22: real uncompressed PCM, no device playback.
    format_object.Type = 22
    stream.Format = format_object
    opened = False
    try:
        phase[0] = 'opening-audio-stream'
        stream.Open(data['output'], 3, False)  # SSFMCreateForWrite
        opened = True
        speaker.AudioOutputStream = stream
        phase[0] = 'synthesizing'
        # SVSFIsNotXML=16; SVSFNLPSpeakPunc=64. No XML interpretation or URLs.
        flags = 16 | (64 if data['punctuation'] else 0)
        speaker.Speak(data['text'], flags)
        phase[0] = 'closing-audio-stream'
    finally:
        if opened:
            stream.Close()
    return {'version': 1, 'architecture': str(struct.calcsize('P') * 8),
            'adapter': 'native-comtypes', 'status': 'rendered'}


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        return 2
    request, reply = (Path(x).absolute() for x in arguments)
    # The reply is a fixed sibling, not an arbitrary path supplied inside JSON.
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
        # COM descriptions can contain engine/user/source data. Expose only a
        # fixed phase and numeric HRESULT for actual native diagnostics.
        result = {'version': 1, 'status': 'failed', 'phase': phase[0]}
        if phase[0] == 'loading-com-client':
            result['error'] = 'com-client-unavailable'
        elif phase[0] == 'validating-request':
            result['error'] = 'invalid-request'
        else:
            result['error'] = 'native-com-failure'
        hresult = getattr(error, 'hresult', None)
        if type(hresult) is int:
            result['hresult'] = hresult & 0xffffffff
        code = 1
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _MAX_REPLY:
        return 2
    try:
        with reply.open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        return 2
    return code


if __name__ == '__main__':
    raise SystemExit(main())
