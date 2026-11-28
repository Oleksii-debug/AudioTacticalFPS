"""Actual PDF/database review boundary plus headless native-handler checks.

The explicit confirmations below concern authored synthetic fixtures. They test
the control boundary, not real human review, Windows, or NVDA acceptance.
"""
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.source_review import PdfReviewSession, ReviewDialog, SourceReviewActions


@pytest.fixture
def pdf_case(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'synthetic.pdf'
    canvas = Canvas(str(source))
    canvas.drawString(72, 700, 'Railway history on the first original page.')
    canvas.showPage()
    canvas.drawString(72, 700, 'Second original page about canals.')
    canvas.save()
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Independent source review')
    collection = app.create_collection(workspace, 'Selected sources')
    # Directly execute the real importer for this trusted synthetic fixture;
    # separate ingestion tests cover subprocess isolation and resume.
    result = app._import_file(workspace, source,
        title='Synthetic title; $(this is data)', collections=(collection,))
    assert result['state'] == 'complete'
    hit, = app.search(workspace, 'Railway history', collection=collection)
    return SimpleNamespace(app=app, workspace=workspace, collection=collection,
        hit=hit, imported=result, source=source)


def session(case, *, topic=None):
    return PdfReviewSession(case.app, case.workspace, case.hit.ref, case.hit.revision, topic=topic)


def reviews(case):
    with case.app.read(case.workspace) as db:
        return tuple(tuple(row) for row in db.execute('SELECT reviewer,active FROM source_page_review'))


def test_preview_and_opening_original_do_not_approve_extraction(pdf_case):
    current = session(pdf_case)
    assert current.page.state == 'unverified'
    assert 'Railway history on the first original page.' in current.page.preview
    assert current.page.locator.index == 1
    launcher = Mock(return_value=True)
    current.open_original(launcher)
    path = Path(launcher.call_args.args[0])
    assert path.parent == pdf_case.app.root / 'source-views'
    assert path.name == current.page.identity.asset_sha256 + '.pdf'
    assert path.read_bytes() == pdf_case.source.read_bytes()
    assert '$(this is data)' not in str(path)
    assert reviews(pdf_case) == ()
    with pytest.raises(ValueError, match='unverified extraction'):
        pdf_case.app.copy(pdf_case.workspace, pdf_case.hit.ref, pdf_case.hit.revision)


def test_viewer_edits_preserve_originals_and_cannot_receive_approval(pdf_case):
    current = session(pdf_case)
    launcher = Mock(return_value=True)
    current.open_original(launcher)
    viewed = Path(launcher.call_args.args[0])
    retained = pdf_case.app.assets.resolve(current.page.identity.asset_sha256)
    original = retained.read_bytes()
    assert viewed != retained and not viewed.samefile(retained)
    viewed.write_bytes(b'Edited by the external reader, not the retained original.')
    assert retained.read_bytes() == original
    with pytest.raises(ValueError, match='viewing copy changed'):
        current.approve('Synthetic reviewer', confirmed=True)
    assert reviews(pdf_case) == ()
    current.open_original(launcher)
    refreshed = Path(launcher.call_args.args[0])
    assert refreshed.parent == pdf_case.app.root / 'source-views'
    assert refreshed != viewed and refreshed.read_bytes() == original
    assert viewed.read_bytes().startswith(b'Edited by the external reader')
    assert retained.read_bytes() == original


@pytest.mark.parametrize('confirmation', [False, None, 1, 'yes'])
def test_review_requires_explicit_boolean_confirmation(pdf_case, confirmation):
    with pytest.raises(ValueError, match='independent comparison'):
        session(pdf_case).approve('Synthetic reviewer', confirmed=confirmation)
    assert reviews(pdf_case) == ()


@pytest.mark.parametrize('reviewer', ['', ' ', 'first\nsecond', 'x' * 513])
def test_review_requires_independent_reviewer_identity(pdf_case, reviewer):
    with pytest.raises(ValueError, match='reviewer'):
        session(pdf_case).approve(reviewer, confirmed=True)
    assert reviews(pdf_case) == ()


def test_approval_uses_actual_service_without_approving_printed_numbers(pdf_case):
    current = session(pdf_case)
    current.approve('Synthetic independent reviewer', confirmed=True)
    assert reviews(pdf_case) == (('Synthetic independent reviewer', 1),)
    hit, = pdf_case.app.search(pdf_case.workspace, 'Railway history')
    assert hit.verified is True
    assert 'Physical page 1' in hit.page and 'printed page unverified' in hit.page
    assert pdf_case.app.copy(pdf_case.workspace, hit.ref, hit.revision, 'quote') == 'Railway history'
    with pdf_case.app.read(pdf_case.workspace) as db:
        assert db.execute('SELECT verified FROM page WHERE physical=1').fetchone()[0] == 0


def test_revocation_denies_fresh_copy_and_cannot_be_undone_here(pdf_case):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    current = session(pdf_case)
    with pytest.raises(ValueError, match='Confirm withdrawal'):
        current.revoke()
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)
    current.revoke(confirmed=True)
    assert reviews(pdf_case) == (('Synthetic reviewer', 0),)
    with pytest.raises(ValueError, match='unverified extraction'):
        pdf_case.app.copy(pdf_case.workspace, pdf_case.hit.ref, pdf_case.hit.revision)
    revoked = session(pdf_case)
    assert revoked.page.state == 'revoked' and 'cannot be restored' in revoked.page.status
    with pytest.raises(ValueError, match='cannot be replaced'):
        revoked.approve('Another reviewer', confirmed=True)
    assert 'Railway history' in pdf_case.app.preview(pdf_case.workspace, pdf_case.hit.ref, pdf_case.hit.revision)


