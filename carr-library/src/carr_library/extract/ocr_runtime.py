"""Discover and verify a portable, offline Tesseract installation.

The manifest checks delivered bytes; it is not a publisher signature. The
installer/build manifest supplies distribution provenance. No CWD probing,
downloads, PATH mutation or fallback from a broken explicitly selected bundle.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys

from .ocr_languages import valid_model_code, model_ids

MANIFEST = 'ocr-manifest.json'
MAX_MANIFEST = 2 * 1024 * 1024
MAX_FILES = 2048
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


class OcrRuntimeError(ValueError):
    """A selected portable runtime cannot be used with verified local bytes."""


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value)):
        raise OcrRuntimeError('Invalid portable OCR relative path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value
            or any(p in ('.', '..') or p.endswith((' ', '.')) for p in path.parts)):
        raise OcrRuntimeError('Invalid portable OCR relative path.')
    for part in path.parts:
        if (any(c in part for c in '<>"|?*')
                or part.split('.')[0].casefold() in
                {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                 *(f'lpt{i}' for i in range(1, 10))}):
            raise OcrRuntimeError('Invalid portable OCR Windows path.')
    return path


def _stat(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise OcrRuntimeError('Portable OCR files must be regular, unlinked files.')
    return info


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, limit, remaining, *, retain=False):
    before = _stat(path)
    if before.st_size > limit:
        raise OcrRuntimeError('Portable OCR file exceeds its size limit.')
    digest, content, count = sha256(), [], 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise OcrRuntimeError('Portable OCR file changed during validation.')
        while chunk := stream.read(1024 * 1024):
            remaining()
            count += len(chunk)
            if count > limit:
                raise OcrRuntimeError('Portable OCR file exceeds its size limit.')
            digest.update(chunk)
            if retain:
                content.append(chunk)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise OcrRuntimeError('Portable OCR file changed during validation.')
    if count != before.st_size or _stamp(_stat(path)) != _stamp(before):
        raise OcrRuntimeError('Portable OCR file changed during validation.')
    return count, digest.hexdigest(), b''.join(content)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OcrRuntimeError('Duplicate portable OCR manifest key.')
        result[key] = value
    return result


@dataclass(frozen=True)
class OcrRuntime:
    executable: str
    tessdata: Path | None = None
    target: str | None = None
    manifest_sha256: str | None = None
    files_sha256: tuple = ()
    languages: tuple = ()
    root: Path | None = None

    @property
    def portable(self):
        return self.manifest_sha256 is not None

    def command(self, argv):
        """Bind every model-using call, preserving an explicit pinned staging dir."""
        argv = list(argv)
        if (self.tessdata is not None and '--tessdata-dir' not in argv
                and '--version' not in argv):
            argv[1:1] = ['--tessdata-dir', str(self.tessdata)]
        return argv

    def fingerprint(self):
        return {'manifest_sha256': self.manifest_sha256, 'target': self.target,
                'files_sha256': dict(self.files_sha256)}

    def require_languages(self, languages, orientation=False):
        try:
            selected = model_ids(languages)
        except ValueError:
            raise OcrRuntimeError('Invalid OCR language selection.') from None
        if self.portable:
            wanted = set(selected) | ({'osd'} if orientation else set())
            if not wanted <= set(self.languages):
                raise OcrRuntimeError('The portable OCR bundle lacks the selected language or orientation model.')

    def assert_unchanged(self, remaining=lambda: None):
        if self.portable and verify_ocr_bundle(self.root, remaining=remaining) != self:
            raise OcrRuntimeError('Portable OCR runtime changed during execution.')


@dataclass(frozen=True)
class PackOcrRuntime:
    """A separate model selection over the unchanged, fully verified runtime."""
    base: OcrRuntime
    pack: object

    @property
    def executable(self):
        return self.base.executable

    @property
    def tessdata(self):
        return self.pack.root

    @property
    def root(self):
        return self.base.root

    @property
    def portable(self):
        return self.base.portable

    @property
    def target(self):
        return self.base.target

    @property
    def manifest_sha256(self):
        return self.base.manifest_sha256

    @property
    def model_pack_sha256(self):
        return self.pack.manifest_sha256

    @property
    def languages(self):
        return self.pack.languages

    def command(self, argv):
        argv = list(argv)
        if (argv.count('--tessdata-dir') > 1
                or any(str(value).startswith('--tessdata-dir=') for value in argv)):
            raise OcrRuntimeError('An OCR command attempted to replace the selected verified model pack.')
        if '--tessdata-dir' in argv:
            index = argv.index('--tessdata-dir')
            if index + 1 >= len(argv) or argv[index + 1] != str(self.tessdata):
                raise OcrRuntimeError('An OCR command attempted to replace the selected verified model pack.')
        elif '--version' not in argv:
            argv[1:1] = ['--tessdata-dir', str(self.tessdata)]
        return argv

    def fingerprint(self):
        return {'base_runtime': self.base.fingerprint(), 'model_pack': self.pack.fingerprint()}

    def require_languages(self, languages, orientation=False):
        wanted = set(languages.split('+')) | ({'osd'} if orientation else set())
        if not wanted <= set(self.languages):
            raise OcrRuntimeError('The selected verified OCR pack lacks a selected language or OSD model.')

    def assert_unchanged(self, remaining=lambda: None):
        from .language_manager import verify_offline_pack
        # Keep the entire original bundle inventory check, including its models,
        # executable, libraries and licenses, even though recognition uses a pack.
        self.base.assert_unchanged(remaining)
        if verify_offline_pack(self.tessdata, expected_manifest_sha256=self.model_pack_sha256,
                               remaining=remaining) != self.pack:
            raise OcrRuntimeError('The selected OCR model pack changed during execution.')


def bind_model_pack(runtime, path, digest, *, languages=None, orientation=False,
                    remaining=lambda: None):
    from .language_manager import validate_pack_selection, verify_offline_pack
    validate_pack_selection(path, digest)
    if path is None:
        return runtime
    if runtime is None:
        raise OcrRuntimeError('A verified model pack also needs an installed local OCR engine.')
    pack = verify_offline_pack(path, expected_manifest_sha256=digest, remaining=remaining)
    if runtime.portable and (pack.root == runtime.root or pack.root.is_relative_to(runtime.root)):
        raise OcrRuntimeError('A separate OCR pack cannot be installed inside the sealed runtime.')
    bound = PackOcrRuntime(runtime, pack)
    if languages is not None:
        bound.require_languages(languages, orientation)
    return bound


def verify_ocr_bundle(root, *, remaining=lambda: None):
    """Validate the complete manifest inventory without executing native code.

    Works on build hosts of any OS. All payload files, including DLLs, models
    and licenses, must be listed. Only the manifest itself is excluded.
    """
    try:
        root = Path(root).absolute()
        for ancestor in (root, *root.parents):
            _stat(ancestor, directory=True)
        remaining()
        _, manifest_hash, raw = _read(root / MANIFEST, MAX_MANIFEST, remaining, retain=True)
        data = json.loads(raw, object_pairs_hook=_unique_object)
        required = {'schema', 'executable', 'tessdata', 'files', 'target'}
        if (not isinstance(data, dict) or not required <= set(data)
                or set(data) - required - {'version', 'provenance'}
                or type(data['schema']) is not int or data['schema'] != 1
                or data['target'] not in ('win_amd64', 'linux_x86_64', 'linux_aarch64',
                                          'darwin_x86_64', 'darwin_arm64')
                or ('version' in data and not isinstance(data['version'], str))
                or ('provenance' in data and not isinstance(data['provenance'], dict))):
            raise OcrRuntimeError('Invalid portable OCR manifest schema.')
        executable = str(_relative(data['executable']))
        tessdata = str(_relative(data['tessdata']))
        files = data['files']
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
            raise OcrRuntimeError('Invalid portable OCR file inventory.')
        if executable not in files or f'{tessdata}/eng.traineddata' not in files:
            raise OcrRuntimeError('Portable OCR needs an executable and English language data.')
        if MANIFEST in files or len({name.casefold() for name in files}) != len(files):
            raise OcrRuntimeError('Ambiguous portable OCR file inventory.')
        total = 0
        for name, item in files.items():
            _relative(name)
            if (not isinstance(item, dict) or set(item) != {'size', 'sha256'}
                    or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE
                    or not isinstance(item['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise OcrRuntimeError('Invalid portable OCR file identity.')
            total += item['size']
        if total > MAX_TOTAL:
            raise OcrRuntimeError('Portable OCR bundle exceeds its size limit.')
        _stat(root / tessdata, directory=True)
        found = set()
        for directory, dirs, names in os.walk(root, followlinks=False):
            remaining()
            for name in dirs:
                _stat(Path(directory) / name, directory=True)
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                _stat(path)
                if relative != MANIFEST:
                    found.add(relative)
                if len(found) > MAX_FILES:
                    raise OcrRuntimeError('Portable OCR bundle exceeds its file limit.')
        if found != set(files):
            raise OcrRuntimeError('Portable OCR bundle has missing or unlisted files.')
        hashes = []
        for name, item in sorted(files.items()):
            size, digest, _ = _read(root / name, item['size'], remaining)
            if size != item['size'] or digest != item['sha256']:
                raise OcrRuntimeError('Portable OCR file checksum mismatch.')
            hashes.append((name, digest))
        # The manifest cannot be changed between selecting and checking its files.
        if _read(root / MANIFEST, MAX_MANIFEST, remaining)[1] != manifest_hash:
            raise OcrRuntimeError('Portable OCR manifest changed during validation.')
        languages = tuple(sorted(
            str(PurePosixPath(name).relative_to(tessdata))[:-len('.traineddata')]
            for name in files
            if name.endswith('.traineddata') and PurePosixPath(name).is_relative_to(tessdata)
            and valid_model_code(str(PurePosixPath(name).relative_to(tessdata))[:-len('.traineddata')])
        ))
        return OcrRuntime(str(root / executable), root / tessdata, data['target'],
                          manifest_hash, tuple(hashes), languages, root)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, OcrRuntimeError):
            raise
        raise OcrRuntimeError('Portable OCR runtime unavailable or invalid.') from None


def _native_target():
    machine = platform.machine().casefold()
    if sys.platform == 'win32' and machine in ('amd64', 'x86_64'):
        return 'win_amd64'
    if sys.platform == 'linux':
        return {'amd64': 'linux_x86_64', 'x86_64': 'linux_x86_64',
                'aarch64': 'linux_aarch64', 'arm64': 'linux_aarch64'}.get(machine)
    if sys.platform == 'darwin':
        return {'x86_64': 'darwin_x86_64', 'arm64': 'darwin_arm64'}.get(machine)
    return None


def discover_ocr_runtime(languages='eng', *, orientation=False, remaining=lambda: None,
                         model_profile='installed'):
    """Use the unchanged engine authority and one explicitly selected profile."""
    try:
        model_ids(languages)
    except ValueError:
        raise OcrRuntimeError('Invalid OCR language selection.') from None
    if model_profile not in ('installed', 'fast', 'best'):
        raise OcrRuntimeError('Choose an installed, Fast or Best OCR model profile.')
    if model_profile == 'installed':
        return _discover_ocr_runtime(languages, orientation=orientation, remaining=remaining)
    runtime = _discover_ocr_runtime('eng', orientation=False, remaining=remaining)
    from .managed_ocr import bind_model_profile
    return bind_model_profile(runtime, model_profile, languages,
                              orientation=orientation, remaining=remaining)


def _discover_ocr_runtime(languages='eng', *, orientation=False, remaining=lambda: None):
    """Prefer explicitly selected or package-relative OCR, then developer PATH.

    Automatic discovery is limited to the interpreter prefixes and their two
    immediate ancestors. A Windows bundle is never auto-executed on Linux.
    An explicit environment selection is also useful for build-host testing.
    """
    explicit = os.environ.get('CARR_OCR_DIR')
    if explicit is not None:
        if not explicit or not Path(explicit).is_absolute():
            raise OcrRuntimeError('CARR_OCR_DIR must name an absolute portable OCR directory.')
        runtime = verify_ocr_bundle(explicit, remaining=remaining)
        runtime.require_languages(languages, orientation)
        return runtime
    seen = set()
    for prefix in (sys.prefix, sys.base_prefix):
        base = Path(prefix)
        if not base.is_absolute():
            continue
        for parent in (base, base.parent, base.parent.parent):
            candidate = parent / 'ocr'
            if candidate in seen:
                continue
            seen.add(candidate)
            if not os.path.lexists(candidate):
                continue
            runtime = verify_ocr_bundle(candidate, remaining=remaining)
            if runtime.target != _native_target():
                continue
            runtime.require_languages(languages, orientation)
            return runtime
    executable = shutil.which('tesseract')
    return OcrRuntime(executable) if executable else None


def check_runtime(*, timeout=30):
    """Execute real English OCR on a generated, nonprivate image without network."""
    import subprocess
    import tempfile
    import time

    deadline = time.monotonic() + timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise OcrRuntimeError('OCR installation check timed out.')
        return seconds

    runtime = discover_ocr_runtime('eng', remaining=remaining)
    if runtime is None:
        raise OcrRuntimeError('No local OCR engine was found.')

    def run(*args):
        return subprocess.run(runtime.command([runtime.executable, *args]),
                              check=True, capture_output=True, timeout=remaining(),
                              cwd=runtime.root,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.decode('utf-8')

    version = run('--version').splitlines()[0]
    if not version.startswith('tesseract '):
        raise OcrRuntimeError('The OCR engine returned an unexpected version.')
    report = run('--list-langs')
    languages = [line.strip() for line in report.splitlines()
                 if valid_model_code(line.strip())]
    if 'eng' not in languages:
        raise OcrRuntimeError('The OCR engine cannot load English language data.')
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise OcrRuntimeError('Pillow is required for the real OCR installation check.') from None
    with tempfile.TemporaryDirectory(prefix='research-library-ocr-check-') as tmp:
        picture = Image.new('RGB', (640, 160), 'white')
        ImageDraw.Draw(picture).text((28, 40), 'RESEARCH', fill='black',
                                    font=ImageFont.load_default(size=52))
        path = Path(tmp) / 'synthetic.png'
        picture.save(path, dpi=(300, 300))
        recognized = ' '.join(run(str(path), 'stdout', '-l', 'eng', '--psm', '7',
                                  '--dpi', '300').split())
    if recognized != 'RESEARCH':
        raise OcrRuntimeError('The OCR engine did not correctly read the synthetic test word.')
    runtime.assert_unchanged(remaining)
    return {'status': 'PASS', 'engine': version, 'languages': languages,
            'runtime': 'portable' if runtime.portable else 'system',
            'manifest_sha256': runtime.manifest_sha256,
            'synthetic_recognition': recognized,
            'platform': sys.platform,
            'windows_execution': 'PASS' if sys.platform == 'win32' else 'NOT_RUN'}


def main(argv=None):
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(description='Check local offline OCR with a synthetic image')
    parser.add_argument('--check', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        result = check_runtime()
    except (OcrRuntimeError, OSError, ValueError, RuntimeError, UnicodeError,
            IndexError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'FAIL', 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
