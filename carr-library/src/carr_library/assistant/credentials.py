"""Endpoint-bound Windows Credential Manager secrets, never plaintext files.

Only opaque references appear in application JSON. There is deliberately no
environment-variable, configuration-file, model-context or Linux plaintext
fallback. Tests may inject an explicit in-memory fake; production cannot.
"""
from __future__ import annotations

import ctypes as C
import json
import os
import re
from uuid import uuid4


_REFERENCE = re.compile(r'wincred:[0-9a-f]{32}\Z')
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
_PREFIX = 'UniversalResearchLibrary/providers/'
_MAX_BLOB = 2560


class CredentialUnavailable(RuntimeError):
    pass


class _FileTime(C.Structure):
    _fields_ = [('low', C.c_uint32), ('high', C.c_uint32)]


class _Credential(C.Structure):
    _fields_ = [('flags', C.c_uint32), ('type', C.c_uint32), ('target', C.c_wchar_p),
                ('comment', C.c_wchar_p), ('written', _FileTime),
                ('size', C.c_uint32), ('blob', C.POINTER(C.c_ubyte)),
                ('persist', C.c_uint32), ('attribute_count', C.c_uint32),
                ('attributes', C.c_void_p), ('alias', C.c_wchar_p), ('username', C.c_wchar_p)]


def validate_secret(secret):
    if (not isinstance(secret, str) or not 8 <= len(secret) <= 2048
            or any(ord(char) < 33 or ord(char) > 126 for char in secret)):
        raise ValueError('Enter an API key of 8 to 2048 printable ASCII characters without spaces.')
    return secret


def validate_profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid provider profile identity.')
    return value


def validate_reference(reference, profile_id=None):
    if profile_id is not None:
        validate_profile_id(profile_id)
        if not isinstance(reference, str) or not re.fullmatch(
                r'carr/provider/' + re.escape(profile_id) + r'/[0-9a-f]{32}', reference):
            raise PermissionError('The credential reference belongs to another provider profile.')
    elif not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ValueError('Invalid credential reference; reconnect this profile.')
    return reference


def _legacy_secret(secret):
    if (not isinstance(secret, str) or not secret
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)):
        raise ValueError('Enter a nonempty credential without control characters.')
    try:
        raw = secret.encode('utf-8', errors='strict')
    except UnicodeError:
        raise ValueError('Enter a valid UTF-8 credential.') from None
    if len(raw) > _MAX_BLOB:
        raise ValueError('The credential exceeds the Windows vault size limit.')
    return raw


class _AvailabilityValue(int):
    def __new__(cls, available):
        return int.__new__(cls, bool(available))

    def __call__(self):
        return bool(self)


class _Availability:
    def __get__(self, instance, owner):
        return _AvailabilityValue(os.name == 'nt' if instance is None else instance._api is not None)


def _binding(profile_id, endpoint):
    if not isinstance(profile_id, str) or not _PROFILE.fullmatch(profile_id):
        raise ValueError('Invalid provider profile identity.')
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 512 or any(ord(ch) < 33 for ch in endpoint):
        raise ValueError('Invalid credential destination binding.')