def test_changed_review_status_invalidates_open_dialog(pdf_case):
    stale = session(pdf_case)
    session(pdf_case).approve('Other independent reviewer', confirmed=True)
    with pytest.raises(ValueError, match='review status changed'):
        stale.approve('Old dialog reviewer', confirmed=True)
    assert reviews(pdf_case) == (('Other independent reviewer', 1),)


def test_changed_selection_and_removed_membership_cannot_be_approved(pdf_case):
    current = session(pdf_case)
    selected = pdf_case.app._selections[pdf_case.hit.ref]
    pdf_case.app._selections[pdf_case.hit.ref] = replace(selected, ordinal=2)
    with pytest.raises(ValueError, match='selected source'):
        current.approve('Synthetic reviewer', confirmed=True)
    pdf_case.app._selections[pdf_case.hit.ref] = selected
    with pdf_case.app.catalog_transaction(pdf_case.workspace) as catalog:
        catalog.remove(pdf_case.collection, selected.edition)
    with pytest.raises(ValueError, match='left the selected collection'):
        current.approve('Synthetic reviewer', confirmed=True)
    assert reviews(pdf_case) == ()


@pytest.mark.parametrize('mutation', ['changed', 'missing'])
def test_original_drift_blocks_open_and_approval_but_not_revocation(pdf_case, mutation):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    current = session(pdf_case)
    retained = pdf_case.app.assets.resolve(current.page.identity.asset_sha256)
    if mutation == 'changed':
        retained.write_bytes(b'Changed original, outside the application.')
    else:
        retained.unlink()
    launcher = Mock(return_value=True)
    with pytest.raises(ValueError, match='retained'):
        current.open_original(launcher)
    launcher.assert_not_called()
    current.revoke(confirmed=True)
    assert reviews(pdf_case) == (('Synthetic reviewer', 0),)


@pytest.mark.parametrize('mutation', ['changed', 'missing'])
def test_revocation_only_session_never_grants_original_source_authority(pdf_case, mutation):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    retained = pdf_case.app.assets.resolve(pdf_case.imported['asset'])
    if mutation == 'changed':
        retained.write_bytes(b'Changed retained source.')
    else:
        retained.unlink()
    with pytest.raises(ValueError):
        session(pdf_case)
    current = PdfReviewSession(pdf_case.app, pdf_case.workspace,
        pdf_case.hit.ref, pdf_case.hit.revision, revocation_only=True)
    opener = Mock(return_value=True)
    with pytest.raises(ValueError, match='withdrawing'):
        current.open_original(opener)
    with pytest.raises(ValueError):
        current.approve('Another reviewer', confirmed=True)
    opener.assert_not_called()
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)
    current.revoke(confirmed=True)
    assert reviews(pdf_case) == (('Synthetic reviewer', 0),)
    with pytest.raises(ValueError):
        pdf_case.app.copy(pdf_case.workspace, pdf_case.hit.ref, pdf_case.hit.revision)
    with pytest.raises(ValueError):
        pdf_case.app.preview(pdf_case.workspace, pdf_case.hit.ref, pdf_case.hit.revision)
    with pytest.raises(ValueError):
        pdf_case.app.search(pdf_case.workspace, 'Railway history', result_mode='short')


