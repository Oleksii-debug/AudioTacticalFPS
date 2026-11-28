"""Confirmation captures real destination state before the native modal answer.

The dialog is controlled headlessly; filesystem inspection and publication are
real. Native keyboard and accessibility evidence belong to the Windows probe.
"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import write_export
from carr_library.ui.export_destination import confirm_export_destination


@pytest.fixture
def paths(tmp_path):
    private = tmp_path / 'private-library'
    private.mkdir()
    return private, tmp_path / 'export.txt'


@pytest.fixture
def native(monkeypatch):
    wx = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4,
                         YES=10, NO=11, ID_CANCEL=12)
    wx.MessageBox = Mock(return_value=wx.YES)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return wx


def test_absent_destination_needs_no_native_import_and_remains_no_clobber(paths, monkeypatch):
    private, target = paths
    monkeypatch.setitem(sys.modules, 'wx', None)
    approval = confirm_export_destination(object(), target, private_root=private)
    assert approval.require_absent and not target.exists()
    target.write_bytes(b'Competing new file')
    with pytest.raises(ValueError, match='destination changed'):
        write_export(target, 'Must not replace the new file', private_root=private, approval=approval)
    assert target.read_bytes() == b'Competing new file'


@pytest.mark.parametrize('answer', ['NO', 'ID_CANCEL'])
def test_declined_or_cancelled_confirmation_never_grants_approval(paths, native, answer):
    private, target = paths
    target.write_bytes(b'Existing work')
    wx = native
    wx.MessageBox.return_value = getattr(wx, answer)
    parent = object()
    assert confirm_export_destination(parent, target, private_root=private,
                                      title='Replace bibliography') is None
    message, caption, style, owner = wx.MessageBox.call_args.args
    assert owner is parent and target.name in message and caption == 'Replace bibliography'
    assert style == wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
    wx.MessageBox.assert_called_once()
    assert target.read_bytes() == b'Existing work'


def test_yes_is_bound_to_state_before_question_not_after_it(paths, native):
    private, target = paths
    target.write_bytes(b'Originally selected work')
    wx = native

    def answer_after_competing_save(*args):
        target.write_bytes(b'Newer work saved during the question')
        return wx.YES

    wx.MessageBox.side_effect = answer_after_competing_save
    approval = confirm_export_destination(object(), target, private_root=private)
    assert not approval.require_absent
    with pytest.raises(ValueError, match='destination changed'):
        write_export(target, 'Stale replacement', private_root=private, approval=approval)
    wx.MessageBox.assert_called_once()
    assert target.read_bytes() == b'Newer work saved during the question'


def test_modal_failure_preserves_file_without_granting_approval(paths, native):
    private, target = paths
    target.write_bytes(b'Existing work')
    wx = native
    wx.MessageBox.side_effect = RuntimeError('Native modal failure')
    with pytest.raises(RuntimeError, match='Native modal failure'):
        confirm_export_destination(object(), target, private_root=private)
    wx.MessageBox.assert_called_once()
    assert target.read_bytes() == b'Existing work'
