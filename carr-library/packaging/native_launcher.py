"""Admit the current source's unsigned private launcher into a sealed bundle."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat

HERE = Path(__file__).resolve().parent / 'native-launcher'
SOURCES = ('launcher.c', 'launcher_core.h', 'launcher.manifest', 'launcher.rc',
           'windows_child_probe.c', 'build_msvc.ps1', 'probe_pe.py')


def _read(path, maximum):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not 0 < info.st_size <= maximum):
        raise ValueError('Launcher inputs must be bounded ordinary files.')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    after = path.lstat()
    identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    if len(raw) != info.st_size or identity(info) != identity(after):
        raise ValueError('Launcher input changed during verification.')
    return raw


def verify(directory):
    directory = Path(directory).absolute()
    for parent in (directory, *directory.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Choose an ordinary local launcher build directory.')
    executable = directory / 'ResearchLibrary.exe'
    raw = _read(executable, 2 * 1024 * 1024)
    evidence_bytes = _read(directory / 'build-evidence.json', 128 * 1024)
    # Windows PowerShell 5 writes UTF-8 with a BOM; preserve those exact bytes.
    evidence = json.loads(evidence_bytes.decode('utf-8-sig'))
    spec = importlib.util.spec_from_file_location('carr_launcher_pe', HERE / 'probe_pe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    actual = probe.inspect_pe(executable)
    if (actual['sha256'] != hashlib.sha256(raw).hexdigest() or type(evidence) is not dict
            or evidence.get('schema') != 1 or evidence.get('target') != 'Windows x64 / MSVC'
            or evidence.get('release_status') != 'unsigned-private-candidate'
            or evidence.get('pe') != actual):
        raise ValueError('Launcher build evidence does not match the inspected executable.')
    expected = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in SOURCES}
    if evidence.get('source_sha256') != expected:
        raise ValueError('Rebuild the launcher from the current reviewed source.')
    return raw, evidence_bytes
