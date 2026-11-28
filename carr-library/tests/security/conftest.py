"""Synthetic keys and tiny bundles; no publisher authority is represented."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp

from carr_library.delivery_contract import CONTRACT_VERSION, Capability, IntegrationContext, ProviderCapabilities
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.anchors import Anchor
from carr_library.storage.revisions import SCHEMAS
from carr_library.security.entitlements import CONTEXT, PublicKey, TrustPolicy
from carr_library.security.updates import UpdatePolicy


REFERENCE = 'carr/provider/trust-test/' + '0' * 32


class RenewalProvider:
    def describe(self):
        return ProviderCapabilities(CONTRACT_VERSION, frozenset({Capability.SIGNED_RENEWAL}))


@pytest.fixture
def integration_context():
    return IntegrationContext('carr-test', 'build-A', 'stable')


class MemoryAnchor:
    """Explicitly synthetic, not a production plaintext fallback."""
    def __init__(self, initial=None):
        self.value = initial or Anchor()
        self.fail = False

    def load(self):
        return self.value

    def compare_and_set(self, expected, replacement, *, guard=lambda: None):
        if self.fail:
            raise TrustError('Synthetic protected-store failure.')
        if self.value != expected:
            raise TrustError('Protected state changed.')
        assert replacement.sequence >= expected.sequence
        assert replacement.max_utc >= expected.max_utc
        guard()
        self.value = replacement


class Clock:
    def __init__(self):
        self.now = int(datetime.now(timezone.utc).timestamp())

    def __call__(self):
        return self.now


@pytest.fixture
def signer():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def policy(signer):
    return TrustPolicy('carr-test', 'synthetic-subject', 'synthetic-device',
                       (PublicKey(signer.public_key().public_bytes_raw()),),
                       frozenset({'policy-1'}), frozenset({'render', 'compose'}))


def grant(clock, **changes):
    value = dict(schema=1, product='carr-test', entitlement_id='grant-one', subject='synthetic-subject',
                 device='synthetic-device', sequence=1, profile='renewable', issued_at=clock.now,
                 not_before=clock.now, expires_at=clock.now + 30 * 86400,
                 offline_until=clock.now + 60 * 86400, refresh_interval=7 * 86400,
                 policy_version='policy-1', terms_sha256='a' * 64, features=['render'], revoked=False,
                 request_nonce=None)
    value.update(changes)
    return value


def envelope(signer, payload):
    import base64
    raw = json_bytes(payload) if isinstance(payload, dict) else payload
    return json_bytes(dict(version=1, kind='carr-entitlement',
        key_id=sha256(signer.public_key().public_bytes_raw()).hexdigest(),
        payload=base64.b64encode(raw).decode('ascii'),
        signature=base64.b64encode(signer.sign(CONTEXT + raw)).decode('ascii')))


def accept(store, raw):
    return store.accept(raw, reviewed_digest=sha256(raw).hexdigest(),
                        accepted_terms=store.preview(raw)['terms_sha256'])


class Repository:
    def __init__(self, root):
        self.root = root
        (root / 'metadata').mkdir(parents=True)
        (root / 'targets').mkdir()
        self.signers = {role: CryptoSigner.generate_ed25519()
                        for role in ('root', 'targets', 'snapshot', 'timestamp')}
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        self.md = {
            'root': Metadata(Root(expires=expires, consistent_snapshot=True)),
            'targets': Metadata(Targets(expires=expires)),
            'snapshot': Metadata(Snapshot(expires=expires)),
            'timestamp': Metadata(Timestamp(expires=expires)),
        }
        for role, signer in self.signers.items():
            self.md['root'].signed.add_key(signer.public_key, role)
        self.bootstrap = self.publish_root()
        self.target_path = 'release.zip'

    def encode(self, role):
        md = self.md[role]
        md.signatures.clear()
        md.sign(self.signers[role])
        return md.to_bytes()

    def publish_root(self, previous_signer=None):
        md = self.md['root']
        md.signatures.clear()
        md.sign(self.signers['root'])
        if previous_signer:
            md.sign(previous_signer, append=True)
        raw = md.to_bytes()
        (self.root / 'metadata' / f'{md.signed.version}.root.json').write_bytes(raw)
        return raw

    def publish(self, package, *, build=2, custom=None):
        metadata = dict(schema=1, kind='carr-application', product='carr-test', channel='stable',
                        version=f'0.{build}.0', build=build, source_commit=str(build % 10) * 40,
                        data_schema=max(SCHEMAS), notes='Synthetic signed fixture; no production publisher.')
        if custom:
            metadata.update(custom)
        target = TargetFile.from_data(self.target_path, package)
        target.unrecognized_fields['custom'] = metadata
        self.md['targets'].signed.targets = {self.target_path: target}
        (self.root / 'targets' / (target.hashes['sha256'] + '.' + self.target_path)).write_bytes(package)
        self.flush()
        return target

    def flush(self):
        targets = self.encode('targets')
        (self.root / 'metadata' / f'{self.md["targets"].signed.version}.targets.json').write_bytes(targets)
        self.md['snapshot'].signed.meta = {'targets.json': MetaFile.from_data(
            self.md['targets'].signed.version, targets, ['sha256'])}
        snapshot = self.encode('snapshot')
        (self.root / 'metadata' / f'{self.md["snapshot"].signed.version}.snapshot.json').write_bytes(snapshot)
        self.md['timestamp'].signed.snapshot_meta = MetaFile.from_data(
            self.md['snapshot'].signed.version, snapshot, ['sha256'])
        (self.root / 'metadata' / 'timestamp.json').write_bytes(self.encode('timestamp'))

    def advance(self):
        for role in ('targets', 'snapshot', 'timestamp'):
            self.md[role].signed.version += 1

    def policy(self, **changes):
        values = dict(product='carr-test', channel='stable', bootstrap=self.bootstrap,
                      bootstrap_sha256=sha256(self.bootstrap).hexdigest(), current_build=1,
                      max_package_bytes=1024**2, max_unpacked_bytes=2 * 1024**2, max_files=100)
        values.update(changes)
        return UpdatePolicy(**values)


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / 'repository')


def bundle(version='B'):
    """Exercise the real installer manifest checker with a kilobyte fixture."""
    files = {
        'install.lock': b'# synthetic fixture only\n',
        'install_windows.py': b'# synthetic fixture; not executed by these tests\n',
        'Install.cmd': b'@exit /b 0\n',
        'ResearchLibrary.cmd': b'@exit /b 0\n',
        'CheckInstallation.cmd': b'@exit /b 0\n',
        'README.txt': b'Synthetic update integration test.\n',
        'wheelhouse/synthetic.whl': b'not a distribution; test adapter only',
        'program.py': f'print({version!r})\n'.encode(),
        'version.txt': version.encode(),
    }
    manifest = json_bytes({'schema': 1, 'files': {name: sha256(data).hexdigest() for name, data in files.items()}})
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('bundle-manifest.json', manifest)
    return result.getvalue()


def installer_verify():
    path = Path(__file__).resolve().parents[2] / 'packaging' / 'install_windows.py'
    spec = importlib.util.spec_from_file_location('trusted_fixture_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify


class SyntheticBundleAdapter:
    """Real verifier, real subprocess health, deliberately no Windows install claim."""
    def __init__(self, *, fail=False):
        self.verify_bundle = installer_verify()
        self.fail = fail
        self.prepared = 0
        self.checked = 0
        self.activated_path = None

    def verify(self, root):
        self.verify_bundle(root)

    def prepare(self, root, release):
        self.verify(root)
        self.prepared += 1

    def activate(self, root, release):
        self.activated_path = root

    def health(self, root, release):
        import subprocess
        self.checked += 1
        self.verify(root)
        answer = subprocess.check_output([sys.executable, '-I', '-B', str(root / 'program.py')], timeout=10)
        return not self.fail and answer.strip() == b'B'


@contextmanager
def idle():
    yield