def test_revocation_only_session_rechecks_selected_collection_and_identity(pdf_case):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    current = PdfReviewSession(pdf_case.app, pdf_case.workspace,
        pdf_case.hit.ref, pdf_case.hit.revision, revocation_only=True)
    selected = pdf_case.app._selections[pdf_case.hit.ref]
    pdf_case.app._selections[pdf_case.hit.ref] = replace(selected, ordinal=2)
    with pytest.raises(ValueError, match='selected source'):
        current.revoke(confirmed=True)
    pdf_case.app._selections[pdf_case.hit.ref] = selected
    with pdf_case.app.catalog_transaction(pdf_case.workspace) as catalog:
        catalog.remove(pdf_case.collection, selected.edition)
    with pytest.raises(ValueError, match='left the selected collection'):
        current.revoke(confirmed=True)
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)


def test_archived_topic_denies_preview_open_and_approval(pdf_case):
    topic = pdf_case.app.create_topic(pdf_case.workspace, 'Topic for review')
    current = session(pdf_case, topic=topic)
    with pdf_case.app.topic_transaction(pdf_case.workspace) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError):
        session(pdf_case, topic=topic)
    opener = Mock(return_value=True)
    with pytest.raises(ValueError):
        current.open_original(opener)
    with pytest.raises(ValueError):
        current.approve('Synthetic reviewer', confirmed=True)
    opener.assert_not_called()
    assert reviews(pdf_case) == ()


def test_topic_is_rechecked_inside_shared_source_launch_read(pdf_case, monkeypatch):
    from carr_library.ui import source_review
    topic = pdf_case.app.create_topic(pdf_case.workspace, 'Topic for source viewing')
    current = session(pdf_case, topic=topic)
    original_launch = source_review.launch_source_file

    def archive_before_launch(app, source, opener):
        with pdf_case.app.topic_transaction(pdf_case.workspace) as tree:
            tree.archive(topic)
        return original_launch(app, source, opener)

    monkeypatch.setattr(source_review, 'launch_source_file', archive_before_launch)
    opener = Mock(return_value=True)
    with pytest.raises(ValueError):
        current.open_original(opener)
    opener.assert_not_called()
    assert reviews(pdf_case) == ()


def test_archived_topic_denies_revocation_in_existing_session(pdf_case):
    topic = pdf_case.app.create_topic(pdf_case.workspace, 'Reviewed topic')
    session(pdf_case, topic=topic).approve('Synthetic reviewer', confirmed=True)
    current = session(pdf_case, topic=topic)
    with pdf_case.app.topic_transaction(pdf_case.workspace) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError):
        current.revoke(confirmed=True)
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)


def test_cancelled_or_failed_render_does_not_create_review(pdf_case, monkeypatch):
    current = session(pdf_case)
    with pytest.raises(InterruptedError):
        current.approve('Synthetic reviewer', confirmed=True, cancelled=lambda: True)
    from carr_library.ingest import worker
    monkeypatch.setattr(worker, 'isolated_render_fingerprint',
                        Mock(side_effect=RuntimeError('Renderer unavailable')))
    with pytest.raises(RuntimeError, match='Renderer unavailable'):
        current.approve('Synthetic reviewer', confirmed=True)
    assert reviews(pdf_case) == ()


@pytest.mark.parametrize('kind', ['txt', 'docx'])
def test_logical_segments_cannot_receive_pdf_review(tmp_path, kind):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Unpaginated sources')
    source = tmp_path / ('original.' + kind)
    if kind == 'txt':
        source.write_text('Unpaginated railway research.', encoding='utf-8')
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Unpaginated railway research.')
        document.save(source)
    assert app._import_file(workspace, source)['state'] == 'complete'
    hit, = app.search(workspace, 'railway')
    with pytest.raises(ValueError, match='physical page'):
        PdfReviewSession(app, workspace, hit.ref, hit.revision)
    with app.read(workspace) as db:
        assert db.execute('SELECT COUNT(*) FROM source_page_review').fetchone()[0] == 0


def frame_for(case):
    panel = SimpleNamespace(selected=Mock(return_value=case.hit), _context_epoch=1,
        model=SimpleNamespace(workspace=case.workspace, clear=Mock()), results={case.hit.ref: case.hit},
        grid=SimpleNamespace(control=SimpleNamespace(SetFocus=Mock()), refresh=Mock()),
        details=SimpleNamespace(ChangeValue=Mock()), status=SimpleNamespace(ChangeValue=Mock()))
    return SimpleNamespace(library=case.app, book=SimpleNamespace(GetCurrentPage=lambda: panel),
        active=(case.workspace, None), working=False, cancel_event=Event(), report=Mock(),
        status=SimpleNamespace(SetFocus=Mock()), run_job=Mock()), panel


