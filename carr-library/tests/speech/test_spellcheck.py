"""Real code-only Spylls, explicit synthetic dictionaries and the owned worker."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Timer

import pytest

from carr_library.speech import spellcheck as module
from carr_library.speech.reading import ReadingDocument
from carr_library.ui.pronunciation_dialog import ReadingSnapshot
from carr_library.ui.spellcheck_dialog import apply_spelling_preview, preview_spelling


def dictionary(tmp_path, *, aff=None, words=None):
    aff = aff or ('SET UTF-8\nTRY abcdefghijklmnopqrstuvwxyzабвгдежзийклмнопрстуфхцчшщьюяіїє\n'
                  'SFX S Y 1\nSFX S 0 s .\n')
    words = words or ['hello', 'world', 'book/S', 'cat/S', 'привіт', 'світ']
    (tmp_path / 'chosen.aff').write_text(aff, encoding='utf-8')
    (tmp_path / 'chosen.dic').write_text(str(len(words)) + '\n' + '\n'.join(words) + '\n', encoding='utf-8')
    return tmp_path / 'chosen.aff'


def capture(document, selection=(0, 0)):
    return ReadingSnapshot.capture(document, selection)


def test_actual_lookup_affixes_unicode_offsets_and_source_preserving_apply_undo(tmp_path):
    selected = dictionary(tmp_path)
    source = '😀 Привіт.\r\nhello wurld books cats світ'
    document = ReadingDocument(source)
    original = document.to_dict()
    snapshot = capture(document)
    before = {p.name: p.read_bytes() for p in tmp_path.glob('chosen.*')}
    review = module.review_spelling(source, selected, library_root=tmp_path)
    assert review.complete and review.checked_tokens == review.total_tokens == 6
    assert [(i.word, i.start, i.end) for i in review.issues] == [('wurld', source.index('wurld'), source.index('wurld') + 5)]
    assert 'world' in review.issues[0].suggestions
    assert review.dictionary.aff_sha256 == sha256(before['chosen.aff']).hexdigest()
    preview = preview_spelling(snapshot, review, (0, len(source)), 0, 'world')
    module.verify_dictionary(review.dictionary)
    assert apply_spelling_preview(document, preview, snapshot)
    assert document.text == source.replace('wurld', 'world') and document.source_text == source
    assert not document.source_span(preview.start, preview.end).exact
    assert document.source_span(0, preview.start).exact
    saved = tmp_path / 'reading.json'; document.save(saved)
    restored = ReadingDocument.load(saved)
    assert restored.undo() and restored.text == source
    assert restored.source_text == original['source_text']
    assert {p.name: p.read_bytes() for p in tmp_path.glob('chosen.*')} == before


def test_selected_scope_maps_only_the_reviewed_word_and_retains_dictionary_encoding(tmp_path):
    selected = dictionary(tmp_path, aff='SET ISO8859-1\nTRY abcdefghijklmnopqrstuvwxyzé\n', words=['café', 'world'])
    selected.write_bytes(selected.read_text().encode('latin-1'))
    selected.with_suffix('.dic').write_bytes('2\ncafé\nworld\n'.encode('latin-1'))
    document = ReadingDocument('wurld outside; café wurld; outside wurld')
    start, end = document.text.index('café'), document.text.index('; outside')
    snapshot = capture(document, (start, end))
    review = module.review_spelling(document.text[start:end], selected.with_suffix('.dic'), library_root=tmp_path)
    preview = preview_spelling(snapshot, review, (start, end), 0, 'world')
    assert apply_spelling_preview(document, preview, snapshot)
    assert document.text == 'wurld outside; café world; outside wurld'


def test_issue_limit_reports_partial_coverage_instead_of_claiming_clean_text(tmp_path):
    selected = dictionary(tmp_path)
    review = module.review_spelling('wurld wurld world', selected, library_root=tmp_path,
                                   budget=module.SpellBudget(max_issues=1))
    assert not review.complete and review.checked_tokens == 1 and review.total_tokens == 3
    assert len(review.issues) == 1


def test_dictionary_wordchars_include_prefix_digits_without_reviewing_bare_numbers(tmp_path):
    selected = dictionary(tmp_path, aff='SET UTF-8\nWORDCHARS 0123456789\n', words=['21st', 'h2o', 'world'])
    review = module.review_spelling('21st h2o 123 world', selected, library_root=tmp_path)
    assert review.complete and review.checked_tokens == review.total_tokens == 3 and not review.issues


@pytest.mark.parametrize('changed', ['content', 'selection', 'identity', 'undo_branch'])
def test_stale_review_cannot_change_the_reading_copy(tmp_path, changed):
    selected = dictionary(tmp_path)
    document = ReadingDocument('hello wurld')
    snapshot = capture(document)
    review = module.review_spelling(document.text, selected, library_root=tmp_path)
    preview = preview_spelling(snapshot, review, (0, len(document.text)), 0, 'world')
    if changed == 'content':
        document.set_text('independent revision')
    elif changed == 'identity':
        document = ReadingDocument(document.text)
    elif changed == 'undo_branch':
        document.set_text('intermediate'); document.undo()
    current = capture(document, (1, 2) if changed == 'selection' else (0, 0))
    before = document.to_dict()
    with pytest.raises(ValueError, match='changed'):
        apply_spelling_preview(document, preview, current)
    assert document.to_dict() == before


@pytest.mark.parametrize('change', ['aff', 'dic'])
def test_actual_dictionary_change_after_worker_invalidates_its_result(tmp_path, monkeypatch, change):
    selected = dictionary(tmp_path)
    real = module.run_process
    def run(*args, **kwargs):
        result = real(*args, **kwargs)
        target = selected.with_suffix('.' + change)
        target.write_bytes(target.read_bytes() + b'\n')
        return result
    monkeypatch.setattr(module, 'run_process', run)
    with pytest.raises(ValueError, match='dictionary changed'):
        module.review_spelling('wurld', selected, library_root=tmp_path)


def test_saved_review_rejects_changed_pair_before_apply(tmp_path):
    selected = dictionary(tmp_path)
    review = module.review_spelling('wurld', selected, library_root=tmp_path)
    selected.with_suffix('.dic').write_text('1\nother\n')
    with pytest.raises(ValueError, match='dictionary changed'):
        module.verify_dictionary(review.dictionary)


@pytest.mark.parametrize('kind', ['missing', 'extension', 'nul', 'count', 'rare', 'too_many_tokens', 'long_token'])
def test_invalid_dictionary_or_scope_is_rejected_without_altering_input(tmp_path, kind):
    selected = dictionary(tmp_path)
    text, budget = 'wurld', module.SpellBudget()
    if kind == 'missing':
        selected.with_suffix('.dic').unlink()
    elif kind == 'extension':
        selected = tmp_path / 'chosen.txt'; selected.write_text('1\nword')
    elif kind == 'nul':
        selected.write_bytes(b'SET UTF-8\n\0')
    elif kind == 'count':
        selected.with_suffix('.dic').write_text('200001\nword\n')
    elif kind == 'rare':
        selected.write_text('SET UTF-8\nCHECKCOMPOUNDPATTERN 1\na b\n')
    elif kind == 'too_many_tokens':
        text, budget = 'world world', module.SpellBudget(max_tokens=1)
    else:
        text = 'a' * 97
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises((ValueError, FileNotFoundError)):
        module.review_spelling(text, selected, library_root=tmp_path, budget=budget)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_oversized_pair_fails_before_child_launch(tmp_path, monkeypatch):
    selected = dictionary(tmp_path)
    monkeypatch.setattr(module, 'MAX_DIC_BYTES', 3)
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('oversized dictionary launched a child'))
    with pytest.raises(ValueError, match='byte budget'):
        module.review_spelling('world', selected, library_root=tmp_path)


def test_denied_shared_memory_admission_does_not_launch_or_copy_dictionary(tmp_path, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceLimits, ResourceUnavailable
    selected = dictionary(tmp_path)
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(memory_bytes=0, min_free_disk_bytes=0))
    monkeypatch.setattr(module, '_capture_pair', lambda *a: pytest.fail('read dictionary before admission'))
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('child started without admission'))
    with pytest.raises(ResourceUnavailable):
        module.review_spelling('world', selected, library_root=tmp_path, broker=broker)


@pytest.mark.parametrize('cancel', [False, True])
def test_actual_pathological_affix_is_bounded_and_owned_child_reaped(tmp_path, monkeypatch, cancel):
    from carr_library.audio import process as runner
    selected = dictionary(tmp_path, aff='SET UTF-8\nSFX X Y 1\nSFX X 0 s (a+)+\n', words=['a/X'])
    actual, children, stopped = subprocess.Popen, [], Event()
    class Processes:
        def Popen(self, *args, **kwargs):
            process = actual(*args, **kwargs); children.append(process); return process
        def __getattr__(self, name):
            return getattr(subprocess, name)
    if os.name == 'nt':
        from carr_library.audio import windows_job
        actual_job = windows_job.JobProcess
        def owned(*args, **kwargs):
            process = actual_job(*args, **kwargs); children.append(process); return process
        monkeypatch.setattr(windows_job, 'JobProcess', owned)
    else:
        monkeypatch.setattr(runner, 'subprocess', Processes())
    timer = Timer(.25, stopped.set)
    if cancel:
        timer.start()
    try:
        with pytest.raises((ValueError, InterruptedError)):
            module.review_spelling('a' * 40 + 'bs', selected, library_root=tmp_path,
                cancelled=stopped.is_set, budget=module.SpellBudget(timeout=.6))
    finally:
        timer.cancel(); timer.join() if timer.ident is not None else None
    from carr_library.operations import ResourceBroker
    assert len(children) == 1 and all(child.poll() is not None for child in children)
    assert ResourceBroker.for_app(tmp_path).snapshot()['leases'] == []


def test_actual_abrupt_child_exit_returns_no_review_and_releases_admission(tmp_path, monkeypatch):
    selected = dictionary(tmp_path)
    real = module.run_process
    def killed(args, **kwargs):
        args = list(args)
        index = args.index('-c') + 1
        args[index] = args[index].replace("runpy.run_path(str(p),run_name='__main__')", '(__import__("os")._exit(23))')
        return real(args, **kwargs)
    monkeypatch.setattr(module, 'run_process', killed)
    with pytest.raises(ValueError, match='could not finish'):
        module.review_spelling('wurld', selected, library_root=tmp_path)
    from carr_library.operations import ResourceBroker
    assert ResourceBroker.for_app(tmp_path).snapshot()['leases'] == []


@pytest.mark.parametrize('field', ['text_sha256', 'aff_sha256', 'worker_sha256', 'backend_wheel_sha256'])
def test_actual_reply_is_bound_to_text_dictionary_and_reviewed_code(tmp_path, monkeypatch, field):
    selected = dictionary(tmp_path)
    actual = module.run_process
    def tamper(*args, **kwargs):
        result = actual(*args, **kwargs)
        value = json.loads(result.stdout); value[field] = '0' * 64
        return replace(result, stdout=json.dumps(value).encode())
    monkeypatch.setattr(module, 'run_process', tamper)
    with pytest.raises(ValueError, match='unbound'):
        module.review_spelling('wurld', selected, library_root=tmp_path)


def test_uncertain_native_cleanup_retains_the_actual_reservation_and_staged_pair(tmp_path, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceBroker
    selected = dictionary(tmp_path)
    broker = ResourceBroker.for_app(tmp_path)
    owner, stages = CleanupOwner(lambda: True), []
    def uncertain(args, **kwargs):
        stage = Path(args[-1]).parent
        assert (stage / 'dictionary.aff').is_file() and (stage / 'dictionary.dic').is_file()
        assert len(broker.snapshot()['leases']) == 1
        stages.append(stage)
        raise AudioCleanupPending('Test native owner cannot yet confirm shutdown.', cleanup_owner=owner)
    monkeypatch.setattr(module, 'run_process', uncertain)
    try:
        with pytest.raises(AudioCleanupPending) as raised:
            module.review_spelling('world', selected, library_root=tmp_path, broker=broker)
        assert raised.value.cleanup_owner is owner and stages[0].is_dir()
        assert len(broker.snapshot()['leases']) == 1
    finally:
        assert owner.close()
    assert not stages[0].exists() and broker.snapshot()['leases'] == []


def test_linked_dictionary_is_refused_before_worker_start(tmp_path, monkeypatch):
    selected = dictionary(tmp_path)
    os.link(selected.with_suffix('.dic'), tmp_path / 'alias.dic')
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('linked dictionary launched'))
    with pytest.raises(ValueError, match='without links'):
        module.review_spelling('world', selected, library_root=tmp_path)


@pytest.mark.parametrize('available', [False, True])
def test_optional_backend_must_be_present_and_exact_code_only_version(tmp_path, monkeypatch, available):
    selected = dictionary(tmp_path)
    def distribution(name):
        if not available:
            raise module.importlib.metadata.PackageNotFoundError(name)
        from types import SimpleNamespace
        return SimpleNamespace(version='0.1.7')
    monkeypatch.setattr(module.importlib.metadata, 'distribution', distribution)
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('unreviewed component launched'))
    with pytest.raises(ValueError, match='reviewed'):
        module.review_spelling('world', selected, library_root=tmp_path)
