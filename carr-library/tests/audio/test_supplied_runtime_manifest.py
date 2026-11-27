"""Local draft setup is bounded and cannot approve or execute an ASR runtime."""
from pathlib import Path
import os
import runpy
import subprocess

import pytest

from carr_library.audio import supplied_runtime_manifest as draft
from carr_library.audio.models import AudioCancelled
from carr_library.audio.transcription import SuppliedWhisperRuntime
from carr_library.durable_state import atomic_json, digest_file, read_json


@pytest.fixture
def installation(tmp_path, monkeypatch):
    # Importlib collection and installed-bundle tests do not add tests/audio to
    # sys.path. Load only the authored sibling fixture; its carr_library imports
    # still resolve to the actual package under test, without adding src paths.
    fixtures = runpy.run_path(str(Path(__file__).with_name('test_supplied_transcription.py')))
    return fixtures['installation'].__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def selected(installation):
    original = read_json(installation)
    return {name: original[name] for name in
            ('python', 'worker', 'runtime_directory', 'models_directory', 'model')}


def test_draft_uses_canonical_inventory_without_approving_or_executing(tmp_path, selected, monkeypatch):
    runtime = Path(selected['runtime_directory'])
    # Valid file inventory does not establish a usable or safe runtime. This
    # sentinel would fail if inspection imported its package.
    (runtime/'faster_whisper'/'__init__.py').write_text(
        "raise AssertionError('A manifest inspector must not import runtime code')\n", encoding='utf-8')
    def must_not_execute(*args, **kwargs):
        raise AssertionError('Manifest inspection must not launch an interpreter or model.')
    monkeypatch.setattr(subprocess, 'Popen', must_not_execute)
    output = tmp_path/'draft-runtime.json'
    result = draft.write_draft_manifest(output, **selected)
    data = read_json(output)
    assert result['status'] == 'review_required'
    assert data['runtime_reviewed'] is False and data['rights_reviewed'] is False
    assert 'REVIEW REQUIRED' in data['rights_notice']
    assert data['runtime_files']['faster_whisper/__init__.py'] == digest_file(runtime/'faster_whisper'/'__init__.py')
    with pytest.raises(ValueError, match='reviewed'):
        SuppliedWhisperRuntime(output)
    # Test-only review flags establish schema/inventory compatibility, not a
    # production approval. verify() still performs no inference or imports.
    data.update(runtime_reviewed=True, rights_reviewed=True,
                rights_notice='Authored synthetic fixture compatibility check only.')
    reviewed = tmp_path/'synthetic-test-only-review.json'
    atomic_json(reviewed, data)
    assert len(SuppliedWhisperRuntime(reviewed).verify()[0]) == 64


@pytest.mark.parametrize('budget,match', [
    (draft.ManifestBudget(max_files=1), 'file allowance'),
    (draft.ManifestBudget(max_entries=1), 'directory allowance'),
    (draft.ManifestBudget(max_bytes=1), 'byte allowance'),
])
def test_draft_enforces_global_inventory_budgets_without_partial_publication(tmp_path, selected, budget, match):
    output = tmp_path/'not-created.json'
    with pytest.raises(ValueError, match=match):
        draft.write_draft_manifest(output, budget=budget, **selected)
    assert not output.exists()


def test_cancellation_during_inspection_publishes_nothing(tmp_path, selected):
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 12
    output = tmp_path/'cancelled.json'
    with pytest.raises(AudioCancelled):
        draft.write_draft_manifest(output, cancelled=cancelled, **selected)
    assert len(calls) >= 12 and not output.exists()


def test_existing_manifest_and_racing_publication_are_preserved(tmp_path, selected, monkeypatch):
    output = tmp_path/'owner.json'
    output.write_text('Owner settings', encoding='utf-8')
    with pytest.raises(FileExistsError):
        draft.write_draft_manifest(output, **selected)
    assert output.read_text(encoding='utf-8') == 'Owner settings'
    racing = tmp_path/'racing.json'
    real = draft.atomic_json
    def appeared(path, data, *, replace=False):
        Path(path).write_text('Concurrent owner settings', encoding='utf-8')
        return real(path, data, replace=replace)
    monkeypatch.setattr(draft, 'atomic_json', appeared)
    with pytest.raises(FileExistsError):
        draft.write_draft_manifest(racing, **selected)
    assert racing.read_text(encoding='utf-8') == 'Concurrent owner settings'


@pytest.mark.parametrize('root', ['runtime_directory', 'models_directory'])
def test_manifest_cannot_be_written_inside_its_inventoried_installation(selected, root):
    output = Path(selected[root])/'draft.json'
    with pytest.raises(ValueError, match='outside'):
        draft.write_draft_manifest(output, **selected)
    assert not output.exists()


def test_unsafe_hardlinks_and_standalone_bytecode_are_rejected(tmp_path, selected):
    runtime = Path(selected['runtime_directory'])
    original = runtime/'faster_whisper'/'__init__.py'
    link = runtime/'linked.py'
    os.link(original, link)
    output = tmp_path/'unsafe.json'
    with pytest.raises(ValueError, match='without links'):
        draft.write_draft_manifest(output, **selected)
    link.unlink()
    (runtime/'loose.pyc').write_bytes(b'Unreviewed bytecode')
    with pytest.raises(ValueError, match='Standalone Python bytecode'):
        draft.write_draft_manifest(output, **selected)
    assert not output.exists()


def test_only_canonical_runtime_cache_directories_are_excluded(tmp_path, selected):
    runtime = Path(selected['runtime_directory'])
    cache = runtime/'faster_whisper'/'__pycache__'
    cache.mkdir()
    (cache/'poison.pyc').write_bytes(b'Never imported')
    output = tmp_path/'cache-excluded.json'
    draft.write_draft_manifest(output, **selected)
    assert not any('__pycache__' in name for name in read_json(output)['runtime_files'])


@pytest.mark.parametrize('damage', ['worker', 'runtime', 'model'])
def test_unrecognized_worker_or_missing_protocol_files_fail_before_publication(tmp_path, selected, damage):
    if damage == 'worker':
        Path(selected['worker']).write_text('Unrecognized source', encoding='utf-8')
    elif damage == 'runtime':
        (Path(selected['runtime_directory'])/'av'/'__init__.py').unlink()
    else:
        (Path(selected['models_directory'])/selected['model']/'model.bin').unlink()
    output = tmp_path/'invalid.json'
    with pytest.raises(ValueError):
        draft.write_draft_manifest(output, **selected)
    assert not output.exists()


def test_cli_can_prepare_unapproved_manifest_without_creating_application_state(tmp_path, selected, capsys):
    from carr_library.transcription_cli import main
    output = tmp_path/'from-cli.json'
    data_root = tmp_path/'unused-application-data'
    args = ['--data-dir', str(data_root), 'draft-runtime', '--output', str(output)]
    for name, value in selected.items():
        args.extend(['--' + name.replace('_', '-'), str(value)])
    assert main(args) == 0
    assert not data_root.exists()
    assert read_json(output)['rights_reviewed'] is False
    assert 'review_required' in capsys.readouterr().out


def test_cli_interrupted_manifest_inspection_does_not_advertise_a_saved_job(tmp_path, selected, capsys, monkeypatch):
    from carr_library.transcription_cli import main
    def stopped(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(draft, 'write_draft_manifest', stopped)
    output = tmp_path/'interrupted.json'
    args = ['draft-runtime', '--output', str(output)]
    for name, value in selected.items():
        args.extend(['--' + name.replace('_', '-'), str(value)])
    assert main(args) == 130
    assert 'remains unapproved' in capsys.readouterr().err
    assert not output.exists()