def test_dialog_cancel_restores_result_focus_without_saving(pdf_case):
    frame, panel = frame_for(pdf_case)
    factory = Mock(return_value=SimpleNamespace(show=lambda: None))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    frame.run_job.assert_not_called()
    panel.grid.control.SetFocus.assert_called_once()
    assert reviews(pdf_case) == ()


def test_scope_change_while_dialog_open_does_not_restore_old_focus_or_save(pdf_case):
    frame, panel = frame_for(pdf_case)

    def close():
        frame.active = ('other-library', None)
        return None

    factory = Mock(return_value=SimpleNamespace(show=close))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    panel.grid.control.SetFocus.assert_not_called()
    frame.status.SetFocus.assert_called_once()
    frame.run_job.assert_not_called()


def test_changed_result_after_confirmation_is_rejected_before_worker(pdf_case):
    frame, panel = frame_for(pdf_case)

    def confirm():
        panel._context_epoch += 1
        return 'Synthetic reviewer'

    factory = Mock(return_value=SimpleNamespace(show=confirm))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    frame.run_job.assert_not_called()
    assert 'selected result changed' in frame.report.call_args.args[0]
    assert reviews(pdf_case) == ()


def test_panel_bound_to_another_topic_cannot_open_review(pdf_case):
    frame, panel = frame_for(pdf_case)
    frame.active = (pdf_case.workspace, 'new-topic')
    panel.bound_topic = 'old-topic'
    factory = Mock()
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    factory.assert_not_called()
    frame.run_job.assert_not_called()
    assert 'different research topic' in frame.report.call_args.args[0]
    assert reviews(pdf_case) == ()


def test_same_ids_in_another_data_root_cannot_use_old_dialog(pdf_case):
    frame, panel = frame_for(pdf_case)

    def confirm_after_root_change():
        # Scope, IDs, controls and epoch deliberately stay unchanged. A data
        # directory switch must invalidate the dialog by host identity alone.
        frame.library = LibraryApp(pdf_case.app.root.parent / 'another-root')
        return 'Synthetic reviewer'

    factory = Mock(return_value=SimpleNamespace(show=confirm_after_root_change))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    frame.run_job.assert_not_called()
    assert 'library data location changed' in frame.report.call_args.args[0]
    assert reviews(pdf_case) == ()


def test_root_switch_after_queueing_cannot_write_the_old_library(pdf_case):
    frame, panel = frame_for(pdf_case)
    factory = Mock(return_value=SimpleNamespace(show=lambda: 'Synthetic reviewer'))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    _, operation, _ = frame.run_job.call_args.args
    frame.library = LibraryApp(pdf_case.app.root.parent / 'new-root')
    with pytest.raises(ValueError, match='changed before the operation'):
        operation()
    assert reviews(pdf_case) == ()


def test_successful_review_invalidates_stale_result_labels(pdf_case):
    frame, panel = frame_for(pdf_case)
    factory = Mock(return_value=SimpleNamespace(show=lambda: 'Synthetic reviewer'))
    SourceReviewActions(frame, SimpleNamespace(), dialog_factory=factory).review()
    _, operation, finished = frame.run_job.call_args.args
    finished(operation())
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)
    assert panel.results == {} and panel._context_epoch == 2
    panel.model.clear.assert_called_once()
    assert 'Printed page numbering was not approved' in frame.report.call_args.args[0]


