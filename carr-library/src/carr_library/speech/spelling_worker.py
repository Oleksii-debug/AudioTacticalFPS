"""Isolated, read-only Windows Spell Checking API bridge.

This file is executed from captured, hash-checked source bytes by spelling.py.
Only installed providers are queried. No Add/Ignore/AutoCorrect or dictionary
installation API is called. See docs/WINDOWS_SPELLING.md for SDK provenance.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

MAX_TEXT = 50000
MAX_ERRORS = 256
MAX_LANGUAGES = 256
MAX_SUGGESTIONS = 12
MAX_REPLACEMENT = 256
MAX_REQUEST_BYTES = 512 * 1024
MAX_REPLY_BYTES = 1024 * 1024
LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}\Z')


class NativeFailure(Exception):
    def __init__(self, code='native_failure'):
        self.code = code


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise NativeFailure('invalid_request')
        value[key] = item
    return value


def valid_text(value, limit, *, empty=False):
    if (type(value) is not str or len(value) > limit
            or (not empty and not value) or '\x00' in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise NativeFailure('invalid_request')
    return value


def validate_request(value):
    if (type(value) is not dict or set(value) !=
            {'version', 'request_id', 'operation', 'text', 'text_sha256', 'language'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['request_id']) is not str
            or not re.fullmatch('[0-9a-f]{32}', value['request_id'])
            or value['operation'] not in ('languages', 'check')):
        raise NativeFailure('invalid_request')
    valid_text(value['text'], MAX_TEXT, empty=True)
    if hashlib.sha256(value['text'].encode('utf-8')).hexdigest() != value['text_sha256']:
        raise NativeFailure('invalid_request')
    if value['operation'] == 'languages':
        if value['text'] or value['language'] is not None:
            raise NativeFailure('invalid_request')
    elif (not value['text'].strip() or type(value['language']) is not str
            or len(value['language']) > 64 or not LANGUAGE.fullmatch(value['language'])):
        raise NativeFailure('invalid_request')
    return value


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


# SDK spellcheck.h: exact interface slots, fixed-width Windows HRESULT/ULONG.
CLSID_FACTORY = '7AB36653-1796-484B-BDFA-E74F1DB7C1DC'
IID_FACTORY = '8E018A9D-2415-4677-BF08-794EA61F94BB'


class WindowsSpeller:
    def __init__(self):
        if sys.platform != 'win32':
            raise NativeFailure('not_windows')
        self.ole = ctypes.WinDLL('ole32.dll', winmode=0x800)
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = ctypes.c_int32
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.ole.CoCreateInstance.restype = ctypes.c_int32
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.factory = ctypes.c_void_p()
        self.initialized = False
        result = self.ole.CoInitializeEx(None, 2)  # STA, private worker thread.
        self._ok(result)
        self.initialized = True
        try:
            self._ok(self.ole.CoCreateInstance(
                ctypes.byref(GUID.parse(CLSID_FACTORY)), None, 1,
                ctypes.byref(GUID.parse(IID_FACTORY)), ctypes.byref(self.factory)))
            if not self.factory.value:
                raise NativeFailure('unavailable')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _ok(result):
        if result < 0:
            raise NativeFailure('native_failure')
        return result

    @staticmethod
    def _method(pointer, slot, result_type, *argument_types):
        if not pointer or not pointer.value:
            raise NativeFailure()
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)(table[slot])

    def _call(self, pointer, slot, types, *arguments):
        return self._ok(self._method(pointer, slot, ctypes.c_int32, *types)(pointer, *arguments))

    def _release(self, pointer):
        if pointer is not None and pointer.value:
            self._method(pointer, 2, ctypes.c_uint32)(pointer)
            pointer.value = None

    def _string(self, pointer, limit):
        if not pointer.value:
            raise NativeFailure()
        try:
            chars = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_wchar))
            for length in range(limit + 1):
                if chars[length] == '\x00':
                    return valid_text(ctypes.wstring_at(pointer, length), limit, empty=True)
            raise NativeFailure('capacity')
        finally:
            self.ole.CoTaskMemFree(pointer)

    def _strings(self, enumerator, limit, width, *, truncate=False):
        values = []
        try:
            while True:
                value = ctypes.c_void_p()
                fetched = ctypes.c_uint32()
                status = self._call(enumerator, 3,
                    (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)),
                    1, ctypes.byref(value), ctypes.byref(fetched))
                if fetched.value == 0 and status == 1:
                    if value.value:
                        self.ole.CoTaskMemFree(value)
                        raise NativeFailure()
                    break
                if status != 0 or fetched.value != 1:
                    if value.value:
                        self.ole.CoTaskMemFree(value)
                    raise NativeFailure()
                item = self._string(value, width)
                if len(values) == limit:
                    if truncate:
                        return values, True
                    raise NativeFailure('capacity')
                values.append(item)
            return values, False
        finally:
            self._release(enumerator)

    def languages(self):
        enumerator = ctypes.c_void_p()
        self._call(self.factory, 3, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(enumerator))
        values, _ = self._strings(enumerator, MAX_LANGUAGES, 64)
        if any(not LANGUAGE.fullmatch(v) for v in values):
            raise NativeFailure()
        if len({v.casefold() for v in values}) != len(values):
            raise NativeFailure()
        return sorted(values, key=str.casefold)

    def check(self, text, language):
        supported = ctypes.c_int32()
        self._call(self.factory, 4, (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int32)),
                   language, ctypes.byref(supported))
        if supported.value == 0:
            raise NativeFailure('unsupported_language')
        checker, errors = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            self._call(self.factory, 5, (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)),
                       language, ctypes.byref(checker))
            selected = ctypes.c_void_p()
            self._call(checker, 3, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(selected))
            if self._string(selected, 64).casefold() != language.casefold():
                raise NativeFailure('unsupported_language')
            self._call(checker, 4, (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)),
                       text, ctypes.byref(errors))
            result = []
            while True:
                error = ctypes.c_void_p()
                status = self._call(errors, 3, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(error))
                if status == 1 and not error.value:
                    break
                if status != 0 or not error.value:
                    self._release(error)
                    raise NativeFailure()
                try:
                    if len(result) == MAX_ERRORS:
                        raise NativeFailure('capacity')
                    start, length, action = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_int32()
                    self._call(error, 3, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(start))
                    self._call(error, 4, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(length))
                    self._call(error, 5, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(action))
                    # Native offsets are UTF-16 code units, not Python offsets.
                    raw = text.encode('utf-16-le')
                    try:
                        word = raw[start.value * 2:(start.value + length.value) * 2].decode('utf-16-le')
                    except UnicodeError:
                        raise NativeFailure() from None
                    if not length.value or start.value + length.value > len(raw) // 2:
                        raise NativeFailure()
                    limited = False
                    if action.value == 1:
                        suggestions = ctypes.c_void_p()
                        self._call(checker, 5, (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)),
                                   word, ctypes.byref(suggestions))
                        replacements, limited = self._strings(suggestions, MAX_SUGGESTIONS,
                                                              MAX_REPLACEMENT, truncate=True)
                    elif action.value == 2:
                        replacement = ctypes.c_void_p()
                        self._call(error, 6, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(replacement))
                        replacements = [self._string(replacement, MAX_REPLACEMENT)]
                    elif action.value == 3:
                        replacements = ['']
                    elif action.value == 0:
                        replacements = []
                    else:
                        raise NativeFailure()
                    result.append({'start_utf16': start.value, 'length_utf16': length.value,
                                   'action': action.value, 'replacements': replacements,
                                   'suggestions_limited': limited})
                finally:
                    self._release(error)
            return result
        finally:
            self._release(errors)
            self._release(checker)

    def close(self):
        self._release(self.factory)
        if self.initialized:
            self.initialized = False
            self.ole.CoUninitialize()


def process_request(request, *, factory=WindowsSpeller):
    request = validate_request(request)
    reply = {key: request[key] for key in
             ('version', 'request_id', 'operation', 'text_sha256', 'language')}
    speller = None
    try:
        speller = factory()
        result = speller.languages() if request['operation'] == 'languages' else speller.check(
            request['text'], request['language'])
        reply.update(status='ok', result=result)
    except NativeFailure as exc:
        reply.update(status='error', error=exc.code)
    except Exception:
        reply.update(status='error', error='native_failure')
    finally:
        if speller is not None:
            speller.close()
    encoded = json.dumps(reply, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_REPLY_BYTES:
        for key in ('result', 'error'):
            reply.pop(key, None)
        reply.update(status='error', error='capacity')
    return reply


def main(argv):
    if len(argv) != 2 or not re.fullmatch('[0-9a-f]{64}', argv[1]):
        raise NativeFailure('invalid_request')
    with open(argv[0], 'rb') as source:
        raw = source.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or hashlib.sha256(raw).hexdigest() != argv[1]:
        raise NativeFailure('invalid_request')
    request = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(NativeFailure('invalid_request')))
    reply = process_request(request)
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception:
        # Never echo document text, filesystem paths, or provider exception text.
        sys.stderr.write('Windows spelling worker could not complete its bounded request.\n')
        raise SystemExit(2) from None
