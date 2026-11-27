"""Canonical refresh and two-archive delivery retain the reviewed native seal.

Real Git, wheel/source verification, runtime locks and installer verification are
used with inert synthetic PE/Python payloads. No application or binary executes.
"""
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'native_launcher_fixture_support', Path(__file__).with_name('test_native_launcher_bundle.py'))
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
candidate = support.candidate


def complete_ocr_fixture(value):
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode='w') as archive:
        raw = b'Synthetic corresponding source; no application data.\n'
        entry = tarfile.TarInfo('source/README.txt')
        entry.size = len(raw)
        archive.addfile(entry, io.BytesIO(raw))
    additional = {'ocr/tesseract.exe': support.synthetic_pe(),
                  'ocr/tessdata/eng.traineddata': b'Synthetic OCR model.',
                  'ocr/ocr-manifest.json': b'{"synthetic":true}\n',
                  'ocr-corresponding-source.tar': source.getvalue()}
    manifest_path = value.output / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for name, raw in additional.items():
        path = value.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        manifest['files'][name] = sha256(raw).hexdigest()
    manifest['bundled_ocr'] = True
    manifest_path.write_text(json.dumps(manifest))
    value.installer.verify(value.output)


def prepare(value, monkeypatch, *, native=True):
    options = {} if native else dict(native_launcher=None, native_launcher_proof=None,
                                    native_launcher_sha256=None)
    support.build(value, **options)
    complete_ocr_fixture(value)
    packaging = value.native.parent
    (packaging / 'windows-cp311.lock').write_bytes(value.lock.read_bytes())
    (packaging / 'windows-sbom.cdx.json').write_text(json.dumps({
        'bomFormat': 'CycloneDX', 'specVersion': '1.5',
        'components': [{'name': 'fixture', 'version': '1.0'}]}))
    (packaging.parent / 'docs/START_V3_UK.txt').write_text('Synthetic canonical instructions.\n')
    monkeypatch.syspath_prepend(str(ROOT / 'packaging'))
    refresh = support.module('native_canonical_refresh', ROOT / 'packaging/refresh_private_bundle.py')
    monkeypatch.setattr(refresh, 'HERE', packaging)
    # Configure the actual verifiers against this fixture's committed repository.
    monkeypatch.setattr(refresh, 'verify_application_source', value.builder.verify_application_source)
    monkeypatch.setattr(refresh, 'capture_native_launcher', value.builder.capture_native_launcher)
    raw = (value.output / 'bundle-manifest.json').read_bytes()
    return SimpleNamespace(value=value, module=refresh, source=value.output,
                           parent=raw, parent_hash=sha256(raw).hexdigest(),
                           output=value.output.with_name('refreshed'), packaging=packaging)


def refresh(value, **options):
    return value.module.refresh(value.source, value.value.app, value.output, value.value.commit,
                               parent_manifest_sha256=value.parent_hash, **options)


def explicit(value):
    return dict(native_launcher=value.exe, native_launcher_proof=value.proof,
                native_launcher_sha256=value.pin)


def test_refresh_rebinds_retained_native_proof_to_the_new_application_source(candidate, monkeypatch):
    value = prepare(candidate, monkeypatch)
    old_commit = candidate.commit
    package = value.packaging.parent / 'src/carr_library'
    (package / '__main__.py').write_bytes(b'# New synthetic application revision.\nraise SystemExit(0)\n')
    support.git(candidate.repo, 'add', '.')
    support.git(candidate.repo, 'commit', '-qm', 'New application with unchanged reviewed native inputs')
    candidate.commit = support.git(candidate.repo, 'rev-parse', 'HEAD')
    support.write_wheel(candidate.app, 'universal_research_library',
                        {'carr_library/' + p.name: p.read_bytes() for p in package.iterdir()},
                        requires=('fixture==1.0',))
    refresh(value)
    candidate.installer.verify(value.output)
    manifest = json.loads((value.output / 'bundle-manifest.json').read_bytes())
    assert manifest['source_commit'] == manifest['native_launcher_source_commit'] == candidate.commit
    assert manifest['source_tree'] == support.git(candidate.repo, 'rev-parse', candidate.commit + '^{tree}')
    assert manifest['source_tree'] != json.loads(value.parent)['source_tree']
    assert manifest['parent_source_commit'] == old_commit and manifest['parent_manifest_sha256'] == value.parent_hash
    for name in ('ResearchLibrary.exe', 'native-launcher-build.json'):
        assert (value.output / name).read_bytes() == (value.source / name).read_bytes()
    assert (value.source / 'bundle-manifest.json').read_bytes() == value.parent


def test_refresh_can_add_reviewed_native_entry_to_a_legacy_nonnative_bundle(candidate, monkeypatch):
    value = prepare(candidate, monkeypatch, native=False)
    refresh(value, **explicit(candidate))
    candidate.installer.verify(value.output)
    manifest = json.loads((value.output / 'bundle-manifest.json').read_bytes())
    assert manifest['bundled_native_launcher'] is True
    assert manifest['native_launcher_sha256'] == candidate.pin
    assert manifest['native_launcher_source_commit'] == candidate.commit
    assert not (value.source / 'ResearchLibrary.exe').exists()
    assert (value.source / 'bundle-manifest.json').read_bytes() == value.parent


@pytest.mark.parametrize('mask', [1, 2, 3, 4, 5, 6])
def test_refresh_rejects_partial_native_options_before_publication(candidate, monkeypatch, mask):
    value = prepare(candidate, monkeypatch, native=False)
    options = {name: argument if mask & (1 << index) else None
               for index, (name, argument) in enumerate(explicit(candidate).items())}
    with pytest.raises(ValueError, match='together'):
        refresh(value, **options)
    assert not value.output.exists()
    assert (value.source / 'bundle-manifest.json').read_bytes() == value.parent


