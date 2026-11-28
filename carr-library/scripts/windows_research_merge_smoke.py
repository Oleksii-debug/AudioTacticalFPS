"""Actual native review of a bounded research-content merge.

Only authored folder/checksum/resource answers and a hold after a genuine final
restore are controlled. Preview, conflicts, original sources, canonical graph,
native keys, Desktop worker and all publication/withdrawal outcomes are real.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace as N
import traceback


def sibling(name, identity):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(identity, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps(dict(schema='carr-native-research-merge/1', result='NOT_RUN',
                              checks=0, reason='Actual native Windows is required.')), flush=True)
        return 77
    if not args.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        result = run_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
            budget=AudioBudget(timeout=540, max_log_bytes=2 * 1024 * 1024),
            owned_tree=True, allow_failure=True)
        sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
        sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
        return result.returncode
    faulthandler.dump_traceback_later(510, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return native()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, durable_state, library_backup, backup_budget, research_merge
    from carr_library.application import LibraryApp
    from carr_library.catalog import intake
    from carr_library.ingest import retained_assets, catalog_asset_copy
    from carr_library.operations import broker
    from carr_library.research import academic_library
    from carr_library.storage import revisions, publication, schema_identity
    from carr_library.ui import desktop_app, research_merge_dialog, backup_actions
    from carr_library.ui import backup_incremental_dialog, backup_resources, command_registry, keyboard_settings
    from carr_library.audio import cleanup, process

    binding = sibling('windows_publishing_smoke.py', '_research_merge_bindings')
    helpers = sibling('windows_backup_import_access_smoke.py', '_research_merge_owned_modals')
    modules = (application, durable_state, library_backup, backup_budget, research_merge, intake,
        retained_assets, catalog_asset_copy, broker, academic_library, revisions, publication,
        schema_identity, desktop_app, research_merge_dialog, backup_actions,
        backup_incremental_dialog, backup_resources, command_registry, keyboard_settings, cleanup, process)
    identities = binding.verify_installed_sources(*modules)
    script_paths = tuple(Path(__file__).with_name(name) for name in (
        Path(__file__).name, 'windows_publishing_smoke.py', 'windows_backup_import_access_smoke.py',
        'research_merge_native_oracle.json'))
    scripts = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths}
    checks, business, errors, jobs, callbacks, peers = [], [], [], [], [], []
    frame = timer = failure_handler = None
    hold = N(armed=False, ready=threading.Event(), release=threading.Event(), result=None)
    state = N(waiting=None, description='', entered=False, finished=False, started=monotonic())

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    def criterion(label, condition):
        check(label, condition)
        if label in business:
            raise AssertionError('A business oracle was repeated.')
        business.append(label)

    def failed(detail):
        errors.append(detail)
        print('FAIL: research merge\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            if failure_handler is not None:
                failure_handler(traceback.format_exc())
            else:
                failed(traceback.format_exc())
                if timer is not None:
                    timer.Stop()
                hold.release.set()
                if frame:
                    frame.cancel_event.set()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def inside(peer, parent):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is parent:
                return True
            peer = peer.GetParent()
        raise RuntimeError('The actual native owner chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise RuntimeError('The actual modal inventory exceeds its bound.')
        modals = [item for item in windows if item and isinstance(item, wx.Dialog) and item.IsModal()
                  and (frame and inside(item, frame) or any(item is row.dialog for row in peers))]
        hidden = [item for item in modals if not item.IsShown()]
        if hidden:
            chosen = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focused) if focused else None
            active = [item for item in modals if item.IsEnabled()]
            chosen = next((item for item in active if item is top), None)
            if chosen is None:
                if len(active) != 1:
                    raise RuntimeError('The owned modal is still returning.')
                chosen = active[0]
        else:
            return ()
        return (N(dialog=chosen, close=lambda value=chosen: value.EndModal(wx.ID_CANCEL)),)

    abort = helpers._OwnedModalAbort(wx, gui, owners, failed)
    def select_failure(value):
        nonlocal failure_handler
        failure_handler = value
    helpers._nested_modal_failure_acceptance(wx, gui, check, select_failure)

    def pump(predicate, description, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A genuine native callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('the native control is shown and enabled',
              bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'The native merge control did not receive foreground focus.')
        check('the complete native control is inside its parent viewport',
              control.GetParent().GetClientRect().Contains(control.GetRect()))

    def key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier)); pressed.append(modifier)
            check('native key down', simulator.KeyDown(code)); pressed.append(code)
            sleep(.015)
        finally:
            for item in reversed(pressed):
                check('native key released', simulator.KeyUp(item))

    def press(control):
        focus(control)
        key(wx.WXK_SPACE)

    def waiting(predicate, description, seconds=30):
        return predicate, description, monotonic() + seconds

    def inventory(directory):
        return {p.relative_to(directory).as_posix(): sha256(p.read_bytes()).hexdigest()
                for p in sorted(directory.rglob('*')) if p.is_file()}

    root = durable_state.local_path(Path(mkdtemp(prefix='carr-native-research-merge-')), exists=True)
    source_app = LibraryApp(root / 'authored-input')
    shared = source_app.create_workspace('Shared exact research')
    topic = source_app.create_topic(shared, 'Exact topic')
    raw = 'Needle Україна e\u0301 — Žilina.\r\nExact original.\fSecond sealed source location.'
    imported = source_app.add_text(shared, 'Original title', raw, language='uk')
    hit, = source_app.search(shared, 'Needle')
    evidence = source_app.save_hit(shared, topic, hit.ref, hit.revision, 'LEFT exact note')
    source_app.set_evidence_tags(shared, topic, evidence, ('tag one', 'tag two'))
    query = source_app.save_query(shared, topic, 'Exact saved query', 'Needle')
    with source_app.write(shared) as db:
        reference = academic_library.AcademicLibrary(db, shared).create(
            title='Exact original academic reference', authors=('Jane Author',),
            edition_id=hit.edition_id, year='2026', notes='Unchanged bibliographic record')
    source_app.update_metadata(shared, hit.edition_id, title='Current retained title',
                               expected_metadata=source_app.edition_metadata(shared, hit.edition_id))
    left_only = source_app.create_workspace('Unchanged identical workspace')
    left_raw = 'Exact left-only source Ω.'
    source_app.add_text(left_only, 'Left original', left_raw)
    broker.ResourceBroker.for_app(source_app.root)
    left = root / 'left-backup'
    left_hash = library_backup.create_backup(source_app, left)
    with source_app.write(shared) as db:
        db.execute('UPDATE research_evidence SET note=? WHERE workspace=? AND topic=? AND id=?',
                   ('RIGHT exact note', shared, topic, evidence))
    right_only = source_app.create_workspace('New right workspace')
    right_raw = 'Exact right-only source 😀.'
    source_app.add_text(right_only, 'Right original', right_raw)
    right = root / 'right-backup'
    right_hash = library_backup.create_backup(source_app, right)
    expected_anchor = source_app.evidence(shared, topic)[0].anchor
    original_inventory = inventory(left), inventory(right)
    check('the authored input cursor is released before native work', source_app.close_search())
    current = LibraryApp(root / 'current-library')
    current_workspace = current.create_workspace('Current library must stay selected')
    current_before = current.workspaces()
    frame = desktop_app.DesktopFrame(current)
    frame.SetSize((1180, 980))
    frame.Show()
    gui.SetTopWindow(frame)
    executor = frame.executor
    actions = frame.research_merge_actions
    actual_submit, actual_run_job = executor.submit, frame.run_job
    actual_factory = research_merge_dialog.create_merge_review
    actual_merge = research_merge.ResearchContentMerge.merge
    output_parent = root / 'outputs'
    output_parent.mkdir()

    class Answers(research_merge_dialog.NativeDialogs):
        def __init__(self, owner):
            super().__init__(owner)
            self.folders, self.hashes = [], []
            self.reports = []
        def reset(self):
            self.folders = [left, right, output_parent]
            self.hashes = [left_hash, right_hash]
        def options(self, owner, action):
            check('the actual action requests explicit restore resource policy', action == 'restore' and owner.current())
            return N(budget=None, include_generated=False)
        def folder(self, title):
            check('one authored folder answer is consumed explicitly', bool(self.folders))
            return self.folders.pop(0)
        def checksum(self, owner):
            check('one independently recorded input checksum is consumed explicitly', owner.current() and bool(self.hashes))
            return self.hashes.pop(0)
        def show(self, title, body, owner):
            self.reports.append((title, body))
            return super().show(title, body, owner)

    answers = Answers(frame)
    actions.dialogs = answers
    def submit(operation, *args, **kwargs):
        record = N(thread=None, future=None)
        def observed():
            record.thread = threading.get_ident()
            return operation(*args, **kwargs)
        record.future = actual_submit(observed)
        jobs.append(record)
        return record.future
    executor.submit = submit

    def run_job(title, operation, done=None, failed=None, **kwargs):
        callbacks.append(N(title=title, done=done, failed=failed))
        return actual_run_job(title, operation, done=done, failed=failed, **kwargs)
    frame.run_job = run_job

    def factory(*args, **kwargs):
        dialog = actual_factory(*args, **kwargs)
        record = N(dialog=dialog, native_id=dialog.GetId(), deaths=[])
        def destroyed(event):
            if event.GetId() == record.native_id:
                record.deaths.append(event.GetId())
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        peers.append(record)
        return dialog
    research_merge_dialog.create_merge_review = factory

    def merged(self, *args, **kwargs):
        result = actual_merge(self, *args, **kwargs)
        if hold.armed:
            hold.armed = False
            hold.result = result
            hold.ready.set()
            if not hold.release.wait(30):
                raise TimeoutError('The genuine completed research merge was not released.')
        return result
    research_merge.ResearchContentMerge.merge = merged

    menu_state = N(opened=0, highlighted=None)
    def menu_open(event):
        menu_state.opened += 1; event.Skip()
    def menu_highlight(event):
        menu_state.highlighted = event.GetId(); event.Skip()
    frame.Bind(wx.EVT_MENU_OPEN, menu_open)
    frame.Bind(wx.EVT_MENU_HIGHLIGHT_ALL, menu_highlight)

    def quiet():
        return (not frame or not frame.working) and all(item.future.done() for item in jobs)

    def review():
        active = [item for item in peers if item.dialog and item.dialog.IsShown() and item.dialog.IsModal()]
        if len(active) > 1:
            raise RuntimeError('More than one research preview owns a modal.')
        return active[0] if active else None

    def open_review():
        check('the same Desktop is idle before explicit merge admission', quiet() and not owners())
        answers.reset()
        bar = frame.GetMenuBar()
        menus = [bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '') == 'Library']
        check('one actual Library menu exists', len(menus) == 1)
        items = [item for item in menus[0].GetMenuItems() if not item.IsSeparator() and item.IsEnabled()]
        wanted = [i for i, item in enumerate(items) if item.GetItemLabelText() == 'Merge research content from two backups…']
        check('the actual Library command is present once', len(wanted) == 1)
        check('the same stable command is in the current palette registry',
              frame.keyboard_commands.registry.get('backup.research.merge').command_id == 'backup.research.merge')
        previous = menu_state.opened
        menu_state.highlighted = None
        focus(frame.status)
        key(ord('L'), (wx.WXK_ALT,))
        yield waiting(lambda: menu_state.opened > previous, 'The actual Library menu did not open', 8)
        key(wx.WXK_HOME)
        yield waiting(lambda: menu_state.highlighted == items[0].GetId(), 'Library Home did not select its first item', 8)
        for item in items[1:wanted[0] + 1]:
            key(wx.WXK_DOWN)
            yield waiting(lambda selected=item.GetId(): menu_state.highlighted == selected,
                          'Actual native Library navigation missed the requested item', 8)
        key(wx.WXK_RETURN)
        yield waiting(lambda: review() is not None, 'Actual backup verification did not open the exact preview', 65)
        check('every authored selection was consumed by the actual action', not answers.folders and not answers.hashes)

    def inspect_preview(record, label):
        dialog = record.dialog
        description = json.loads(actions.preview.payload)
        check(label + ': native preview is read-only and byte-exact',
              bool(dialog.details.GetWindowStyleFlag() & wx.TE_READONLY)
              and dialog.details.GetValue() == actions.preview.text)
        check(label + ': original checksums and finite profile are displayed',
              left_hash in dialog.details.GetValue() and right_hash in dialog.details.GetValue()
              and description['profile'] == 'published-research-content/1')
        check(label + ': omitted sidecars are explicit and initially unapproved',
              all(description['omitted'][side] for side in ('left', 'right'))
              and not dialog.acknowledge.GetValue() and not dialog.choices)
        check(label + ': actual workspaces identify conflict, identical and right-only cases',
              {row['state'] for row in dialog.rows} == {'conflict', 'identical', 'right_only'})
        return description

    def select_and_accept(record, side):
        dialog = record.dialog
        press(dialog.nonconflicting)
        yield waiting(lambda: dialog.choices.get(left_only) == 'left'
                      and dialog.choices.get(right_only) == 'right',
                      'Actual nonconflicting selection did not arrive')
        check('conflicting workspace is not selected by the convenience action', shared not in dialog.choices)
        index = next(i for i, row in enumerate(dialog.rows) if row['id'] == shared)
        focus(dialog.items)
        key(wx.WXK_HOME)
        for _ in range(index):
            key(wx.WXK_DOWN)
        yield waiting(lambda: dialog.items.GetSelection() == index,
                      'The actual conflict list selection did not arrive')
        press(dialog.right if side == 'right' else dialog.left)
        yield waiting(lambda: dialog.choices.get(shared) == side, 'Actual complete workspace choice did not arrive')
        press(dialog.acknowledge)
        yield waiting(dialog.acknowledge.GetValue, 'The actual omission acknowledgement did not arrive')
        press(dialog.accept)
        yield waiting(lambda: not record.dialog or not record.dialog.IsModal(),
                      'The exact review did not return before publication', 15)

    def verify_output(result, selected_note):
        app = LibraryApp(result.path)
        try:
            saved, = app.evidence(shared, topic)
            check('merged evidence keeps its exact identifier, anchor and selected whole-workspace note',
                  saved.evidence_id == evidence and saved.anchor == expected_anchor and saved.note == selected_note)
            check('evidence tags and saved query survive the actual restore',
                  app.evidence_tags(shared, topic, evidence) == ('tag one', 'tag two')
                  and bool(app.run_query(shared, topic, query)))
            for workspace, text in ((shared, raw.split('\f')[0]), (left_only, left_raw), (right_only, right_raw)):
                hit, = app.search(workspace, text)
                check('actual current source copy keeps full Unicode and exact scope',
                      hit.verified and app.copy(workspace, hit.ref, hit.revision, 'quote') == text)
            with app.read() as db:
                check('history, sealed pages and active index survive the canonical rebuilt graph',
                      db.execute('SELECT count(*) FROM workspace').fetchone()[0] == 3
                      and db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 4
                      and db.execute('SELECT count(*) FROM page').fetchone()[0] == 6
                      and db.execute('SELECT count(*) FROM indexed').fetchone()[0] == 4)
                check('academic source links retain their exact public record',
                      academic_library.AcademicLibrary(db, shared).get(reference.id) == reference)
            check('only research originals survive and operational state is omitted',
                  len(tuple(app.assets_dir.iterdir())) == 3
                  and not (app.root / 'state/operations/resources.json').exists()
                  and not (app.root / 'imports.sqlite').exists()
                  and not (app.root / library_backup.INCOMPLETE).exists())
        finally:
            check('actual restored search resources confirm cleanup', app.close_search())

    def result_dialog():
        return next((item for item in wx.GetTopLevelWindows() if item and isinstance(item, wx.Dialog)
                     and item.IsModal() and item.GetTitle() == 'New research library created'), None)

    def scenario():
        criterion('actual_library_menu_and_exact_review', wx.Platform == '__WXMSW__' and bool(identities))
        yield from open_review()
        record = review()
        inspect_preview(record, 'cancel')
        destination = actions.action.destination
        press(record.dialog.back)
        yield waiting(lambda: not record.dialog and quiet(), 'The native cancelled preview did not return')
        criterion('native_cancel_publishes_nothing', not destination.exists() and actions.token is None)

        yield from open_review()
        record = review()
        inspect_preview(record, 'destroy')
        destination = actions.action.destination
        retained_confirm = record.dialog.confirm
        record.dialog.Destroy()
        yield waiting(lambda: not record.dialog and quiet(), 'Direct native preview deletion did not settle')
        retained_confirm()
        criterion('direct_delete_revokes_queued_confirmation',
                  not destination.exists() and actions.token is None and not owners())

        yield from open_review()
        record = review()
        inspect_preview(record, 'publish-right')
        before_jobs = len(jobs)
        press(record.dialog.accept)
        yield waiting(lambda: 'acknowledge' in record.dialog.status.GetLabel().lower()
                      or 'choice' in record.dialog.status.GetLabel().lower(),
                      'The native unapproved selection was not refused')
        criterion('exact_choices_and_omissions_are_required',
                  record.dialog.IsModal() and len(jobs) == before_jobs
                  and not actions.action.destination.exists())
        yield from select_and_accept(record, 'right')
        yield waiting(lambda: result_dialog() is not None and actions.result is not None,
                      'The genuine merged library and native completion report did not arrive', 65)
        first_result = actions.result
        report = result_dialog()
        texts = [child.GetValue() for child in report.GetChildren() if isinstance(child, wx.TextCtrl)]
        check('the actual read-only completion body matches the genuine result dialog',
              len(texts) == 1 and not next(child for child in report.GetChildren()
                                           if isinstance(child, wx.TextCtrl)).IsEditable()
              and texts[0] == answers.reports[-1][1])
        # The native report contains JSON: Windows path backslashes are escaped
        # in its displayed text and must be decoded before exact comparison.
        displayed = json.loads(texts[0].partition('\n\n')[2])
        criterion('actual_final_report_matches_committed_result',
                  displayed == dict(path=str(first_result.path), profile=first_result.profile,
                      preview_sha256=first_result.preview_sha256,
                      choices=[list(item) for item in first_result.choices],
                      omitted=[[side, list(items)] for side, items in first_result.omitted]))
        press(report.FindWindow(wx.ID_CANCEL))
        yield waiting(lambda: not report and quiet(), 'The genuine completion report did not return')
        verify_output(first_result, 'RIGHT exact note')
        criterion('right_conflict_preserves_graph_history_and_sources',
                  dict(first_result.choices)[shared] == 'right' and frame.library is current
                  and current.workspaces() == current_before)

        yield from open_review()
        record = review()
        inspect_preview(record, 'late-completed')
        hold.ready.clear(); hold.release.clear(); hold.result = None; hold.armed = True
        yield from select_and_accept(record, 'left')
        yield waiting(hold.ready.is_set, 'The real final restore did not reach its post-commit hold', 65)
        returned = hold.result
        callback = callbacks[-1].done
        criterion('completed_restore_is_held_by_the_existing_worker',
                  returned.path.is_dir() and not jobs[-1].future.done() and frame.working
                  and frame.executor is executor)
        frame.Close(force=True)
        yield waiting(lambda: not frame and actions.withdrawn.is_set(),
                      'Forced native Desktop closure did not withdraw the exact owner')
        hold.release.set()
        yield waiting(lambda: all(item.future.done() for item in jobs),
                      'The existing worker did not release the genuine committed result', 30)
        callback(returned)
        criterion('late_committed_receipt_survives_dead_native_owner',
                  actions.result is returned and actions.token is None and not frame and not owners())
        verify_output(returned, 'LEFT exact note')
        criterion('left_conflict_preserves_its_complete_original_snapshot', dict(returned.choices)[shared] == 'left')
        criterion('both_full_input_backups_remain_byte_exact',
                  (inventory(left), inventory(right)) == original_inventory)
        criterion('one_existing_executor_and_exact_modal_lifetimes',
                  len(peers) == 4 and all(not item.dialog and item.deaths == [item.native_id] for item in peers)
                  and all(item.thread != threading.get_ident() and item.future.done() for item in jobs))
        criterion('all_owned_private_resources_are_released',
                  current.close_search() and source_app.close_search()
                  and not list(output_parent.glob('.carr-research-merge-*')))
        state.finished = True

    sequence = scenario()
    timer = wx.Timer(gui)
    def drive(event=None):
        if state.entered or state.finished:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 450:
                raise TimeoutError('The actual native merge scenario exceeded its finite bound.')
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(sequence)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('The scenario ended before its complete oracle.')
                timer.Stop()
                gui.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            timer.Stop()
            failed(traceback.format_exc() + '\nWaiting: ' + state.description)
            hold.release.set()
            if frame:
                frame.cancel_event.set()
            abort.request()
        finally:
            state.entered = False

    gui.Bind(wx.EVT_TIMER, drive, timer)
    successful = False
    try:
        timer.Start(25)
        gui.MainLoop()
        if errors or not state.finished:
            raise RuntimeError('Native research merge failed; see retained failure evidence.')
        check('every selected installed source remains exact', binding.verify_installed_sources(*modules) == identities)
        check('all actual probe and reused helper bytes remain exact',
              {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths} == scripts)
        successful = True
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        hold.release.set()
        research_merge_dialog.create_merge_review = actual_factory
        research_merge.ResearchContentMerge.merge = actual_merge
        executor.submit = actual_submit
        if frame:
            frame.run_job = actual_run_job
            frame._closing = True
            frame.cancel_event.set()
        executor.shutdown(wait=True, cancel_futures=False)
        if not current.close_search() or not source_app.close_search():
            raise RuntimeError('A current library resource has not confirmed cleanup.')
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
        if successful:
            shutil.rmtree(root)
        else:
            print('RETAINED_NATIVE_RESEARCH_MERGE_FIXTURE ' + str(root), file=sys.stderr, flush=True)

    print(json.dumps(dict(schema='carr-native-research-merge/1', result='PASS',
        checks=business, native_checks=len(checks), native_keyboard='PASS',
        exact_research_graph='PASS', whole_workspace_conflicts='PASS',
        reviewed_omissions='PASS', current_library_unchanged='PASS',
        actual_cancel_and_owner_withdrawal='PASS', unchanged_input_backups='PASS',
        same_desktop_executor='PASS', source_identity=identities, probe_and_binding=scripts,
        native_file_picker='NOT_RUN', NVDA='NOT_RUN', physical_keyboard='NOT_RUN',
        controlled_seams=['authored folder/checksum/resource answers',
                          'hold after a genuine completed final restore']),
        ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
