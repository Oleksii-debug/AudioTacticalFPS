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

MAX_BYTES = 64 * 1024 * 1024
SUFFIXES = {'text/plain': '.txt', 'application/pdf': '.pdf'}


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

    def resolve(self, digest):
        """Internal verified lookup; never expose directly to unscoped UI."""
        paths = [p for p in self._paths(digest) if os.path.lexists(p)]
        if len(paths) != 1:
            raise RetentionError('missing or ambiguous retained asset')
        path = paths[0]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                getattr(info, 'st_file_attributes', 0) & 0x400 or
                not 0 < info.st_size <= MAX_BYTES):
            raise RetentionError('invalid retained asset file')
        if hash_file(path) != digest:
            raise RetentionError('retained asset digest mismatch')
        return path

    def retain(self, data, mime, digest):
        """Preserve validated snapshot bytes; no overwrite, rename or deletion."""
        paths = self._paths(digest)
        if mime not in SUFFIXES or not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise RetentionError('unsupported or oversized asset')
        if sha256(data).hexdigest() != digest:
            raise RetentionError('snapshot digest mismatch')
        target = self.root / (digest + SUFFIXES[mime])
        if any(os.path.lexists(p) for p in paths):
            if self.resolve(digest) != target:
                raise RetentionError('retained asset type mismatch')
            return target
        with TemporaryDirectory(prefix='.carr-original-', dir=self.root) as tmp:
            stage = Path(tmp) / 'original'
            with stage.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if hash_file(stage) != digest:
                raise RetentionError('staged asset digest mismatch')
            # Same-filesystem atomic publication, never replace. A crashed
            # staging directory may be left behind; do not auto-delete it.
            os.link(stage, target)
        return self.resolve(digest)

    def start(self, ingest, workspace, edition, source_root, relative, **kwargs):
        """Reuse bounded catalog intake and existing Ingest.start."""
        from carr_library.catalog.intake import snapshot
        asset = snapshot(source_root, relative)
        retained = self.retain(asset.data, asset.mime, asset.digest)
        job = ingest.start(workspace, edition, self.root, retained.name, **kwargs)
        if ingest.inspect(workspace, job)['asset'] != asset.digest:
            raise RetentionError('job asset mismatch')
        return job

    def run(self, ingest, workspace, job, **kwargs):
        """Scope-check via existing journal first; resume from original bytes."""
        record = ingest.inspect(workspace, job)
        retained = self.resolve(record['asset'])
        return ingest.run(workspace, job, self.root, retained.name, **kwargs)
