"""Private original-byte retention for cooperative ingestion writers.

Internal service only: asset hashes are not authorization tokens. The caller
must hold the shared writer guard and supply a stable private directory.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from ..extract.document_formats import MIME_SUFFIXES
from ..catalog.streaming_pdf import FileAsset, valid_original_size, snapshot_original

MAX_BYTES = 64 * 1024 * 1024
SUFFIXES = dict(MIME_SUFFIXES)


class RetentionError(ValueError):
    pass


class RetainedAssets:
    def __init__(self, root):
        path = Path(root)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RetentionError('private asset root must be a local directory')
        self.root = path.resolve(strict=True)

    def _paths(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RetentionError('invalid asset digest')
        return [self.root / (digest + suffix) for suffix in SUFFIXES.values()]

    def resolve(self, digest, *, cancelled=lambda: False):
        """Internal verified lookup; never expose directly to unscoped UI."""
        paths = [p for p in self._paths(digest) if os.path.lexists(p)]
        if len(paths) != 1:
            raise RetentionError('missing or ambiguous retained asset')
        path = paths[0]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                getattr(info, 'st_file_attributes', 0) & 0x400 or
                not valid_original_size('application/pdf' if path.suffix == '.pdf' else '', info.st_size)):
            raise RetentionError('invalid retained asset file')
        if path.suffix == '.pdf' and info.st_size > MAX_BYTES:
            FileAsset.capture(path, cancelled=cancelled,
                              expected_manifest=dict(sha256=digest, mime='application/pdf', size=info.st_size))
        elif hash_file(path) != digest:
            raise RetentionError('retained asset digest mismatch')
        return path

    def _recover_staged_publication(self, target, digest, *, cancelled=lambda: False):
        """Finish only a fully accounted link publication under the writer guard.

        A process can die after linking the target but before TemporaryDirectory
        removes its original. Readers still reject extra links. A writer may
        remove those exact staging twins only after validating the complete set;
        an unknown link or ambiguous directory leaves every entry untouched.
        """
        published = target.lstat()
        if (not stat.S_ISREG(published.st_mode) or
                getattr(published, 'st_file_attributes', 0) & 0x400 or
                not valid_original_size('application/pdf' if target.suffix == '.pdf' else '', published.st_size)):
            raise RetentionError('invalid retained asset file')
        if published.st_nlink == 1:
            return
        if published.st_nlink < 2 or not published.st_ino:
            raise RetentionError('invalid retained asset links')
        if target.suffix == '.pdf' and published.st_size > MAX_BYTES:
            from ..audio.jobs import file_identity, verified_reader
            from ..catalog.streaming_pdf import _digest
            with verified_reader(target, expected_identity=file_identity(published)) as stream:
                actual = _digest(stream, published.st_size, cancelled)
        else:
            actual = hash_file(target)
        if actual != digest:
            raise RetentionError('retained asset digest mismatch')

        def private_directory(path):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or
                    getattr(info, 'st_file_attributes', 0) & 0x400 or
                    (os.name == 'posix' and
                     (stat.S_IMODE(info.st_mode) != 0o700 or
                      info.st_uid != os.geteuid()))):
                raise RetentionError('invalid retained asset staging directory')
            return info

        def unchanged(before, after):
            return all(getattr(before, field, 0) == getattr(after, field, 0)
                       for field in ('st_dev', 'st_ino', 'st_mode', 'st_nlink',
                                     'st_size', 'st_mtime_ns', 'st_ctime_ns',
                                     'st_uid', 'st_file_attributes'))

        twins = []
        for directory in self.root.iterdir():
            # The fixed prefix and eight-character suffix are the namespace
            # created by this module's TemporaryDirectory, not arbitrary paths.
            if not re.fullmatch(r'\.carr-original-[a-z0-9_]{8}', directory.name):
                continue
            directory_info = private_directory(directory)
            entries = tuple(directory.iterdir())
            if not entries:
                # A recovery itself may die after unlinking its original.
                # Empty directories have no proven target binding; leave them.
                continue
            if len(entries) != 1 or entries[0].name != 'original':
                raise RetentionError('ambiguous retained asset staging directory')
            original = entries[0]
            original_info = original.lstat()
            if (not stat.S_ISREG(original_info.st_mode) or
                    getattr(original_info, 'st_file_attributes', 0) & 0x400):
                raise RetentionError('invalid retained asset staging original')
            if os.path.samestat(published, original_info):
                if not unchanged(published, original_info):
                    raise RetentionError('retained asset changed during recovery')
                twins.append((directory, directory_info, original, original_info))
        if len(twins) != published.st_nlink - 1:
            raise RetentionError('unaccounted retained asset links')

        # Complete all checks before removing even one staging entry. The caller
        # owns the cooperative writer guard and a stable private directory.
        if not unchanged(published, target.lstat()):
            raise RetentionError('retained asset changed during recovery')
        for directory, directory_info, original, original_info in twins:
            if (not unchanged(directory_info, private_directory(directory)) or
                    tuple(directory.iterdir()) != (original,) or
                    not unchanged(original_info, original.lstat())):
                raise RetentionError('retained asset staging changed during recovery')
        for directory, _, original, _ in twins:
            original.unlink()
            directory.rmdir()

    def retain(self, data, mime, digest):
        """Preserve source bytes; recover only proven abandoned staging links."""
        paths = self._paths(digest)
        if mime not in SUFFIXES or not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise RetentionError('unsupported or oversized asset')
        if sha256(data).hexdigest() != digest:
            raise RetentionError('snapshot digest mismatch')
        target = self.root / (digest + SUFFIXES[mime])
        existing = [p for p in paths if os.path.lexists(p)]
        if existing:
            if len(existing) != 1:
                raise RetentionError('missing or ambiguous retained asset')
            if existing[0] != target:
                raise RetentionError('retained asset type mismatch')
            self._recover_staged_publication(target, digest)
            return self.resolve(digest)
        with TemporaryDirectory(prefix='.carr-original-', dir=self.root) as tmp:
            stage = Path(tmp) / 'original'
            with stage.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if hash_file(stage) != digest:
                raise RetentionError('staged asset digest mismatch')
            # Same-filesystem atomic publication, never replace. A crashed
            # publication is recoverable by retain after exact link accounting.
            os.link(stage, target)
        return self.resolve(digest)

    def retain_file(self, asset, *, cancelled=lambda: False):
        """Publish a verified streamed PDF using the existing no-replace layout."""
        if not isinstance(asset, FileAsset):
            raise RetentionError('A bound PDF file snapshot is required.')
        paths = self._paths(asset.digest)
        target = self.root / (asset.digest + SUFFIXES[asset.mime])
        existing = [p for p in paths if os.path.lexists(p)]
        if existing:
            if len(existing) != 1 or existing[0] != target:
                raise RetentionError('Missing, ambiguous or changed retained asset type.')
            self._recover_staged_publication(target, asset.digest, cancelled=cancelled)
            # Validate the supplied original even when an exact retained copy exists.
            with asset.open(cancelled=cancelled):
                pass
        else:
            with TemporaryDirectory(prefix='.carr-original-', dir=self.root) as tmp:
                stage = Path(tmp) / 'original'
                asset.copy_to(stage, cancelled=cancelled)
                FileAsset.capture(stage, cancelled=cancelled, expected_manifest=asset.manifest())
                os.link(stage, target)
        # Finish directory durability on a successful retry as well.
        if os.name != 'nt':
            descriptor = os.open(self.root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        FileAsset.capture(target, cancelled=cancelled, expected_manifest=asset.manifest())
        return target

    def start(self, ingest, workspace, edition, source_root, relative, **kwargs):
        """Reuse bounded catalog intake and existing Ingest.start."""
        from ..catalog.streaming_pdf import MAX_PDF_BYTES
        asset = snapshot_original(source_root, relative,
                                  pdf_max_bytes=kwargs.get('pdf_max_bytes', MAX_PDF_BYTES),
                                  cancelled=kwargs.get('cancelled', lambda: False))
        retained = (self.retain_file(asset, cancelled=kwargs.get('cancelled', lambda: False))
                    if isinstance(asset, FileAsset) else self.retain(asset.data, asset.mime, asset.digest))
        job = ingest.start(workspace, edition, self.root, retained.name, **kwargs)
        if ingest.inspect(workspace, job)['asset'] != asset.digest:
            raise RetentionError('job asset mismatch')
        return job

    def run(self, ingest, workspace, job, **kwargs):
        """Scope-check via existing journal first; resume from original bytes."""
        record = ingest.inspect(workspace, job)
        retained = self.resolve(record['asset'], cancelled=kwargs.get('cancelled', lambda: False))
        return ingest.run(workspace, job, self.root, retained.name, **kwargs)
