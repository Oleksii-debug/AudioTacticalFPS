"""Native consent adapters with real destinations and atomic publishers.

These checks use explicit wx doubles; native focus/NVDA are separate evidence.
"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import write_binary_export, write_export
from carr_library.ui.export_destination import approve_export_destination


@pytest.fixture
def paths(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    return private, tmp_path / 'Research — Україна.txt'


@pytest.mark.parametrize('writer,payload', [(write_export, 'Approved export'),
                                         (write_binary_export, b'Approved document')],
                         ids=['text', 'binary'])
def test_new_destination_stays_no_clobber_across_rendering(paths, writer, payload):
    private, target = paths
    question = Mock(side_effect=AssertionError('A new file needs no overwrite question'))
    approval = approve_export_destination(object(), target, private_root=private, confirm=question)
    target.write_bytes(b'A new competing document')
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, payload, private_root=private, approval=approval)
    question.assert_not_called()
    assert target.read_bytes() == b'A new competing document'
    assert not tuple(target.parent.glob('.research-export-*'))


def test_confirmation_keeps_the_original_snapshot_if_file_changes_during_question(paths):
    private, target = paths
    target.write_bytes(b'Original chosen output')

    def consent(parent, selected):
        assert selected == target
        selected.write_bytes(b'An external edit made while the question was open')
        return True

    approval = approve_export_destination(None, target, private_root=private, confirm=consent)
    with pytest.raises(ValueError, match='destination changed'):
        write_export(target, 'Rendered replacement', private_root=private, approval=approval)
    assert target.read_bytes() == b'An external edit made while the question was open'


def test_confirmed_existing_file_can_be_replaced_if_unchanged(paths):
    private, target = paths
    target.write_bytes(b'Previous approved output')
    question = Mock(return_value=True)
    parent = object()
    approval = approve_export_destination(parent, target, private_root=private, confirm=question)
    question.assert_called_once_with(parent, target)
    write_export(target, 'Replacement — Україна\r\n', private_root=private, approval=approval)
    assert target.read_bytes() == 'Replacement — Україна\r\n'.encode('utf-8')


@pytest.mark.parametrize('answer', [False, None, 1, 'yes'])
def test_only_explicit_affirmative_consent_returns_an_overwrite_approval(paths, answer):
    private, target = paths
    target.write_bytes(b'Keep existing output')
    assert approve_export_destination(None, target, private_root=private,
                                      confirm=lambda *_: answer) is None
    assert target.read_bytes() == b'Keep existing output'


def test_private_destination_is_rejected_before_asking_for_consent(paths):
    private, _ = paths
    target = private / 'catalog.sqlite'
    target.write_bytes(b'Private library data')
    question = Mock()
    with pytest.raises(ValueError, match='outside the private library'):
        approve_export_destination(None, target, private_root=private, confirm=question)
    question.assert_not_called()
    assert target.read_bytes() == b'Private library data'


def test_parent_directory_replacement_invalidates_selected_destination(paths):
    private, target = paths
    folder = target.parent / 'exports'
    folder.mkdir()
    selected = folder / 'selected.txt'
    approval = approve_export_destination(None, selected, private_root=private)
    folder.rename(folder.with_name('moved exports'))
    folder.mkdir()
    with pytest.raises(ValueError, match='destination changed'):
        write_export(selected, 'Must not publish to another directory', private_root=private,
                     approval=approval)
    assert not selected.exists()


@pytest.mark.parametrize('answer,expected', [(10, True), (11, False), (12, False)],
                         ids=['yes', 'no', 'cancel'])
def test_native_question_defaults_to_no_and_always_destroys_its_dialog(
        paths, monkeypatch, answer, expected):
    private, target = paths
    target.write_text('Previous output', encoding='utf-8')
    question = Mock()
    question.ShowModal.return_value = answer
    wx = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, ID_YES=10,
                         MessageDialog=Mock(return_value=question))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    parent = object()
    approval = approve_export_destination(parent, target, private_root=private)
    args, options = wx.MessageDialog.call_args
    assert args[0] is parent and str(target) in args[1]
    assert options['style'] & wx.NO_DEFAULT
    assert (approval is not None) is expected
    question.Destroy.assert_called_once()
    assert target.read_text(encoding='utf-8') == 'Previous output'


def test_native_question_destroys_its_dialog_after_display_failure(paths, monkeypatch):
    private, target = paths
    target.write_bytes(b'Previous output')
    question = Mock()
    question.ShowModal.side_effect = RuntimeError('Synthetic display failure')
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(YES_NO=1, NO_DEFAULT=2,
        ICON_QUESTION=4, ID_YES=10, MessageDialog=Mock(return_value=question)))
    with pytest.raises(RuntimeError, match='Synthetic display failure'):
        approve_export_destination(None, target, private_root=private)
    question.Destroy.assert_called_once()
    assert target.read_bytes() == b'Previous output'
