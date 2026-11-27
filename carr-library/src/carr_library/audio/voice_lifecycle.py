"""Reviewed local export and removal through the existing owner voice API.

Only reference-package data is exported. Revocation remains in the existing
reviewed installation manifest, including after data deletion, so an old legacy
catalog entry cannot silently become a replacement voice.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .cleanup import retained_context, retained_temporary_directory
from .package_publication import capture_package, publish_package
from .voice_manifest import ModelManifestError, digest_file, local_path, read_json
from .voice_packages import (ID, MAX_PACKAGE_BYTES, MAX_PROFILES, VoicePackageManager,
                             VoicePackageReview, _cancel, _document, _hash, _index,
                             _json, _query, runtime_settings)


@dataclass(frozen=True)
class VoiceRemovalReview:
    profile: str
    version: str
    active: bool
    delete_files: bool
    manifest_sha256: str
    settings_revision: int
    index_sha256: str
    files_json: str

    @property
    def files(self):
        import json
        return json.loads(self.files_json)

    @property
    def confirmation(self):
        return ('DELETE ' if self.delete_files else 'REVOKE ') + self.profile

    def summary(self):
        files = self.files
        versions = sorted({name.split('/')[0] for name in files})
        action = ('Delete all listed installed versions and reference files permanently.'
                  if self.delete_files else
                  'Deactivate this profile and retain all its installed versions and reference files.')
        return (
            f'Profile: {self.profile}; selected version: {self.version}; '
            f'current state: {"active" if self.active else "inactive"}.\n'
            + action + '\n'
            + f'Installed versions: {", ".join(versions) or "no remaining files"}.\n'
            + f'Exact reviewed inventory: {len(files)} files; '
            + str(sum(row['size_bytes'] for row in files.values())) + ' bytes.\n'
            + 'Only this profile is selected. Shared engine/model files and other profiles remain outside this action. '
            + 'This local installation may also be used by other applications; close their voice sessions before removal.\n'
            + 'Production permission is revoked. Reuse requires a fresh explicit rights and language review; '
            + 'an old legacy entry cannot replace the revoked identity.\n'
            + 'Type exactly ' + self.confirmation + ' to apply this reviewed action.')



PROTECTED_SUFFIX = '.carr-voice.enc'


@dataclass(frozen=True)
class ProtectedVoicePackageReview:
    """Passive identities only; neither a password nor decrypted bytes survive review."""
    library_root: str
    library_identity: tuple
    manifest_sha256: str
    settings_revision: int
    source: str
    source_sha256: str
    size_bytes: int
    plaintext_sha256: str
    plaintext_size: int
    document_json: str

    @property
    def operation(self):
        return 'protected-install'

    @property
    def document(self):
        import json
        return json.loads(self.document_json)

    def summary(self):
        plain = VoicePackageReview('install', self.manifest_sha256,
            self.settings_revision, self.source, self.plaintext_sha256, self.document_json)
        return (plain.summary()
            + '\nProtected package SHA-256: ' + self.source_sha256
            + '\nThe complete encrypted package was authenticated and its exact data inspected. '
            'No profile has been installed. Temporary plaintext was removed after review. '
            'Apply requires the password again and a fresh rights/language decision; '
            'the same package and registration will be checked again.')


class VoiceProfileLifecycle:
    """Compose current registration, broker, owner API and publication services."""

    def __init__(self, root, *, manager=None, broker=None):
        self.manager = manager if manager is not None else VoicePackageManager(root)
        self.root = self.manager.store.projects.root
        self._broker = broker

    @property
    def broker(self):
        if self._broker is None:
            from ..operations import ResourceBroker
            self._broker = ResourceBroker.for_app(self.root)
        return self._broker

    @staticmethod
    def _profile(profile):
        if not isinstance(profile, str) or not ID.fullmatch(profile):
            raise ValueError('Select a valid installed profile identity.')
        return profile

    @staticmethod
    def _prefix(manifest):
        return runtime_settings(manifest.settings)['references'] + '/packages'


    def _library_binding(self):
        path = local_path(self.root)
        info = path.stat()
        if not path.is_dir():
            raise ValueError('The selected research library is unavailable.')
        return str(path), (info.st_dev, info.st_ino)

    @contextmanager
    def _opened_protected(self, package, passphrase, *, expected=None,
                          cancelled=lambda: False):
        """One measured admission owns authenticated staging and every owner child."""
        from ..operations import ResourceRequest
        from ..sync import crypto
        _cancel(cancelled)
        previous, manifest = self.manager._current(cancelled=cancelled)
        binding = self._library_binding()
        if expected is not None and (expected.library_root, expected.library_identity) != binding:
            raise ValueError('The protected review belongs to another library identity.')
        # The existing optional implementation enforces its exact dependency and
        # secret limits. No package reader receives ciphertext or a passphrase.
        crypto._password(passphrase)
        crypto._load_crypto()
        package = local_path(package)
        if not package.name.lower().endswith(PROTECTED_SUFFIX):
            raise ValueError('Select an explicit .carr-voice.enc protected data package.')
        size = package.stat().st_size
        if not package.is_file() or not 0 < size <= MAX_PACKAGE_BYTES:
            raise ModelManifestError('Choose a bounded regular protected voice package.')
        parent = local_path(self.root.parent)
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
            disk_path=parent, disk_bytes=2 * size + 4 * 1024**2,
            devices=('voice-maintenance:' + manifest.manifest_sha256,))
        with retained_context(self.broker.reserve(request, timeout=60, cancelled=cancelled,
                operation_id='voice-protected-import:' + uuid4().hex)) as lease:
            def stop():
                if cancelled():
                    return True
                if self._library_binding() != binding:
                    raise ValueError('The selected research library changed during the operation.')
                lease.checkpoint()
                return False
            _cancel(stop)
            current, current_manifest = self.manager._current(cancelled=stop)
            if current != previous or current_manifest.manifest_sha256 != manifest.manifest_sha256:
                raise ValueError('Voice registration changed while waiting for protected-package admission.')
            captured = capture_package(package, maximum_bytes=MAX_PACKAGE_BYTES, cancelled=stop)
            if captured.size_bytes != size:
                raise ValueError('The protected package changed while waiting for admission.')
            if expected is not None and (
                    str(package) != expected.source
                    or captured.sha256 != expected.source_sha256
                    or captured.size_bytes != expected.size_bytes
                    or previous['revision'] != expected.settings_revision
                    or manifest.manifest_sha256 != expected.manifest_sha256):
                raise ValueError('The protected package or registration changed after review.')
            with retained_temporary_directory(prefix='.voice-protected-import-', dir=parent) as staging:
                plain = Path(staging) / 'profile.dordiai-voice'
                receipt = crypto.decrypt_file(package, plain, passphrase, cancelled=stop)
                if (receipt.encrypted or receipt.size != receipt.plaintext_size
                        or not 0 < receipt.size <= MAX_PACKAGE_BYTES):
                    raise ModelManifestError('The authenticated voice package exceeds its data allowance.')
                if capture_package(package, maximum_bytes=MAX_PACKAGE_BYTES, cancelled=stop) != captured:
                    raise ValueError('The encrypted package changed during authentication.')
                plaintext = capture_package(plain, maximum_bytes=MAX_PACKAGE_BYTES, cancelled=stop)
                if (plaintext.sha256, plaintext.size_bytes) != (receipt.sha256, receipt.size):
                    raise ModelManifestError('The authenticated private package changed before inspection.')
                inspected = self.manager.inspect(plain, cancelled=stop)
                if (inspected.manifest_sha256 != manifest.manifest_sha256
                        or inspected.settings_revision != previous['revision']
                        or inspected.source_sha256 != plaintext.sha256
                        or capture_package(plain, maximum_bytes=MAX_PACKAGE_BYTES, cancelled=stop) != plaintext
                        or self.manager.store.read() != previous):
                    raise ValueError('The authenticated package or registration changed during inspection.')
                if expected is not None and (
                        plaintext.sha256 != expected.plaintext_sha256
                        or plaintext.size_bytes != expected.plaintext_size
                        or inspected.document_json != expected.document_json):
                    raise ValueError('The protected package differs from its exact reviewed contents.')
                _cancel(stop)
                yield inspected, captured, plaintext, binding, stop

    def inspect_protected(self, package, *, passphrase, cancelled=lambda: False):
        """Authenticate and inspect, then discard plaintext before returning a review."""
        with self._opened_protected(package, passphrase, cancelled=cancelled) as opened:
            inspected, captured, plain, binding, _ = opened
            result = ProtectedVoicePackageReview(*binding, inspected.manifest_sha256,
                inspected.settings_revision, str(captured.path), captured.sha256,
                captured.size_bytes, plain.sha256, plain.size_bytes, inspected.document_json)
        return result

    def apply_protected(self, review, *, passphrase, rights_approved,
                        supported_languages, cancelled=lambda: False):
        """Freshly decrypt/re-inspect the same review before the existing apply boundary."""
        if type(review) is not ProtectedVoicePackageReview:
            raise ValueError('Inspect the exact protected package before applying it.')
        if rights_approved is not True:
            raise ValueError('Explicitly confirm permission to use the reviewed voice references.')
        # Language admission and publication remain owned by manager.apply.
        applied = None
        try:
            with self._opened_protected(review.source, passphrase, expected=review,
                    cancelled=cancelled) as opened:
                inspected, _, _, _, stop = opened
                applied = self.manager.apply(inspected, rights_approved=rights_approved,
                    supported_languages=supported_languages, cancelled=stop)
        except Exception:
            if applied is None:
                raise
            applied = {**applied, 'cleanup_warning':
                'The reviewed profile was applied. Private staging or admission cleanup needs attention; '
                'do not repeat the installation. Revalidate the existing registration before further use.'}
        return applied

    def review_removal(self, profile, *, delete_files=False, cancelled=lambda: False):
        profile = self._profile(profile)
        if type(delete_files) is not bool:
            raise ValueError('Choose deactivation or deletion explicitly.')
        value, manifest = self.manager._current(cancelled=cancelled)
        prefix = self._prefix(manifest)
        root = manifest.root / prefix
        self.manager._writable_package_tree(root, cancelled)
        index = root / 'voice-packages.json'
        active, inactive = _index(read_json(index))
        version = active.get(profile, inactive.get(profile))
        if version is None:
            raise ValueError('The selected profile is not registered in this installation.')
        selected = prefix + '/' + profile + '/'
        files = {}
        for name, checksum in manifest.files:
            _cancel(cancelled)
            if name.startswith(selected):
                path = local_path(manifest.root / name)
                info = path.stat()
                if not path.is_file() or info.st_nlink != 1:
                    raise ModelManifestError('Reviewed reference data must be regular, unlinked files.')
                files[name[len(selected):]] = {'sha256': checksum, 'size_bytes': info.st_size}
        if not files and not (delete_files and profile in inactive):
            raise ModelManifestError('The selected profile has no verified reference data.')
        revoked = set(manifest.settings.get('revoked_profiles', [])) | {profile}
        if len(revoked) > MAX_PROFILES:
            raise ModelManifestError('The installation has reached its explicit revocation inventory budget.')
        checksum = digest_file(index, cancelled=cancelled)
        # A different complete index between the metadata read and its hash
        # must never become an approved deletion precondition.
        if _index(read_json(index)) != (active, inactive):
            raise ValueError('Profile activation changed during review.')
        _cancel(cancelled)
        return VoiceRemovalReview(profile, version, profile in active, delete_files,
                                  manifest.manifest_sha256, value['revision'],
                                  checksum, _json(files))

    def remove(self, review, *, confirmation, cancelled=lambda: False):
        if type(review) is not VoiceRemovalReview:
            raise ValueError('Review the exact profile and removal mode first.')
        if confirmation != review.confirmation:
            raise ValueError('Type the exact reviewed removal confirmation.')
        _cancel(cancelled)
        from ..operations import ResourceRequest
        # Existing cooperative CPU consumers settle before shared reference
        # data is changed. This does not claim authority over other applications.
        request = ResourceRequest(cpu_slots=self.broker.limits.cpu_slots,
            memory_bytes=256 * 1024**2,
            devices=('voice-maintenance:' + review.manifest_sha256,))
        with retained_context(self.broker.reserve(request, timeout=60, cancelled=cancelled,
                operation_id='voice-remove:' + uuid4().hex)) as lease:
            def stop():
                if cancelled():
                    return True
                lease.checkpoint()
                return False
            current = self.review_removal(review.profile, delete_files=review.delete_files, cancelled=stop)
            if current != review:
                raise ValueError('The exact profile changed after removal review; review it again.')
            previous, manifest = self.manager._current(cancelled=stop)
            if (previous['revision'] != review.settings_revision
                    or manifest.manifest_sha256 != review.manifest_sha256):
                raise ValueError('Voice registration changed after removal review.')
            prefix = self._prefix(manifest)
            index_path = manifest.root / prefix / 'voice-packages.json'
            old_active, old_inactive = _index(read_json(index_path))
            result = _query(manifest, 'remove', profile=review.profile,
                delete_files=review.delete_files, expected_index_sha256=review.index_sha256,
                expected_files=review.files, cancelled=stop, timeout=600)
            if result != {'profile': review.profile, 'deleted': review.delete_files}:
                raise ModelManifestError('The owner returned a different profile removal result.')
            active, inactive = _index(read_json(index_path))
            expected_active = {key: value for key, value in old_active.items() if key != review.profile}
            expected_inactive = {key: value for key, value in old_inactive.items() if key != review.profile}
            if not review.delete_files:
                expected_inactive[review.profile] = review.version
            if (active, inactive) != (expected_active, expected_inactive):
                raise ModelManifestError('The owner changed another profile activation during removal.')
            index_hash = digest_file(index_path, cancelled=stop)
            files = self._removed_inventory(manifest, prefix, review, index_hash, stop)
            settings = dict(manifest.settings)
            settings['managed_profiles'] = True
            settings['profile_languages'] = {key: value for key, value in
                settings.get('profile_languages', {}).items() if key != review.profile}
            settings['profile_reviews'] = {key: value for key, value in
                settings.get('profile_reviews', {}).items() if key != review.profile}
            settings['revoked_profiles'] = sorted(set(settings.get('revoked_profiles', [])) | {review.profile})
            return self.manager._publish(previous, manifest, settings, files, stop)

    @staticmethod
    def _removed_inventory(manifest, prefix, review, index_hash, cancelled):
        old = dict(manifest.files)
        selected = prefix + '/' + review.profile + '/'
        mutable = {prefix + '/voice-packages.json', prefix + '/.voice-packages.lock'}
        removed = {name for name in old if review.delete_files and name.startswith(selected)}
        result = {}
        for count, path in enumerate(manifest.root.rglob('*'), 1):
            _cancel(cancelled)
            if count > 32768:
                raise ModelManifestError('The local installation exceeds its directory entry budget.')
            path = local_path(path)
            if not path.is_file() or path == manifest.path:
                continue
            name = path.relative_to(manifest.root).as_posix()
            checksum = digest_file(path, cancelled=cancelled)
            if name in removed:
                raise ModelManifestError('The selected reference files were not completely deleted.')
            if name not in mutable:
                if name not in old or checksum != old[name]:
                    raise ModelManifestError('An unselected runtime or reference file changed during removal.')
            elif name.endswith('/voice-packages.json'):
                if checksum != index_hash:
                    raise ModelManifestError('The activation index changed during removal verification.')
            elif path.read_bytes() not in (b'', b'0'):
                raise ModelManifestError('Unexpected owner voice lock contents.')
            result[name] = checksum
        if set(old) - removed - set(result) or prefix + '/voice-packages.json' not in result:
            raise ModelManifestError('An unselected reviewed file disappeared during profile removal.')
        return result

    def export(self, review, destination, *, export_rights_approved=False, cancelled=lambda: False):
        return self._export(review, destination, export_rights_approved=export_rights_approved,
                            cancelled=cancelled)

    def export_protected(self, review, destination, *, passphrase,
                         export_rights_approved=False, cancelled=lambda: False):
        return self._export(review, destination, export_rights_approved=export_rights_approved,
                            protected=True, passphrase=passphrase, cancelled=cancelled)

    def _export(self, review, destination, *, export_rights_approved=False, protected=False,
                passphrase=None, cancelled=lambda: False):
        if type(review) is not VoicePackageReview or review.operation != 'activate':
            raise ValueError('Review the exact installed version before exporting it.')
        if export_rights_approved is not True:
            raise ValueError('Explicitly confirm permission to export these exact reference files.')
        document = _document(review.document)
        destination = local_path(destination, must_exist=False)
        if destination.exists():
            raise FileExistsError('Choose a new private data package; existing files are never overwritten.')
        parent = local_path(destination.parent)
        if protected:
            if not parent.is_dir() or not destination.name.lower().endswith(PROTECTED_SUFFIX):
                raise ValueError('Choose a new local .carr-voice.enc file in an existing directory.')
            from ..sync import crypto
            crypto._password(passphrase)
            crypto._load_crypto()
        elif not parent.is_dir() or destination.suffix.lower() not in {'.zip', '.dordiai-voice'}:
            raise ValueError('Choose a new local .zip or .dordiai-voice file in an existing directory.')
        previous, manifest = self.manager._current(cancelled=cancelled)
        if (previous['revision'] != review.settings_revision
                or manifest.manifest_sha256 != review.manifest_sha256):
            raise ValueError('Voice registration changed after export review.')
        if destination.is_relative_to(manifest.root) or destination.is_relative_to(self.root.resolve()):
            raise ValueError('Export to a private directory outside the installation and research library.')
        from ..studio.publishing import capture_publication_destination
        approval = capture_publication_destination(destination, private_root=self.root, cancelled=cancelled)
        parent_identity = approval.parent_identity
        total = sum(row['size_bytes'] for row in document['files'].values())
        allowance = total + total // 100 + len(document['files']) * 4096 + 4 * 1024**2
        if allowance > MAX_PACKAGE_BYTES:
            raise ModelManifestError('This profile exceeds the bounded local export allowance.')
        cipher_allowance = crypto._serialized_size(allowance) if protected else 0
        if cipher_allowance > MAX_PACKAGE_BYTES:
            raise ModelManifestError('The protected package exceeds its bounded local export allowance.')
        from ..operations import ResourceRequest
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
            disk_path=parent, disk_bytes=allowance + cipher_allowance,
            devices=('voice-maintenance:' + review.manifest_sha256,))
        published = None
        try:
            with retained_context(self.broker.reserve(request, timeout=60, cancelled=cancelled,
                    operation_id='voice-export:' + uuid4().hex)) as lease:
                def stop():
                    if cancelled():
                        return True
                    lease.checkpoint()
                    return False
                current = self.manager.review_version(document['id'], document['version'], cancelled=stop)
                if current != review:
                    raise ValueError('The exact installed version changed after export review.')
                self.manager._writable_package_tree(manifest.root / self._prefix(manifest), stop)
                with retained_temporary_directory(prefix='.voice-export-', dir=parent) as staging:
                    staged = Path(staging) / 'profile.dordiai-voice'
                    result = _document(_query(manifest, 'export', profile=document['id'],
                        version=document['version'], expected_document_sha256=_hash(document),
                        output=str(staged), cancelled=stop, timeout=600))
                    if result != document or not staged.is_file() or staged.stat().st_size > allowance:
                        raise ModelManifestError('The exported package differs from the reviewed bounded profile.')
                    captured = capture_package(staged, maximum_bytes=allowance, cancelled=stop)
                    # Re-inspect the actual archive using the same owner package
                    # rules. It must contain exactly this document and its data.
                    checked = _document(_query(manifest, 'inspect', package=str(staged),
                        cancelled=stop, timeout=600))
                    if checked != document:
                        raise ModelManifestError('The actual exported package failed its final identity check.')
                    if capture_package(staged, maximum_bytes=allowance, cancelled=stop) != captured:
                        raise ModelManifestError('The exported package changed during owner inspection.')
                    checksum, size, maximum = captured.sha256, captured.size_bytes, allowance
                    if protected:
                        encrypted = Path(staging) / ('profile' + PROTECTED_SUFFIX)
                        sealed = crypto.encrypt_file(staged, encrypted, passphrase, cancelled=stop)
                        if capture_package(staged, maximum_bytes=allowance, cancelled=stop) != captured:
                            raise ModelManifestError('The exact inspected package changed during encryption.')
                        cipher = capture_package(encrypted, maximum_bytes=cipher_allowance, cancelled=stop)
                        if (not sealed.encrypted or sealed.plaintext_size != captured.size_bytes
                                or (sealed.sha256, sealed.size) != (cipher.sha256, cipher.size_bytes)):
                            raise ModelManifestError('The protected output differs from its encryption receipt.')
                        staged, checksum, size, maximum = encrypted, cipher.sha256, cipher.size_bytes, cipher_allowance
                    if self.manager.store.read() != previous:
                        raise ValueError('Voice registration changed before export publication.')
                    manifest.verify(cancelled=stop, require_complete=True)
                    now = local_path(parent).stat()
                    if (now.st_dev, now.st_ino) != parent_identity:
                        raise ValueError('The selected export directory changed during preparation.')
                    _cancel(stop)
                    receipt = publish_package(staged, destination, private_root=self.root,
                        expected_sha256=checksum, maximum_bytes=maximum,
                        approval=approval, cancelled=stop)
                    published = {'path': str(receipt.path), 'sha256': checksum, 'size_bytes': size,
                                 'profile': document['id'], 'version': document['version'],
                                 'encrypted': protected, 'model_inference': 'NOT_RUN'}
                    if receipt.cleanup_warning:
                        published['cleanup_warning'] = receipt.cleanup_warning
        except Exception:
            if published is None:
                raise
            notice = ('The exact package was saved and verified. Private staging or resource cleanup '
                      'still needs attention; retain this file and do not repeat the export.')
            published['cleanup_warning'] = (published.get('cleanup_warning', '') + ' ' + notice).strip()
        return published
