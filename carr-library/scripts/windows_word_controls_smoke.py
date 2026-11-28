"""Actual wx Word controls with an explicitly controlled external Word host.

Current library/CSL/DOCX publication, formatter modal handoff, native widgets
and keys, the existing Desktop executor and lifecycle are real. Only the
external Word host and file-picker answers are controlled. This is not actual
Office, native picker, production COM attachment or NVDA qualification.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import faulthandler
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from time import monotonic, sleep
from types import SimpleNamespace as N
import traceback

from carr_library.academic_integration import word_com as word
from carr_library.academic_integration.documents import (
    CitationParagraph, _citation_xml, _read_citation_xml, read_citation_docx, export_citation_docx)

class ControlledDocument:
    """Only Word is controlled; real source checks, CSL and publication run."""
    next_window = 100

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.state = read_citation_docx(self.path)
        self.saved, self.editable, self.managed = True, True, False
        self.generation, self.applied, self.adopted = 0, [], 0
        self.before_paragraphs = lambda: None
        self.selection_supported = True
        self.after_apply = lambda: None
        self.worker_threads = []
        type(self).next_window += 1
        self.window = type(self).next_window

    def owner(self):
        return (7, self.window, str(self.path))

    def snapshot(self):
        return deepcopy(self.state)

    def require_editable(self, *, saved=False):
        if not self.editable or (saved and not self.saved):
            raise ValueError('Save an editable document first.')

    def signature(self, *, selection=True):
        return sha256(json.dumps([self.state, self.generation, self.managed],
                                 sort_keys=True).encode()).hexdigest()

    def preflight(self, snapshot):
        self.require_editable()
        assert snapshot == self.state

    def adopt(self, snapshot, *, cancelled):
        word._check(cancelled)
        self.preflight(snapshot)
        self.managed = True
        self.adopted += 1

    def capture(self, snapshot, *, insertion=False):
        if not self.managed or snapshot != self.state:
            raise ValueError('The actual controlled visible citations differ from metadata.')
        if insertion and not self.selection_supported:
            raise ValueError('Place a collapsed caret in ordinary managed body text.')
        anchors = tuple((identity, index * 100 + 10, index * 100 + 20)
                        for index, identity in enumerate(word._ids(snapshot)))
        return dict(ids=word._ids(snapshot), anchors=anchors, caret=10_000)

    def paragraphs(self, snapshot, *, insertion=None):
        self.before_paragraphs()
        result = [CitationParagraph(row['text'], tuple(row['citation_ids']))
                  for row in snapshot['document']['paragraphs']]
        if insertion is not None:
            result.append(CitationParagraph('Explicit additional prose 😀', (insertion[1],)))
        return tuple(result)

    def apply(self, previous, snapshot, *, insertion, cancelled):
        word._check(cancelled)
        assert self.state == previous
        assert _read_citation_xml(_citation_xml(snapshot).decode()) == snapshot
        self.state = deepcopy(snapshot)
        self.applied.append((previous, snapshot, insertion))
        self.worker_threads.append(threading.get_ident())
        self.after_apply()


class ControlledApplication:
    def __init__(self, source):
        self.document = ControlledDocument(source)
        self.opens = []

    def active(self):
        return self.document

    def open_copy(self, path):
        self.opens.append(Path(path))
        self.document = ControlledDocument(path)
        return self.document



def _sibling(name, label):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps(dict(schema='carr-native-word-controls/1', status='NOT_RUN',
            reason='Actual Windows desktop is required.', checks=0)), flush=True)
        return 77
    if not args.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        result = run_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
            budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
            owned_tree=True, allow_failure=True)
        sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
        sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
        return result.returncode
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _native()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, durable_state
    from carr_library.application import LibraryApp
    from carr_library.academic_integration import documents, formatting, regeneration
    from carr_library.ui import desktop_app, academic_library_dialog, citation_formatting
    from carr_library.ui import word_citations_dialog as word_ui
    from carr_library.citations import bibliography_workbench
    from carr_library.research import export_file

    binding = _sibling('windows_publishing_smoke.py', '_word_controls_bindings')
    helpers = _sibling('windows_speech_preparation_smoke.py', '_word_controls_owned_modals')
    selected = (application, durable_state, documents, formatting, regeneration, word,
        desktop_app, academic_library_dialog, citation_formatting, word_ui,
        bibliography_workbench, export_file)
    before = binding.verify_installed_sources(*selected)
    paths = tuple(Path(__file__).with_name(name) for name in (
        Path(__file__).name, 'windows_publishing_smoke.py', 'windows_speech_preparation_smoke.py'))
    scripts = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in paths}
    checks, errors, records, bridges, applications, originals = [], [], [], [], [], []
    frame = timer = failure_handler = None
    state = N(waiting=None, description='', entered=False, finished=False,
        returned=True, started=monotonic(), formatter=None)
    hold = N(armed=False, ready=threading.Event(), release=threading.Event(), mode=None)
    hold.release.set()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    def failed(detail):
        errors.append(detail)
        print('FAIL: native Word controls\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
                if timer is not None:
                    timer.Stop()
                hold.release.set()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = (wintypes.HWND,), wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = (), wintypes.HWND

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is ancestor:
                return True
            peer = peer.GetParent()
        raise ValueError('Native owner chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owner inventory exceeds its bound.')
        modals = [p for p in windows if p and isinstance(p, wx.Dialog) and p.IsModal()
                  and frame and inside(p, frame)]
        hidden = [p for p in modals if not p.IsShown()]
        if hidden:
            chosen = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focused) if focused else None
            active = [p for p in modals if p.IsEnabled()]
            chosen = next((p for p in active if p is top), None)
            if chosen is None:
                if len(active) != 1:
                    raise RuntimeError('The current owned modal is still returning.')
                chosen = active[0]
        else:
            return ()
        return (N(dialog=chosen, close=lambda value=chosen: value.EndModal(wx.ID_CANCEL)),)

    abort = helpers._OwnedModalAbort(wx, gui, owners, failed)
    def select_failure(value):
        nonlocal failure_handler
        failure_handler = value
    helpers._nested_modal_failure_acceptance(wx, gui, check, select_failure)

    def pump(predicate, message, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A real native callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('named control is alive, visible and enabled',
              bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle()
             and control.GetParent().GetClientRect().Contains(control.GetRect()),
             'The complete named control did not receive visible foreground focus.')

    def key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for code in reversed(pressed):
                check('native key released', simulator.KeyUp(code))

    def press(control):
        focus(control)
        key(wx.WXK_SPACE)

    def choose(control, index):
        focus(control)
        key(wx.WXK_HOME)
        for _ in range(index):
            key(wx.WXK_DOWN)
        key(wx.WXK_TAB)
        pump(lambda: control.GetSelection() == index, 'Actual native selection did not arrive.')

    def enter(control, value):
        focus(control)
        key(ord('A'), (wx.WXK_CONTROL,))
        check('native ASCII text accepted', simulator.Text(str(value).encode('ascii')))
        key(wx.WXK_TAB)
        pump(lambda: control.GetValue() == str(value), 'Actual native text did not arrive.')

    def waiting(predicate, message, seconds=60):
        return predicate, message, monotonic() + seconds

    def titled(title):
        rows = [p for p in wx.GetTopLevelWindows() if p and isinstance(p, wx.Dialog)
                and p.GetTitle() == title and p.IsShown() and p.IsModal()]
        if len(rows) > 1:
            raise ValueError('Native dialog title is ambiguous.')
        return rows[0] if rows else None

    root = durable_state.local_path(Path(tempfile.mkdtemp(prefix='carr-word-controls-')), exists=True)
    durable_state.regular(root, directory=True)
    library = LibraryApp(root / 'library')
    workspace = library.create_workspace('Actual native Word controls')
    retained = library.add_text(workspace, 'Retained source', 'Exact retained quotation 😀\r\nSecond line.')
    asset = library.assets.resolve(retained['asset'])
    raw = asset.read_bytes()
    academic = academic_library_dialog.AcademicController(library, workspace)
    academic.create(dict(title='First native reference', authors=('Smith, Jane',),
                         year='2024', edition_id=retained['edition']))
    academic.create(dict(title='Second native reference', authors=('Reader, Ana',), year='2025'))
    frame = desktop_app.DesktopFrame(library)
    frame.SetSize((1180, 980))
    frame.Show()
    gui.SetTopWindow(frame)
    frame._open_workspace(workspace)
    desktop_executor = frame.executor
    real_factory, real_bridge, real_picker = word_ui.create_word_citations_dialog, word_ui.WordBridge, wx.FileDialog

    def after_apply():
        if hold.mode == 'uncertain':
            hold.mode = None
            raise RuntimeError('Disclosed external host failed after actual proposed state acceptance.')
        if hold.armed:
            hold.armed = False
            hold.ready.set()
            if not hold.release.wait(90):
                raise TimeoutError('The disclosed external host result hold was not released.')

    def controlled_bridge(controller, snapshot, result):
        # This creates only the synthetic external host. All production library,
        # CSL, typed bridge and publication logic below remains unchanged.
        index = len(bridges)
        source = root / ('original-' + str(index) + '.docx')
        export_citation_docx(result, source,
            paragraphs=[CitationParagraph('Untouched synthetic body 😀', tuple(
                citation.cluster_id for citation in result.citations))])
        source = durable_state.local_path(source, exists=True)
        originals.append((source, source.read_bytes()))
        host = ControlledApplication(source)
        applications.append(host)
        real_open = host.open_copy
        def open_copy(path):
            actual = real_open(path)
            actual.after_apply = after_apply
            return actual
        host.open_copy = open_copy
        @contextmanager
        def connector():
            yield host
        bridge = word.WordBridge(controller, snapshot, result, connector=connector)
        bridges.append(bridge)
        return bridge

    def observed_factory(parent, *arguments, **options):
        # The actual wrapper already returned from and destroyed the formatter.
        check('Word dialog opens after the formatter modal has really returned',
              titled('Format bibliography and citations') is None
              and (state.formatter is None or not state.formatter))
        dialog = real_factory(parent, *arguments, **options)
        record = N(dialog=dialog, native_id=dialog.GetId(), deaths=[])
        def destroyed(event):
            if event.GetId() == record.native_id:
                record.deaths.append(event.GetId())
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        records.append(record)
        return dialog

    class Picker(real_picker):
        def ShowModal(self):
            check('controlled picker belongs to the explicit new-copy action',
                  self.GetMessage() == 'Choose a NEW Word working copy')
            self.SetPath(str(root / ('working-' + str(len(records) - 1) + '.docx')))
            return wx.ID_OK

    word_ui.WordBridge = controlled_bridge
    word_ui.create_word_citations_dialog = observed_factory
    wx.FileDialog = Picker

    def idle(dialog):
        return bool(dialog) and dialog.pending is None and not frame.working

    def launch_formatter():
        state.returned = False
        try:
            citation_formatting.show_citation_formatter(frame)
        finally:
            state.returned = True

    def launch_word(controller, snapshot, result):
        state.returned = False
        try:
            word_ui.show_word_citations(frame, controller, snapshot, result)
        finally:
            state.returned = True

    def scenario():
        yield waiting(lambda: frame.active is not None and frame.active[0] == workspace and not frame.working,
                      'The real desktop did not open its captured workspace.')
        wx.CallAfter(launch_formatter)
        yield waiting(lambda: titled('Format bibliography and citations') is not None,
                      'The actual formatter did not open.')
        formatter = state.formatter = titled('Format bibliography and citations')
        yield waiting(lambda: formatter.snapshot is not None and not formatter.pending,
                      'Actual reference loading did not complete.')
        press(formatter.all_button)
        yield waiting(lambda: len(formatter.references_box.GetCheckedItems()) == len(formatter.references),
                      'Actual Check all handler did not select the current references.')
        choose(formatter.style, next(i for i, row in enumerate(formatter.styles) if row.id == 'apa'))
        key(wx.WXK_TAB, (wx.WXK_CONTROL,))
        yield waiting(lambda: formatter.tabs.GetSelection() == 1, 'Actual notebook keyboard navigation failed.')
        choose(formatter.citation_reference, 0)
        press(formatter.add_citation)
        yield waiting(lambda: len(formatter.clusters) == 1,
                      'Actual Add citation handler did not create its source-bound occurrence.')
        check('actual formatter created one source-bound citation', len(formatter.clusters) == 1)
        press(formatter.format_button)
        yield waiting(lambda: formatter.result is not None and not formatter.pending,
                      'Actual current CSL format did not finish.')
        controller, snapshot, result = formatter.controller, formatter.snapshot, formatter.result
        prior_id = result.citations[0].cluster_id
        press(formatter.word_button)
        yield waiting(lambda: len(records) == 1 and records[-1].dialog.IsModal(),
                      'Actual formatter-to-Word handoff did not open controls.')
        record = records[-1]
        dialog, bridge, host = record.dialog, bridges[-1], applications[-1]
        check('opening Word controls starts no external mutation or new Desktop executor',
              not host.opens and not host.document.applied and frame.executor is desktop_executor)
        press(dialog.connect_button)
        yield waiting(lambda: idle(dialog) and dialog.copy is not None, 'Actual create-only connection did not finish.')
        copy = dialog.copy
        check('real publisher creates exactly the new captured DOCX copy',
              copy.path.read_bytes() == originals[0][1] and len(host.opens) == 1
              and bridge.completed_result(copy).citation_ids == (prior_id,))
        choose(dialog.reference, 1)
        enter(dialog.locator, '12')
        prior_status = dialog.status.GetValue()
        press(dialog.insert_button)
        yield waiting(lambda: dialog.status.GetValue() != prior_status
                      and 'Check this exact locator' in dialog.status.GetValue(),
                      'Actual unchecked insertion handler did not report its refusal.')
        check('unchecked typed locator refuses before external mutation',
              not host.document.applied and 'Check this exact locator' in dialog.status.GetValue())
        press(dialog.locator_reviewed)
        yield waiting(dialog.locator_reviewed.GetValue, 'Actual locator review checkbox was not accepted.')
        press(dialog.insert_button)
        yield waiting(lambda: idle(dialog) and len(host.document.applied) == 1, 'Actual typed insertion did not finish.')
        inserted = bridge.completed_result(copy).citation_ids
        check('native insertion preserves old occurrence and explicit source locator',
              inserted[0] == prior_id and len(inserted) == 2
              and host.document.state['clusters'][-1]['items'][0]['locator'] == '12'
              and host.document.state['clusters'][-1]['locator_evidence'][0]['locator_verified'] is True)
        choose(dialog.occurrence, 1)
        enter(dialog.locator, '14')
        check('real locator text event withdraws its previous review', not dialog.locator_reviewed.GetValue())
        prior_status = dialog.status.GetValue()
        press(dialog.edit_button)
        yield waiting(lambda: dialog.status.GetValue() != prior_status
                      and 'Check this exact locator' in dialog.status.GetValue(),
                      'Actual changed-locator edit handler did not report its new refusal.')
        check('changed unchecked locator causes no second host mutation', len(host.document.applied) == 1)
        press(dialog.locator_reviewed)
        yield waiting(dialog.locator_reviewed.GetValue, 'Actual replacement locator review was not accepted.')
        press(dialog.edit_button)
        yield waiting(lambda: idle(dialog) and len(host.document.applied) == 2, 'Actual typed replacement did not finish.')
        check('native edit keeps stable occurrence IDs', bridge.completed_result(copy).citation_ids == inserted)
        press(dialog.refresh_button)
        yield waiting(lambda: idle(dialog) and len(host.document.applied) == 3, 'Actual bibliography refresh did not finish.')
        check('actual refresh retains source bytes and never implicitly saves Word',
              bridge.completed_result(copy).citation_ids == inserted
              and copy.path.read_bytes() == originals[0][1] and asset.read_bytes() == raw)
        hold.mode = 'uncertain'
        press(dialog.refresh_button)
        yield waiting(lambda: idle(dialog) and bridge.recovery_required, 'Unknown external result did not stay blocked.')
        check('uncertain host outcome blocks replay and keeps explicit inspect reachable',
              not dialog.insert_button.IsEnabled() and not dialog.edit_button.IsEnabled()
              and not dialog.refresh_button.IsEnabled() and dialog.inspect_button.IsEnabled())
        count = len(host.document.applied)
        press(dialog.inspect_button)
        yield waiting(lambda: idle(dialog) and not bridge.recovery_required, 'Actual current-state inspection did not settle.')
        check('explicit inspect reconciles the issued result without another mutation',
              len(host.document.applied) == count and bridge.completed_result(copy).citation_ids == inserted)
        hold.ready.clear(); hold.release.clear(); hold.armed = True
        press(dialog.refresh_button)
        yield waiting(hold.ready.is_set, 'The genuine completed external result did not reach its controlled hold.')
        token = dialog.pending
        check('one actual Desktop worker retains the pending host result',
              token is not None and frame.working and frame.executor is desktop_executor)
        press(dialog.close_button)
        yield waiting(lambda: dialog.closing and dialog.cancelled.is_set(),
                      'Actual native Close did not request cancellation before hold release.')
        check('native Close waits for real worker return without destroying its owner',
              dialog.pending is token and dialog.closing and dialog.IsModal() and not record.deaths)
        hold.release.set()
        yield waiting(lambda: state.returned and not dialog and not frame.working,
                      'Late completed host result did not safely close the actual dialog.')
        check('actual close receives one destruction and keeps the completed result',
              record.deaths == [record.native_id] and not bridge.recovery_required
              and bridge.completed_result(copy).citation_ids == inserted)
        wx.CallAfter(launch_word, controller, snapshot, result)
        yield waiting(lambda: len(records) == 2 and records[-1].dialog.IsModal(),
                      'The current reviewed controls did not reopen.')
        record = records[-1]
        dialog, bridge, host = record.dialog, bridges[-1], applications[-1]
        press(dialog.connect_button)
        yield waiting(lambda: idle(dialog) and dialog.copy is not None, 'Second exact new copy did not connect.')
        hold.ready.clear(); hold.release.clear(); hold.armed = True
        press(dialog.refresh_button)
        yield waiting(hold.ready.is_set, 'Second genuine host result did not reach its hold.')
        token, settle = dialog.pending, dialog.settle
        dialog.Destroy()
        yield waiting(lambda: state.returned and not dialog, 'Direct native child deletion did not return its modal.')
        check('dead native peer leaves the actual Desktop worker retained',
              frame.working and dialog.pending is token and record.deaths == [record.native_id])
        hold.release.set()
        yield waiting(lambda: dialog.pending is None and not frame.working,
                      'Actual worker after native deletion did not settle its Python owner.')
        delivered = []
        def replay():
            settle(token, bridge.completed_result(bridge.connected_copy), None)
            dialog.Destroy()
            delivered.append(True)
        wx.CallAfter(replay)
        yield waiting(lambda: delivered == [True], 'Withdrawn late native delivery did not return.')
        check('late replay never destroys another native peer or starts a host mutation',
              record.deaths == [record.native_id] and len(host.document.applied) == 1
              and not bridge.recovery_required)
        check('every Word host mutation ran on the existing worker rather than GUI',
              all(doc.document.worker_threads for doc in applications)
              and all(thread_id != threading.get_ident() for doc in applications
                      for thread_id in doc.document.worker_threads)
              and frame.executor is desktop_executor)
        check('every original and retained source stays byte-exact',
              all(path.read_bytes() == payload for path, payload in originals)
              and asset.read_bytes() == raw and controller.load().sha256 == snapshot.sha256)
        state.finished = True
        gui.ExitMainLoop()

    generator = scenario()
    def tick(event):
        if state.entered:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 270:
                raise TimeoutError('Native Word-controls scenario expired: ' + state.description)
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(generator)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('The actual native scenario ended without its final proof.')
        except (Exception, KeyboardInterrupt):
            failed(traceback.format_exc())
            timer.Stop()
            hold.release.set()
            abort.request()
        finally:
            state.entered = False

    timer = wx.Timer(frame)
    frame.Bind(wx.EVT_TIMER, tick, timer)
    timer.Start(25)
    try:
        gui.MainLoop()
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        hold.release.set()
        word_ui.create_word_citations_dialog = real_factory
        word_ui.WordBridge = real_bridge
        wx.FileDialog = real_picker
        if frame:
            frame._closing = True
            frame.cancel_event.set()
        desktop_executor.shutdown(wait=True, cancel_futures=False)
        if not library.close_search():
            raise RuntimeError('The native Word-controls library did not release its cursor owner.')
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
    if errors or not state.finished:
        print(json.dumps(dict(schema='carr-native-word-controls/1', status='FAIL',
            checks=len(checks), errors=errors, fixture=str(root), waiting=state.description)), flush=True)
        return 1
    check('all selected installed sources remain exact', binding.verify_installed_sources(*selected) == before)
    check('every native script and modal helper remains exact',
          {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in paths} == scripts)
    shutil.rmtree(root)
    print(json.dumps(dict(schema='carr-native-word-controls/1', status='PASS',
        checks=len(checks), check_names=checks, selected_sources=before, scripts=scripts,
        source_sha256=sha256(raw).hexdigest(), actual_native_word_controls='PASS',
        actual_office='NOT_RUN', external_word='CONTROLLED_PROTOCOL_BOUNDARY',
        picker_answers='CONTROLLED', native_picker_parent_death='NOT_RUN',
        actual_com_attachment='NOT_RUN', NVDA='NOT_RUN'), ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
