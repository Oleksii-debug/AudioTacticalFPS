"""Source/evidence and package boundaries using inert, non-executed PE fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = load('native_launcher_admission', 'packaging/native_launcher.py')
pe_probe = load('native_launcher_pe_fixture', 'packaging/native-launcher/probe_pe.py')


def inert_pe():
    data = bytearray(2048)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 132, 0x8664, 1, 0, 0, 0, 240, 0)
    optional = 152
    struct.pack_into('<H', data, optional, 0x20b)
    struct.pack_into('<HH', data, optional + 68, 2, 0x160)
    struct.pack_into('<I', data, optional + 108, 16)
    struct.pack_into('<II', data, optional + 120, 0x1050, 40)
    struct.pack_into('<II', data, optional + 128, 0x1200, 256)
    struct.pack_into('<IIII', data, optional + 240 + 8, 1536, 0x1000, 1536, 512)
    struct.pack_into('<IIIII', data, 0x250, 0, 0, 0, 0x1100, 0)
    data[0x300:0x30d] = b'KERNEL32.dll\0'
    manifest = b'<requestedExecutionLevel level="asInvoker" uiAccess="false"/><longPathAware>true</longPathAware>'
    data[0x400:0x400 + len(manifest)] = manifest
    return bytes(data)


def build(tmp_path):
    directory = tmp_path / 'private fixture'
    directory.mkdir()
    executable = directory / 'ResearchLibrary.exe'
    executable.write_bytes(inert_pe())
    evidence = {'schema': 1, 'target': 'Windows x64 / MSVC',
        'release_status': 'unsigned-private-candidate', 'pe': pe_probe.inspect_pe(executable),
        'source_sha256': {name: hashlib.sha256((verifier.HERE / name).read_bytes()).hexdigest()
                          for name in verifier.SOURCES}}
    report = directory / 'build-evidence.json'
    report.write_text(json.dumps(evidence), encoding='utf-8-sig')
    return directory, executable, report, evidence


def test_current_source_exact_bytes_and_powershell_bom_are_preserved(tmp_path):
    directory, executable, report, _ = build(tmp_path)
    assert verifier.verify(directory) == (executable.read_bytes(), report.read_bytes())


@pytest.mark.parametrize('change', ['executable', 'source', 'report', 'oversize', 'directory', 'missing'])
def test_changed_unbounded_or_wrong_source_launcher_is_not_admitted(tmp_path, change):
    directory, executable, report, evidence = build(tmp_path)
    if change == 'executable':
        executable.write_bytes(executable.read_bytes() + b'changed')
    elif change == 'source':
        evidence['source_sha256']['launcher.c'] = '0' * 64
        report.write_text(json.dumps(evidence))
    elif change == 'report':
        evidence['pe']['sha256'] = '0' * 64
        report.write_text(json.dumps(evidence))
    elif change == 'oversize':
        executable.write_bytes(b'0' * (2 * 1024 * 1024 + 1))
    elif change == 'directory':
        executable.unlink()
        executable.mkdir()
    else:
        report.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        verifier.verify(directory)