def test_refresh_rejects_reusing_a_launcher_when_committed_native_source_changes(candidate, monkeypatch):
    value = prepare(candidate, monkeypatch)
    source = candidate.native / 'launcher.c'
    source.write_bytes(source.read_bytes() + b'\n/* Changed native source. */\n')
    support.git(candidate.repo, 'add', '.')
    support.git(candidate.repo, 'commit', '-qm', 'Changed native source cannot inherit the old binary proof')
    candidate.commit = support.git(candidate.repo, 'rev-parse', 'HEAD')
    with pytest.raises(ValueError, match='committed source'):
        refresh(value)
    assert not value.output.exists()
    assert (value.source / 'bundle-manifest.json').read_bytes() == value.parent


def test_reused_native_proof_remains_bound_to_the_verified_parent_snapshot(candidate, monkeypatch):
    value = prepare(candidate, monkeypatch)
    original = value.module.verify

    def replace_after_parent_verification(root, **kwargs):
        result = original(root, **kwargs)
        if root == value.source:
            proof = root / 'native-launcher-build.json'
            proof.write_bytes(proof.read_bytes() + b'\n')  # Still valid evidence, different sealed bytes.
        return result

    monkeypatch.setattr(value.module, 'verify', replace_after_parent_verification)
    with pytest.raises(ValueError, match='parent native launcher changed'):
        refresh(value)
    assert not value.output.exists()


def test_refresh_uses_captured_native_inputs_after_mutable_paths_are_replaced(candidate, monkeypatch):
    value = prepare(candidate, monkeypatch, native=False)
    expected_exe, expected_proof = candidate.exe.read_bytes(), candidate.proof.read_bytes()
    original = value.module.shutil.copyfile
    changed = []

    def replace_before_copy(source, destination, *args, **kwargs):
        if not changed:
            candidate.exe.write_bytes(b'Unverified later executable')
            candidate.proof.write_bytes(b'Unverified later producer proof')
            changed.append(True)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(value.module.shutil, 'copyfile', replace_before_copy)
    refresh(value, **explicit(candidate))
    assert changed == [True]
    assert (value.output / 'ResearchLibrary.exe').read_bytes() == expected_exe
    assert (value.output / 'native-launcher-build.json').read_bytes() == expected_proof
    candidate.installer.verify(value.output)


def canonical_runtimes(value):
    audio = value.output.parent / 'canonical-audio'
    citations = value.output.parent / 'canonical-citations'
    audio.mkdir()
    citations.mkdir()
    lock = json.loads((ROOT / 'packaging/windows-audio.lock.json').read_bytes())
    for name, entry in lock['output_files'].items():
        raw = support.synthetic_pe() if name.endswith('.exe') else b'Synthetic canonical audio notice.\n'
        path = audio / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entry.update(size=len(raw), sha256=sha256(raw).hexdigest())
    (value.packaging / 'windows-audio.lock.json').write_text(json.dumps(lock))
    node = json.loads((ROOT / 'packaging/windows-citations.lock.json').read_bytes())
    for name in node['files']:
        raw = support.synthetic_pe() if name.endswith('.exe') else b'Synthetic canonical citation notice.\n'
        path = citations / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        node['files'][name] = sha256(raw).hexdigest()
    (value.packaging / 'windows-citations.lock.json').write_text(json.dumps(node))
    return audio, citations


@pytest.mark.parametrize('component,flag,required', [
    ('audio', 'bundled_audio', 'audio/bin/ffmpeg.exe'),
    ('citation-runtime', 'bundled_citation_runtime', 'citation-runtime/node.exe')])
def test_native_refresh_preserves_canonical_audio_and_citation_seals(candidate, monkeypatch, component, flag, required):
    value = prepare(candidate, monkeypatch)
    audio, citations = canonical_runtimes(value)
    refresh(value, audio=audio, citations=citations)
    candidate.installer.verify(value.output)
    path = value.output / 'bundle-manifest.json'
    manifest = json.loads(path.read_bytes())
    assert all(manifest[key] for key in ('bundled_native_launcher', 'bundled_audio', 'bundled_citation_runtime'))
    assert manifest[flag] is True
    assert (value.output / 'audio/bin/ffmpeg.exe').read_bytes() == (audio / 'bin/ffmpeg.exe').read_bytes()
    assert (value.output / 'citation-runtime/node.exe').read_bytes() == (citations / 'node.exe').read_bytes()
    # Remove both bytes and their ordinary hash entry: the canonical required-file
    # contract must still reject the incomplete runtime beside a valid native seal.
    (value.output / required).unlink()
    del manifest['files'][required]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=component + ' runtime file'):
        candidate.installer.verify(value.output)


def test_delivery_keeps_native_proof_and_gives_the_matching_owner_entrypoint(candidate, tmp_path):
    support.build(candidate)
    complete_ocr_fixture(candidate)
    delivery = support.module('native_delivery_archive', ROOT / 'packaging/create_delivery.py')
    report = delivery.create_delivery(candidate.output, candidate.commit, tmp_path / 'delivery')
    application = Path(report['output']) / report['archives']['application']['path']
    with ZipFile(application) as archive:
        for name in ('ResearchLibrary.exe', 'native-launcher-build.json'):
            assert archive.read(delivery.PREFIX + name) == (candidate.output / name).read_bytes()
    instructions = (Path(report['output']) / 'START_HERE_UK.txt').read_text(encoding='utf-8')
    assert '4. Запускайте програму через ResearchLibrary.exe.' in instructions
    assert report['boundaries']['native_execution'] == 'NOT_RUN'
