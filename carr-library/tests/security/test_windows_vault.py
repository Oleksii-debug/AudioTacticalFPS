"""Required native Windows gate; portable jobs name their own suites explicitly."""
from hashlib import sha256
import os
import uuid

from carr_library.security.anchors import Anchor, WindowsVaultAnchor


def test_real_windows_vault_anchor_roundtrip():
    assert os.name == 'nt', 'This required native vault gate must execute on Windows.'
    from carr_library.assistant.credentials import WindowsCredentialVault
    vault = WindowsCredentialVault()
    reference = 'carr/provider/trust-test/' + uuid.uuid4().hex
    binding = sha256(b'synthetic-trust-test').hexdigest()
    anchor = WindowsVaultAnchor(reference, binding, product='carr-test', profile_id='trust-test', vault=vault)
    try:
        vault.put(anchor.profile_id, anchor.encode(Anchor()), reference=anchor.reference)
        anchor.compare_and_set(Anchor(), Anchor(1, 'a' * 64, 1))
        assert anchor.load() == Anchor(1, 'a' * 64, 1)
    finally:
        vault.delete(anchor.profile_id, anchor.reference)