def fake_wx():
    wx = SimpleNamespace(ID_OK=1, ID_CANCEL=2, ID_YES=3, ID_NO=4, VERTICAL=8,
        HORIZONTAL=16, TE_MULTILINE=32, TE_READONLY=64, EXPAND=128, ALL=256,
        LEFT=512, RIGHT=1024, EVT_BUTTON='button', EVT_TEXT='text',
        EVT_CHECKBOX='checkbox', YES_NO=2048, NO_DEFAULT=4096, ICON_WARNING=8192)
    wx.created = []

    class Control:
        def __init__(self, *args, **kwargs):
            self.value = kwargs.get('value', '')
            self.label = kwargs.get('label', '')
            self.enabled, self.default, self.focused = True, False, False
            self.modal_result = wx.ID_CANCEL
            wx.created.append(self)

        def SetValue(self, value): self.value = value
        def GetValue(self): return self.value
        def SetName(self, value): self.name = value
        def Enable(self, enabled): self.enabled = enabled
        def SetDefault(self): self.default = True
        def SetFocus(self): self.focused = True
        def EndModal(self, result): self.modal_result = result
        def ShowModal(self): return self.modal_result
        def Destroy(self): self.destroyed = True
        def SetEscapeId(self, value): self.escape_id = value
        def SetSizer(self, value): pass
        def SetInsertionPoint(self, value): pass
        def SetMaxLength(self, value): pass
        def Add(self, *args, **kwargs): pass
        def Bind(self, *args, **kwargs): pass

    for name in ('Dialog', 'TextCtrl', 'StaticText', 'Button', 'CheckBox', 'BoxSizer'):
        setattr(wx, name, Control)
    return wx


def test_native_dialog_defaults_to_cancel_and_unconfirmed(pdf_case):
    wx = fake_wx()
    dialog = ReviewDialog(wx, object(), session(pdf_case), Mock())
    assert dialog.reviewer.GetValue() == ''
    assert dialog.confirmed.GetValue() is False
    assert dialog.record.enabled is False
    assert [c.label for c in wx.created if c.default] == ['&Leave unchanged']
    assert dialog.dialog.escape_id == wx.ID_CANCEL
    dialog.reviewer.SetValue('Synthetic reviewer')
    dialog._update()
    assert not dialog.record.enabled
    dialog._accept()
    assert dialog.decision is None and dialog.status.focused
    assert dialog.show() is None and reviews(pdf_case) == ()


def test_dialog_accept_still_checks_source_before_returning_confirmation(pdf_case):
    wx = fake_wx()
    guard = Mock(side_effect=ValueError('Source selection changed'))
    dialog = ReviewDialog(wx, object(), session(pdf_case), guard)
    dialog.reviewer.SetValue('Synthetic reviewer')
    dialog.confirmed.SetValue(True)
    dialog._update()
    assert dialog.record.enabled
    dialog._accept()
    assert dialog.decision is None and dialog.status.GetValue() == 'Source selection changed'
    assert reviews(pdf_case) == ()


def test_opening_new_viewing_copy_resets_previous_confirmation(pdf_case):
    wx = fake_wx()
    wx.LaunchDefaultApplication = Mock(return_value=True)
    dialog = ReviewDialog(wx, object(), session(pdf_case), Mock())
    dialog.reviewer.SetValue('Synthetic reviewer')
    dialog.confirmed.SetValue(True)
    dialog._update()
    assert dialog.record.enabled
    dialog._open_original()
    assert dialog.confirmed.GetValue() is False and not dialog.record.enabled
    opened = Path(wx.LaunchDefaultApplication.call_args.args[0])
    assert opened.parent == pdf_case.app.root / 'source-views'
    assert reviews(pdf_case) == ()


def test_revocation_dialog_has_no_default_and_cancel_keeps_review(pdf_case):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    frame, panel = frame_for(pdf_case)
    wx = fake_wx()
    native = SimpleNamespace(ShowModal=lambda: wx.ID_NO, Destroy=Mock())
    wx.MessageDialog = Mock(return_value=native)
    SourceReviewActions(frame, wx).revoke()
    assert wx.MessageDialog.call_args.args[-1] & wx.NO_DEFAULT
    frame.run_job.assert_not_called()
    panel.grid.control.SetFocus.assert_called_once()
    native.Destroy.assert_called_once()
    assert reviews(pdf_case) == (('Synthetic reviewer', 1),)


def test_native_revocation_action_can_withdraw_after_original_disappears(pdf_case):
    session(pdf_case).approve('Synthetic reviewer', confirmed=True)
    pdf_case.app.assets.resolve(pdf_case.imported['asset']).unlink()
    frame, panel = frame_for(pdf_case)
    wx = fake_wx()
    native = SimpleNamespace(ShowModal=lambda: wx.ID_YES, Destroy=Mock())
    wx.MessageDialog = Mock(return_value=native)
    SourceReviewActions(frame, wx).revoke()
    _, operation, complete = frame.run_job.call_args.args
    complete(operation())
    assert reviews(pdf_case) == (('Synthetic reviewer', 0),)
    panel.model.clear.assert_called_once()
    assert 'revoked' in frame.report.call_args.args[0]
