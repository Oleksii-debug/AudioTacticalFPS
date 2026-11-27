"""Actual owned protocol children and production mutation admission with doubles.

No synthetic backend result is reported as actual Microsoft Word execution.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest

import carr_library
from carr_library.durable_state import atomic_json, read_json_snapshot
from carr_library.academic_integration.word_records import fingerprint
from carr_library.academic_integration.word_native import WordNative
from carr_library.academic_integration import word_worker

_spec = importlib.util.spec_from_file_location('word_protocol_fixtures', Path(__file__).with_name('word_fixtures.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def wait_file(path, child, timeout=12):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=2)
            raise AssertionError('Synthetic protocol exited before its expected immutable record: '
                                 + str(child.returncode) + ' ' + stderr.decode('utf-8', 'replace')[-1200:])
        if time.monotonic() >= deadline:
            raise AssertionError('Synthetic protocol did not produce its bounded record.')
        time.sleep(.01)
    return read_json_snapshot(path)[0]


@pytest.fixture
def process(tmp_path):
    children = []
    def start(*, mode='complete', preview_seconds=3, request_change=None):
        stage = tmp_path / ('stage-' + str(len(children)))
        stage.mkdir()
        observed = tmp_path / ('observed-' + str(len(children)))
        observed.mkdir()
        info = stage.stat()
        request = {'schema': 'resonance-word-request/1', 'session': fixture.SESSION,
            'mode': 'mutate', 'staging': {'path': str(stage), 'device': info.st_dev, 'inode': info.st_ino},
            'timeout': 20, 'preview_seconds': preview_seconds}
        if request_change:
            request_change(request)
        atomic_json(stage / 'request.json', request)
        digest = sha256((stage / 'request.json').read_bytes()).hexdigest()
        environment = os.environ.copy()
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__'):
            environment.pop(key, None)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        executable = str(Path(sys.executable).absolute())
        child = subprocess.Popen([executable, '-I', '-B', str(Path(fixture.__file__)),
            str(Path(carr_library.__file__).resolve().parent.parent), str(Path(word_worker.__file__).resolve()),
            digest, mode, str(observed)], cwd=stage, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(child)
        return child, stage, observed
    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def preview(child, stage):
    current = wait_file(stage / 'capture.json', child)
    value = fixture.proposal(current)
    atomic_json(stage / 'proposal.json', value)
    return current, value


def approve(child, stage, value):
    atomic_json(stage / 'approved.json', {'schema': 'resonance-word-approval/1',
        'session': fixture.SESSION, 'proposal_sha256': fingerprint(value)})
    return wait_file(stage / 'ready.json', child)


def grant(stage, ready):
    atomic_json(stage / 'grant.json', {'schema': 'resonance-word-grant/1',
        'session': fixture.SESSION, 'ready_sha256': fingerprint(ready)})


def test_actual_protocol_waits_for_each_explicit_approval_boundary(process):
    child, stage, observed = process()
    current, value = preview(child, stage)
    assert child.poll() is None and not (observed / 'mutation.json').exists()
    assert not (stage / 'ready.json').exists() and not (stage / 'begun.json').exists()
    ready = approve(child, stage, value)
    assert child.poll() is None and not (observed / 'mutation.json').exists()
    assert not (stage / 'begun.json').exists()
    grant(stage, ready)
    child.communicate(timeout=12)
    assert child.returncode == 0
    assert read_json_snapshot(observed / 'mutation.json')[0]['count'] == 1
    assert read_json_snapshot(stage / 'receipt.json')[0]['proposal_sha256'] == fingerprint(value)
    assert read_json_snapshot(stage / 'begun.json')[0]['session'] == current['session']


@pytest.mark.parametrize('boundary', ['approval', 'grant', 'capture'])
def test_changed_immutable_protocol_authority_refuses_before_first_mutation(process, boundary):
    child, stage, observed = process()
    current = wait_file(stage / 'capture.json', child)
    value = fixture.proposal(current)
    if boundary == 'capture':
        # The proposal must bind the complete held document, not selected fields.
        value['capture_sha256'] = '0' * 64
    atomic_json(stage / 'proposal.json', value)
    if boundary == 'capture':
        pass  # The invalid capture must fail before asking for approval.
    elif boundary == 'approval':
        atomic_json(stage / 'approved.json', {'schema': 'resonance-word-approval/1',
            'session': fixture.SESSION, 'proposal_sha256': '0' * 64})
    else:
        ready = approve(child, stage, value)
        atomic_json(stage / 'grant.json', {'schema': 'resonance-word-grant/1',
            'session': fixture.SESSION, 'ready_sha256': '0' * 64})
    child.communicate(timeout=12)
    assert child.returncode != 0 and child.poll() is not None
    assert read_json_snapshot(stage / 'failure.json')[0]['category'] == 'invalid-or-unavailable'
    assert not (observed / 'mutation.json').exists()
    assert not (stage / 'receipt.json').exists()


def test_expired_preview_cannot_be_applied_or_replayed(process):
    child, stage, observed = process(preview_seconds=1)
    _, value = preview(child, stage)
    child.communicate(timeout=6)
    assert child.returncode == 1
    assert read_json_snapshot(stage / 'failure.json')[0]['category'] == 'expired'
    assert not (observed / 'mutation.json').exists()
    atomic_json(stage / 'approved.json', {'schema': 'resonance-word-approval/1',
        'session': fixture.SESSION, 'proposal_sha256': fingerprint(value)})
    assert child.poll() is not None and not (stage / 'begun.json').exists()


def test_slow_native_first_write_cannot_use_an_expired_source_grant(process):
    child, stage, observed = process(mode='slow-first-write')
    _, value = preview(child, stage)
    ready = approve(child, stage, value)
    grant(stage, ready)
    child.communicate(timeout=12)
    assert child.returncode == 3  # Begun is conservatively uncertain to the host.
    assert read_json_snapshot(stage / 'failure.json')[0]['category'] == 'expired'
    assert not (observed / 'mutation.json').exists() and not (stage / 'receipt.json').exists()


@pytest.mark.parametrize('extra', ['program', 'macro', 'document_path', 'open_or_save'])
def test_fixed_request_has_no_arbitrary_program_or_document_operations(process, extra):
    child, stage, observed = process(request_change=lambda request: request.__setitem__(extra, 'untrusted'))
    child.communicate(timeout=8)
    assert child.returncode != 0
    assert not (stage / 'capture.json').exists() and not (observed / 'mutation.json').exists()


def test_actual_worker_keeps_completed_record_when_adapter_close_fails(process):
    child, stage, observed = process(mode='close-error')
    _, value = preview(child, stage)
    grant(stage, approve(child, stage, value))
    child.communicate(timeout=12)
    assert child.returncode != 0
    assert (observed / 'mutation.json').is_file()
    assert read_json_snapshot(stage / 'receipt.json')[0] == fixture.receipt(value)


def admitted_native(*, initial_level=0, replace_group=False, track_after_start=False):
    native = WordNative.__new__(WordNative)
    native.session = fixture.SESSION
    current = fixture.capture()
    native.capture = Mock(return_value=current)
    native._same_document = Mock()
    native._controls, native._fields, native._notes, native._metadata_part = {}, [], [], None
    undo = N(CustomRecordLevel=initial_level,
             CustomRecordName='foreign group' if initial_level else '')
    document = N(ReadOnly=False, ProtectionType=-1, TrackRevisions=False)
    def start(name):
        undo.CustomRecordLevel = 1
        undo.CustomRecordName = 'foreign group' if replace_group else name
        if track_after_start:
            document.TrackRevisions = True
    def end():
        undo.CustomRecordLevel = 0; undo.CustomRecordName = ''
    undo.StartCustomRecord, undo.EndCustomRecord = Mock(side_effect=start), Mock(side_effect=end)
    target = N(Start=0, End=0, StoryType=1)
    native.application = N(Selection=N(Range=N(Duplicate=target)), UndoRecord=undo)
    document.ContentControls = N(Add=Mock(side_effect=AssertionError('No content mutation was admitted.')))
    native.document = document
    return native, current, undo, document


@pytest.mark.parametrize('kind', ['already-active', 'replaced-group', 'track-changed'])
def test_production_native_method_never_ends_a_foreign_undo_record_or_changes_revision_policy(kind):
    native, current, undo, document = admitted_native(
        initial_level=1 if kind == 'already-active' else 0,
        replace_group=kind == 'replaced-group', track_after_start=kind == 'track-changed')
    with pytest.raises(ValueError):
        native.apply(fixture.proposal(current), current, check=lambda: None, first_write=lambda: None)
    document.ContentControls.Add.assert_not_called()
    if kind in ('already-active', 'replaced-group'):
        undo.EndCustomRecord.assert_not_called()
        assert undo.CustomRecordName == 'foreign group'
    else:
        undo.EndCustomRecord.assert_called_once()
        assert document.TrackRevisions is True


def test_first_write_authority_is_checked_immediately_before_content_control_add():
    native, current, undo, document = admitted_native()
    seen = []
    def first_write():
        seen.append(undo.CustomRecordName)
        raise TimeoutError('Final source grant expired.')
    with pytest.raises(TimeoutError):
        native.apply(fixture.proposal(current), current, check=lambda: None, first_write=first_write)
    assert seen == ['Resonance citations ' + fixture.SESSION]
    document.ContentControls.Add.assert_not_called()
    undo.EndCustomRecord.assert_called_once()


def test_native_capture_refuses_aggregate_repeated_text_before_reading_remaining_controls():
    native = WordNative.__new__(WordNative)
    native.session = fixture.SESSION
    native._same_document = Mock()
    native._unknown = lambda value: value
    native._address = id
    reads = {'main': 0, 'control': 0}
    class Area:
        Start, End, StoryType = 0, 1_000_000, 1
        def __init__(self, kind):
            self.kind = kind
            self.TextRetrievalMode = N(IncludeHiddenText=False, IncludeFieldCodes=False)
        @property
        def Duplicate(self):
            result = Area(self.kind); result.Document = self.Document
            return result
        @property
        def Text(self):
            assert self.TextRetrievalMode.IncludeHiddenText and self.TextRetrievalMode.IncludeFieldCodes
            reads[self.kind] += 1
            return 'x' * 1_000_000
    main = Area('main')
    ranges = [Area('control') for _ in range(500)]
    controls = [N(ID=str(index), Tag='foreign-' + str(index), Range=area,
                  LockContents=False, LockContentControl=False) for index, area in enumerate(ranges)]
    native.document = N(Content=main, Endnotes=N(Count=0),
        ContentControls=N(Count=len(controls), Item=lambda index: controls[index - 1]))
    for area in [main, *ranges]:
        area.Document = native.document
    native._document_iunknown = native.document
    native.application = N(Selection=N(Range=main))
    with pytest.raises(ValueError, match='combined Word capture'):
        native.capture()
    assert reads == {'main': 1, 'control': 1}
    assert len(controls) == 500  # The other 499 large strings were never fetched.


def test_outside_proof_uses_native_offsets_when_display_text_has_different_length():
    native = WordNative.__new__(WordNative)
    seen = []
    class Area:
        def __init__(self, start=0, end=10):
            self.Start, self.End = start, end
            self.TextRetrievalMode = N(IncludeHiddenText=False, IncludeFieldCodes=False)
        @property
        def Duplicate(self):
            return Area(self.Start, self.End)
        def SetRange(self, start, end):
            self.Start, self.End = start, end
        @property
        def Text(self):
            assert self.TextRetrievalMode.IncludeHiddenText and self.TextRetrievalMode.IncludeFieldCodes
            seen.append((self.Start, self.End))
            return {(0, 4): 'A', (8, 10): 'B', (0, 10): 'Avisible-fieldB'}[(self.Start, self.End)]
    original = Area()
    result = native._outside_hash(original, [(4, 8)])
    assert result == sha256(b'AB').hexdigest()
    assert seen == [(0, 4), (8, 10)]
    assert not original.TextRetrievalMode.IncludeHiddenText and not original.TextRetrievalMode.IncludeFieldCodes
