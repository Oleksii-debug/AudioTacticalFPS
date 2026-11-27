"""Real file snapshots across a controlled native overwrite confirmation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import write_binary_export, write_export
from carr_library.ui.export_destination import confirm_export_destination


@pytest.fixture
def native():
    wx = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=8, NO=16)
    wx.MessageBox = Mock(return_value=wx.YES)
    return wx


def test_new_destination_needs_no_overwrite_confirmation(tmp_path, native):
    target = tmp_path / 'new.txt'
    approval = confirm_export_destination(object(), target,
        private_root=tmp_path / 'private', wx_module=native)
    assert approval.require_absent
    native.MessageBox.assert_not_called()
    assert not target.exists()


def test_declined_confirmation_preserves_existing_bytes(tmp_path, native):
    target = tmp_path / 'previous.txt'
    target.write_bytes(b'Previous independent work')
    native.MessageBox.return_value = native.NO
    parent = object()
    assert confirm_export_destination(parent, target,
        private_root=tmp_path / 'private', wx_module=native) is None
    message, title, style, owner = native.MessageBox.call_args.args
    assert target.name in message and title == 'Replace export' and owner is parent
    assert style & native.NO_DEFAULT and style & native.YES_NO
    assert target.read_bytes() == b'Previous independent work'
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_file_changed_while_confirmation_is_open_is_not_approved(tmp_path, native, binary):
    target = tmp_path / 'selected'
    target.write_bytes(b'File inspected before confirmation')
    private = tmp_path / 'private'

    def confirm(*args):
        target.write_bytes(b'Independent edit during confirmation')
        return native.YES

    native.MessageBox.side_effect = confirm
    approval = confirm_export_destination(object(), target, private_root=private, wx_module=native)
    writer = write_binary_export if binary else write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == b'Independent edit during confirmation'
    assert not list(tmp_path.glob('.research-export-*'))


def test_private_destination_is_rejected_before_confirmation(tmp_path, native):
    private = tmp_path / 'private'
    private.mkdir()
    target = private / 'original.txt'
    target.write_bytes(b'Original source')
    with pytest.raises(ValueError, match='outside the private library'):
        confirm_export_destination(object(), target, private_root=private, wx_module=native)
    native.MessageBox.assert_not_called()
    assert target.read_bytes() == b'Original source'
