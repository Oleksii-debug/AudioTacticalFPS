"""Windows native vault CRUD with a synthetic key; no service or paid calls.

Run installed package: python -I -B -X utf8 tests/assistant/native_credentials_probe.py
"""
import json
import os
from uuid import uuid4


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Windows Credential Manager is required.'}))
        return 77
    from carr_library.assistant.credentials import CredentialUnavailable, WindowsCredentialVault
    vault = WindowsCredentialVault()
    profile_id = 'native-probe-' + uuid4().hex
    endpoint = 'https://example.invalid/v1'
    secret = 'synthetic-not-an-api-key-' + uuid4().hex
    reference = None
    checks = []
    try:
        assert vault.available
        reference = vault.store(profile_id, endpoint, secret)
        assert vault.resolve(reference, profile_id, endpoint) == secret
        checks.append('real native credential write and read')
        for identity, destination in ((profile_id, 'https://other.invalid/v1'), ('other-profile', endpoint)):
            try:
                vault.resolve(reference, identity, destination)
                raise AssertionError('Credential destination binding was not enforced.')
            except CredentialUnavailable:
                pass
        checks.append('different endpoint and account cannot resolve the key')
        vault.delete(reference)
        try:
            vault.resolve(reference, profile_id, endpoint)
            raise AssertionError('Deleted credential was still available.')
        except CredentialUnavailable:
            pass
        checks.append('real native credential deletion')
    finally:
        if reference is not None:
            vault.delete(reference)
    print(json.dumps({'status': 'PASS', 'checks': checks,
                      'external_requests': 0, 'real_api_credentials_used': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
