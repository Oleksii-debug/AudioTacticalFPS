"""Real file preservation across approval budgets and completed publication."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research import export_file
from carr_library.ui.export_destination import confirm_export_destination


def test_large_previous_export_is_rejected_before_open_or_confirmation(tmp_path, monkeypatch):
    target = tmp_path / 'previous.txt'
    original = b'Independent earlier work'
    target.write_bytes(original)
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', len(original) - 1)
    actual_open = os.open

    def guarded_open(path, *args, **kwargs):
        if Path(path) == target:
            raise AssertionError('The oversized previous export must not be opened.')
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(export_file.os, 'open', guarded_open)
    native = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=8,
                             MessageBox=Mock(return_value=8))
    with pytest.raises(ValueError, match='too large'):
        captured = export_file.capture_export_destination(target, private_root=tmp_path / 'private')
        confirm_export_destination(object(), captured, wx_module=native)
    native.MessageBox.assert_not_called()
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_previous_file_budget_does_not_restrict_new_export_size(tmp_path, monkeypatch, binary):
    target = tmp_path / 'new'
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', 1)
    payload = 'Точна цитата\r\nNew complete export.'
    writer = export_file.write_binary_export if binary else export_file.write_export
    receipt = writer(target, payload.encode() if binary else payload,
                     private_root=tmp_path / 'private', require_absent=True)
    assert receipt.path == target and receipt.cleanup_warning is None
    assert target.read_bytes() == payload.encode()
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_expired_approval_budget_preserves_previous_file(tmp_path, monkeypatch, binary):
    target = tmp_path / 'previous'
    original = b'Original previous export'
    target.write_bytes(original)
    monkeypatch.setattr(export_file, '_OVERWRITE_CHECK_SECONDS', 0)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='quickly enough'):
        writer(target, b'new' if binary else 'new', private_root=tmp_path / 'private')
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_published_hard_link_remains_complete_when_cleanup_and_logging_fail(
        tmp_path, monkeypatch, binary):
    """Exercise the retained POSIX publication branch with actual local links."""
    target = tmp_path / 'result'
    # A local proxy selects that branch without changing pathlib's host platform.
    proxy = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    proxy.name = 'posix'
    monkeypatch.setattr(export_file, 'os', proxy)
    unlink = Path.unlink

    def refuse_published_stage(path, *args, **kwargs):
        if path.name.startswith('.research-export-') and target.exists():
            assert path.samefile(target)
            raise PermissionError('Injected persistent staging-link cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', refuse_published_stage)
    monkeypatch.setattr(export_file._logger, 'warning',
                        Mock(side_effect=RuntimeError('Injected logging failure')))
    writer = export_file.write_binary_export if binary else export_file.write_export
    payload = 'Published exact bytes\r\nДжерело.'
    receipt = writer(target, payload.encode() if binary else payload,
                     private_root=tmp_path / 'private', require_absent=True)
    assert receipt.path == target
    assert receipt.cleanup_warning and 'saved' in receipt.cleanup_warning
    assert target.read_bytes() == payload.encode()
    stages = list(tmp_path.glob('.research-export-*'))
    assert len(stages) == 1 and stages[0].samefile(target)
    assert target.stat().st_nlink == 2
    # The remaining second link is never silently approved for replacement.
    with pytest.raises(ValueError, match='hard links'):
        writer(target, b'replace' if binary else 'replace', private_root=tmp_path / 'private')
    assert target.read_bytes() == payload.encode()
    unlink(stages[0])
