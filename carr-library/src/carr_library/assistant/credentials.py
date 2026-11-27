"""Windows user Credential Manager. There is deliberately no file fallback."""
import ctypes
import os
import re
from uuid import uuid4


def validate_profile_id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid provider profile identity.')
    return value


def validate_reference(reference, profile_id):
    validate_profile_id(profile_id)
    if not isinstance(reference, str) or not re.fullmatch(
            r'carr/provider/' + re.escape(profile_id) + r'/[0-9a-f]{32}', reference):
        raise PermissionError('The credential reference belongs to another provider profile.')
    return reference


class CredentialUnavailable(RuntimeError):
    pass


class WindowsCredentialVault:
    """Opaque references only cross the vault boundary; values never enter plans."""
    @staticmethod
    def available():
        return os.name == 'nt'

    def _native(self):
        if os.name != 'nt':
            raise CredentialUnavailable('Windows Credential Manager is unavailable. Remote credentials cannot be stored on this host.')
        from ctypes import wintypes
        class Credential(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        library = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        pointer = ctypes.POINTER(Credential)
        library.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        library.CredWriteW.restype = wintypes.BOOL
        library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
        library.CredReadW.restype = wintypes.BOOL
        library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        library.CredDeleteW.restype = wintypes.BOOL
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None
        return library, Credential

    def put(self, profile_id, secret, *, reference=None):
        profile_id = validate_profile_id(profile_id)
        if not isinstance(secret, str) or not secret or any(c in secret for c in '\x00\r\n'):
            raise ValueError('Enter a nonempty credential without control characters.')
        raw = secret.encode('utf-8')
        if len(raw) > 2560:
            raise ValueError('The credential exceeds the Windows vault size limit.')
        reference = reference or ('carr/provider/' + profile_id + '/' + uuid4().hex)
        validate_reference(reference, profile_id)
        library, Credential = self._native()
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = Credential()
        credential.Type = 1  # CRED_TYPE_GENERIC, scoped to the current Windows user.
        credential.TargetName = reference
        credential.Comment = 'Universal Research Library provider credential'
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = buffer
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, current user's vault.
        credential.UserName = profile_id
        try:
            if not library.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialUnavailable('Windows could not save the credential (error %d).' % ctypes.get_last_error())
        finally:
            ctypes.memset(buffer, 0, len(raw))
        return reference

    def get(self, profile_id, reference):
        validate_reference(reference, profile_id)
        library, Credential = self._native()
        result = ctypes.POINTER(Credential)()
        if not library.CredReadW(reference, 1, 0, ctypes.byref(result)):
            raise CredentialUnavailable('Reconnect this provider profile in Windows Credential Manager.')
        try:
            credential = result.contents
            if credential.Type != 1 or credential.TargetName != reference or not 0 < credential.CredentialBlobSize <= 2560:
                raise CredentialUnavailable('The provider credential is invalid.')
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode('utf-8', errors='strict')
        finally:
            library.CredFree(result)

    def delete(self, profile_id, reference):
        validate_reference(reference, profile_id)
        library, _ = self._native()
        if not library.CredDeleteW(reference, 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialUnavailable('Windows could not remove the provider credential.')