class WindowsCredentialVault:
    """Store user API keys in the current Windows account's native vault."""
    def __init__(self):
        self._api = None
        if os.name == 'nt':
            self._api = C.WinDLL('advapi32.dll', use_last_error=True, winmode=0x800)
            self._api.CredWriteW.argtypes = (C.POINTER(_Credential), C.c_uint32)
            self._api.CredWriteW.restype = C.c_int
            self._api.CredReadW.argtypes = (C.c_wchar_p, C.c_uint32, C.c_uint32,
                                           C.POINTER(C.POINTER(_Credential)))
            self._api.CredReadW.restype = C.c_int
            self._api.CredDeleteW.argtypes = (C.c_wchar_p, C.c_uint32, C.c_uint32)
            self._api.CredDeleteW.restype = C.c_int
            self._api.CredFree.argtypes = (C.c_void_p,)
            self._api.CredFree.restype = None

    # Both published callers are supported: vault.available and vault.available().
    available = _Availability()

    def _require(self):
        if self._api is None:
            raise CredentialUnavailable('Windows Credential Manager is unavailable; no plaintext fallback is used.')

    def store(self, profile_id, endpoint, secret):
        self._require()
        _binding(profile_id, endpoint)
        validate_secret(secret)
        reference = 'wincred:' + uuid4().hex
        raw = json.dumps({'version': 1, 'profile_id': profile_id, 'endpoint': endpoint,
                          'secret': secret}, separators=(',', ':')).encode('utf-8')
        if len(raw) > _MAX_BLOB:
            raise ValueError('The credential is too large for the native vault.')
        buffer = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
        record = _Credential(type=1, target=_PREFIX + reference[8:],
                             comment='Endpoint-bound research library API profile',
                             size=len(raw), blob=buffer, persist=2, username='profile:' + profile_id)
        try:
            if not self._api.CredWriteW(C.byref(record), 0):
                raise CredentialUnavailable('The native credential vault could not save this key.')
        finally:
            C.memset(buffer, 0, len(buffer))
        return reference

    def resolve(self, reference, profile_id, endpoint):
        self._require()
        validate_reference(reference)
        _binding(profile_id, endpoint)
        pointer = C.POINTER(_Credential)()
        if not self._api.CredReadW(_PREFIX + reference[8:], 1, 0, C.byref(pointer)):
            raise CredentialUnavailable('The API key is missing or unavailable; reconnect the profile.')
        try:
            record = pointer.contents
            if record.type != 1 or not 0 < record.size <= _MAX_BLOB or not record.blob:
                raise CredentialUnavailable('The credential vault returned an invalid record.')
            raw = C.string_at(record.blob, record.size)
            try:
                value = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError):
                raise CredentialUnavailable('The credential vault record is invalid.') from None
            if (type(value) is not dict or set(value) != {'version', 'profile_id', 'endpoint', 'secret'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['profile_id'] != profile_id or value['endpoint'] != endpoint):
                raise CredentialUnavailable('This credential belongs to a different profile or destination.')
            try:
                return validate_secret(value['secret'])
            except ValueError:
                raise CredentialUnavailable('The stored API key is invalid; reconnect the profile.') from None
        finally:
            if pointer:
                if pointer.contents.blob and 0 < pointer.contents.size <= _MAX_BLOB:
                    C.memset(pointer.contents.blob, 0, pointer.contents.size)
                self._api.CredFree(pointer)

    def put(self, profile_id, secret, *, reference=None):
        profile_id = validate_profile_id(profile_id)
        raw = _legacy_secret(secret)
        reference = reference or ('carr/provider/' + profile_id + '/' + uuid4().hex)
        validate_reference(reference, profile_id)
        self._require()
        buffer = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
        record = _Credential(type=1, target=reference,
            comment='Research library provider credential', size=len(raw),
            blob=buffer, persist=2, username=profile_id)
        try:
            if not self._api.CredWriteW(C.byref(record), 0):
                raise CredentialUnavailable('The native credential vault could not save this key.')
        finally:
            C.memset(buffer, 0, len(buffer))
        return reference

    def get(self, profile_id, reference):
        validate_reference(reference, profile_id)
        self._require()
        pointer = C.POINTER(_Credential)()
        if not self._api.CredReadW(reference, 1, 0, C.byref(pointer)):
            raise CredentialUnavailable('Reconnect this provider profile in Windows Credential Manager.')
        try:
            record = pointer.contents
            if (record.type != 1 or record.target != reference or record.username != profile_id
                    or not 0 < record.size <= _MAX_BLOB or not record.blob):
                raise CredentialUnavailable('The provider credential is invalid.')
            try:
                secret = C.string_at(record.blob, record.size).decode('utf-8', errors='strict')
                _legacy_secret(secret)
                return secret
            except (UnicodeError, ValueError):
                raise CredentialUnavailable('The stored provider credential is invalid; reconnect the profile.') from None
        finally:
            if pointer:
                if pointer.contents.blob and 0 < pointer.contents.size <= _MAX_BLOB:
                    C.memset(pointer.contents.blob, 0, pointer.contents.size)
                self._api.CredFree(pointer)

    def delete(self, reference=None, legacy_reference=None, *, profile_id=None):
        # Never guess or migrate a reference namespace during secret lookup.
        if legacy_reference is not None:
            if profile_id is not None:
                raise ValueError('Choose one provider profile identity.')
            profile_id, reference = reference, legacy_reference
        if profile_id is not None:
            validate_reference(reference, profile_id)
            target = reference
        else:
            validate_reference(reference)
            target = _PREFIX + reference[8:]
        self._require()
        if not self._api.CredDeleteW(target, 1, 0):
            if C.get_last_error() != 1168:
                raise CredentialUnavailable('The native credential vault could not remove this key.')
