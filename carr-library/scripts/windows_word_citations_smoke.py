"""Actual wx/worker/CSL probe; Office COM is an explicit separate qualification.

--mode modeled runs native widgets with a named object-model fixture.
--mode office attaches only to a manually opened, exact marked test document.
It never launches, opens, saves, closes or quits Word. A hung native write is
kept owned; the watchdog prints diagnostics and does not terminate its caller.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback


def validate_options(options):
    if options.mode == "office":
        if (options.allow_word_write is not True
                or not isinstance(options.expected_document, str)
                or not options.expected_document.strip()
                or len(options.expected_document) > 4096
                or not isinstance(options.fixture_token, str)
                or re.fullmatch(r"[0-9a-f]{32}", options.fixture_token) is None):
            raise ValueError("Office mode requires --allow-word-write, the exact --expected-document and a 32-lowerhex --fixture-token.")
    elif options.allow_word_write or options.expected_document or options.fixture_token:
        raise ValueError("Office authorization arguments do not apply to the modeled UI qualification.")


def evidence_helpers():
    path = Path(__file__).resolve(strict=True).with_name("windows_clipboard_reader_smoke.py")
    spec = importlib.util.spec_from_file_location("_word_native_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_origin(options):
    if os.name != "nt" or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError("Run the actual Windows probe with Python -I -B.")
    checkout = Path(__file__).resolve(strict=True).parents[1]
    if options.source is not None:
        if options.expect_installed or options.source.resolve(strict=True) != checkout:
            raise ValueError("The source override must be this exact checkout.")
        sys.path.insert(0, str(checkout / "src"))
    elif not options.expect_installed:
        raise ValueError("Select explicit source or installed qualification.")
    return checkout


def run(options, report, evidence, checkout):
    import wx
    import carr_library
    from carr_library.academic_integration import word_fields, word_com, formatting
    from carr_library.ui import word_citations, citation_formatting, desktop_app
    package = Path(carr_library.__file__).resolve(strict=True).parent
    distribution = None
    if options.expect_installed:
        owners = []
        for candidate in importlib.metadata.distributions(name="universal-research-library"):
            for member in candidate.files or ():
                if str(member).replace("\\", "/") == "carr_library/__init__.py":
                    if Path(candidate.locate_file(member)).resolve(strict=True) == Path(carr_library.__file__).resolve(strict=True):
                        owners.append(candidate)
        if len(owners) != 1:
            raise RuntimeError("Exactly one installed RECORD must own this imported Word adapter.")
        distribution = owners[0]
    report["modules"] = evidence._module_evidence(checkout, package, (
        ("academic_integration/word_fields.py", word_fields),
        ("academic_integration/word_com.py", word_com),
        ("academic_integration/formatting.py", formatting),
        ("ui/word_citations.py", word_citations),
        ("ui/citation_formatting.py", citation_formatting),
        ("ui/desktop_app.py", desktop_app)),
        source_mode=bool(options.source), distribution=distribution)
    fixture_path = checkout / "tests" / "fixtures" / "word_model.py"
    fixture_bytes = fixture_path.read_bytes()
    if len(fixture_bytes) > 64_000:
        raise ValueError("The explicitly synthetic fixture exceeds its code bound.")
    report["fixture_sha256"] = sha256(fixture_bytes).hexdigest()
    spec = importlib.util.spec_from_file_location("_native_word_fixture", fixture_path)
    model = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = model
    exec(compile(fixture_bytes, str(fixture_path), "exec"), model.__dict__)
    if wx.Platform != "__WXMSW__":
        raise RuntimeError("Native wxMSW controls are required.")
    report["wx"] = wx.version()
    report["office"] = "MODELED_NOT_QUALIFIED" if options.mode == "modeled" else "RUNNING_INSTANCE_PENDING"
    report["confirmation"] = "exact approved fixture callback; production dialog handler retained"
    failures, state = [], {"phase": 0, "dialog": None, "actions": None, "returned": False}
    main_thread = threading.get_ident()

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    gui = App(False)
    gui.SetExitOnFrameDelete(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()

    def check(name, condition):
        report["phase"] = name
        evidence._checkpoint(options.report, report)
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)
        evidence._checkpoint(options.report, report)
        print("PASS: " + name, flush=True)

    with TemporaryDirectory(prefix="carr-word-native-") as temporary:
        values = model.library_fixture(Path(temporary))
        library, workspace, _, academic, reference, second, source = values
        source_file = library.assets.resolve(source["asset"])
        source_sha = sha256(source_file.read_bytes()).hexdigest()
        hit, = library.search(workspace, "spectroscopy")
        quote = library.copy(workspace, hit.ref, hit.revision, "quote")
        token = options.fixture_token or ("1" * 32)
        fixture_text = "CARR Word integration " + token + "\r"
        expected_name = options.expected_document or "CARR synthetic Word fixture.docx"
        modeled_app = model.App(model.Document(fixture_text)) if options.mode == "modeled" else None

        class ProbeBridge(word_com.WordComBridge):
            first = True
            def capture(self, *, cancelled=lambda: False):
                snapshot = super().capture(cancelled=cancelled)
                if self.first:
                    with self._operation(cancelled) as calls:
                        actual = calls.call(lambda: self._doc.Content.Text)
                    if (snapshot.document_name != expected_name or actual != fixture_text
                            or snapshot.fields or snapshot.endnotes):
                        raise ValueError("The exact manually prepared empty marked Word test document was not selected; no write is authorized.")
                    self.first = False
                return snapshot

        bridge = (ProbeBridge(connect=lambda: modeled_app, identity=lambda doc: doc.identity)
                  if modeled_app is not None else ProbeBridge())
        frame = desktop_app.DesktopFrame(library)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        frame.Show()
        frame.Raise()
        idle_deadline = time.monotonic() + 20
        while frame.working or frame.busy:
            if time.monotonic() >= idle_deadline:
                raise TimeoutError("The existing desktop worker did not become available.")
            gui.Yield()
            wx.MilliSleep(10)
        timer = wx.Timer(frame)
        old_message = wx.MessageBox
        approval_count = 0

        def confirm(message, title, flags, parent=None):
            nonlocal approval_count
            if title != "Apply reviewed Word fields":
                raise AssertionError("The probe does not approve any unrelated native dialog.")
            check("confirmation binds the exact fixture document on the GUI thread",
                  threading.get_ident() == main_thread and expected_name in message
                  and flags & wx.NO_DEFAULT and parent is state["dialog"])
            approval_count += 1
            return wx.YES

        wx.MessageBox = confirm

        def find_control(name):
            def descend(window):
                for child in window.GetChildren():
                    if child.GetName() == name:
                        return child
                    value = descend(child)
                    if value is not None:
                        return value
                return None
            result = descend(state["dialog"])
            if result is None:
                raise AssertionError("Missing native Word control: " + name)
            return result

        def press(name):
            control = find_control(name)
            check("actual native button ready: " + name,
                  isinstance(control, wx.Button) and control.IsEnabled()
                  and control.AcceptsFocusFromKeyboard())
            control.SetFocus()
            if not simulator.Char(wx.WXK_SPACE):
                raise RuntimeError("Native Space dispatch failed.")

        def choose(name, index):
            control = find_control(name)
            control.SetSelection(index)
            event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
            event.SetEventObject(control)
            control.ProcessWindowEvent(event)

        def move_cursor_then_capture(actions):
            def move():
                actions.guard()
                if modeled_app is not None:
                    modeled_app.document.move_end()
                    return
                with bridge._operation(actions.cancelled) as calls:
                    doc = bridge._active(calls)
                    end = int(calls.call(lambda: doc.Content.End)) - 1
                    selected = calls.call(lambda: doc.Range(end, end))
                    calls.call(lambda: selected.Select(), mutation=True)
            actions._job("Positioning the explicitly approved Word fixture cursor", move,
                         lambda value: None)

        deadline = time.monotonic() + 180
        last_phase = None
        heartbeat = 0

        def tick(event):
            nonlocal last_phase, heartbeat
            heartbeat += 1
            try:
                if time.monotonic() > deadline:
                    raise TimeoutError("The Word native probe deadline expired; the exact pending owner is retained until its call returns.")
                dialog = state["dialog"]
                if dialog is None:
                    candidates = [window for window in wx.GetTopLevelWindows()
                        if bool(window) and getattr(window, "word_citations", None) is not None
                        and window.GetParent() is frame and window.IsModal() and window.IsShown()]
                    if not candidates:
                        return
                    dialog = state["dialog"] = candidates[0]
                    state["actions"] = dialog.word_citations
                actions = state["actions"]
                phase = state["phase"]
                if phase != last_phase:
                    report["phase"] = "native-word-step-" + str(phase)
                    evidence._checkpoint(options.report, report)
                    last_phase = phase
                if actions.pending is not None or frame.working or frame.busy:
                    return
                status = find_control("Word citation operation status").GetValue()
                if phase == 0:
                    press("Inspect open Word and references")
                elif phase == 1:
                    check("inspected exact opened document and current academic sources",
                          actions.context is not None
                          and actions.context.document.document_name == expected_name
                          and len(actions.context.bibliography.references) == 2)
                    styles = formatting.available_styles()
                    choose("Pinned CSL style for Word fields", [row.id for row in styles].index(options.style))
                    choices = find_control("Academic references for this Word citation or bibliography")
                    # Exact ID selection, independent of SQL/reference sort order.
                    for index, row in enumerate(actions.context.bibliography.references):
                        choices.Check(index, row["id"] == reference.id)
                    press("Preview Word changes")
                elif phase == 2:
                    check("real CSL preview exists before any apply", actions.plan is not None)
                    state["stable_id"] = actions.plan.insert_id
                    press("Apply reviewed Word changes")
                elif phase == 3:
                    check("native insertion has a verified completion receipt",
                          actions.context is None and "were applied" in status)
                    press("Inspect open Word and references")
                elif phase == 4:
                    check("native inserted citation retains stable identity and requested placement",
                          actions.context is not None and len(actions.context.document.fields) == 1
                          and actions.context.document.fields[0].id == state["stable_id"]
                          and actions.context.document.fields[0].placement ==
                              ("endnote" if options.style == "chicago-notes" else "inline"))
                    choose("Reviewed Word operation", 1)
                    choose("Existing stable CARR Word field", 1)
                    find_control("Explicit verified source locator; empty for none").SetValue("7")
                elif phase == 5:
                    find_control("Explicit source locator review").SetValue(True)
                    press("Preview Word changes")
                elif phase == 6:
                    check("reviewed edit keeps the original stable field ID",
                          actions.plan is not None and actions.plan.insert_id is None
                          and actions.plan.changes[0].id == state["stable_id"])
                    press("Apply reviewed Word changes")
                elif phase == 7:
                    check("native edit is acknowledged before the next action",
                          actions.context is None and "were applied" in status)
                    move_cursor_then_capture(actions)
                elif phase == 8:
                    press("Inspect open Word and references")
                elif phase == 9:
                    check("edited locator is observed from the actual field",
                          actions.context is not None and
                          word_fields.decode_field(actions.context.document.fields[0])["cluster"]["items"][0]["locator"] == "7")
                    choose("Reviewed Word operation", 2)
                    choices = find_control("Academic references for this Word citation or bibliography")
                    for index in range(choices.GetCount()):
                        choices.Check(index, True)
                    find_control("Explicit review of historical Word locators").SetValue(True)
                    press("Preview Word changes")
                elif phase == 10:
                    check("bibliography preview uses both current references",
                          actions.plan is not None and len(actions.plan.changes) == 2
                          and any(row.kind == "bibliography" for row in actions.plan.changes))
                    state["bibliography_id"] = actions.plan.insert_id
                    press("Apply reviewed Word changes")
                elif phase == 11:
                    check("bibliography native write has a verified outcome",
                          actions.context is None and "were applied" in status)
                    press("Inspect open Word and references")
                elif phase == 12:
                    check("citation and bibliography remain individually addressable",
                          actions.context is not None and {row.id for row in actions.context.document.fields}
                          == {state["stable_id"], state["bibliography_id"]})
                    choose("Reviewed Word operation", 3)
                    find_control("Explicit review of historical Word locators").SetValue(True)
                    press("Preview Word changes")
                elif phase == 13:
                    check("refresh preview preserves exact owned field membership",
                          actions.plan is not None and actions.plan.insert_id is None
                          and {row.id for row in actions.plan.changes}
                          == {state["stable_id"], state["bibliography_id"]})
                    press("Apply reviewed Word changes")
                elif phase == 14:
                    check("selective refresh completes without a bridge Save call",
                          actions.context is None and "were applied" in status and approval_count == 4)
                    press("Inspect open Word and references")
                elif phase == 15:
                    check("final native fields retain IDs and locator data",
                          actions.context is not None and len(actions.context.document.fields) == 2
                          and {row.id for row in actions.context.document.fields}
                          == {state["stable_id"], state["bibliography_id"]})
                    check("native Word work leaves exact original source and quotation unchanged",
                          sha256(source_file.read_bytes()).hexdigest() == source_sha
                          and library.copy(workspace, hit.ref, hit.revision, "quote") == quote)
                    check("existing GUI loop remains live during worker operations", heartbeat >= 16)
                    press("Close Word review")
                    timer.Stop()
                state["phase"] += 1
            except BaseException as exc:
                failures.append(traceback.format_exc())
                if options.mode == "office" and (isinstance(exc, word_com.WordUnavailable) or bridge.first):
                    report["office"] = "NOT_RUN"
                timer.Stop()
                actions = state.get("actions")
                if actions is not None:
                    actions.cancel()
                    actions.close()
                dialog = state.get("dialog")
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        frame.Bind(wx.EVT_TIMER, tick, timer)
        timer.Start(150)
        try:
            word_citations.show_word_citations(frame, bridge=bridge)
            state["returned"] = True
            if failures:
                raise RuntimeError(failures[0])
            if state["phase"] != 16:
                raise AssertionError("The complete native Word sequence did not run.")
            actions = state["actions"]
            if actions is None or actions._cleanup_future is None:
                raise AssertionError("The exact native owner was not queued for disposal.")
            # A mutating RPC is never detached to satisfy this probe deadline.
            # Successful modal completion has no pending call; normal cleanup
            # runs on the same existing desktop worker.
            actions._cleanup_future.result(timeout=30)
            check("native proxies close on the existing worker after the dialog",
                  actions._unregister is None and bridge._closed)
            report["office"] = "MODELED_NOT_QUALIFIED" if modeled_app is not None else "PASS"
            # A second real modal is destroyed while its inspection is queued.
            # The existing worker remains occupied until this exact barrier is
            # released; no Office call is needed for this lifetime assertion.
            blocker_entered, blocker_release = threading.Event(), threading.Event()
            def blocker():
                blocker_entered.set()
                if not blocker_release.wait(15):
                    raise TimeoutError("The native destruction probe did not release its worker.")
            blocking = frame.executor.submit(blocker)
            if not blocker_entered.wait(3):
                raise TimeoutError("The existing worker barrier did not begin.")
            destroyed = {"owner": None, "stage": 0}
            destroy_timer = wx.Timer(frame)
            destruction_deadline = time.monotonic() + 12
            def destroy_tick(event):
                try:
                    if time.monotonic() > destruction_deadline:
                        raise TimeoutError("The queued Word destruction probe did not finish.")
                    candidates = [window for window in wx.GetTopLevelWindows()
                        if bool(window) and getattr(window, "word_citations", None) is not None
                        and window.GetParent() is frame and window.IsModal() and window.IsShown()]
                    if not candidates:
                        return
                    current = candidates[0]
                    if destroyed["stage"] == 0:
                        destroyed["owner"] = current.word_citations
                        target = next(child for child in current.GetChildren()
                            if child.GetName() == "Inspect open Word and references")
                        target.SetFocus()
                        if not simulator.Char(wx.WXK_SPACE):
                            raise RuntimeError("The queued native inspection was not dispatched.")
                        destroyed["stage"] = 1
                    elif destroyed["owner"].pending is not None:
                        current.Destroy()
                        destroy_timer.Stop()
                        blocker_release.set()
                        destroyed["stage"] = 2
                except BaseException:
                    failures.append(traceback.format_exc())
                    destroy_timer.Stop()
                    blocker_release.set()
                    for current in wx.GetTopLevelWindows():
                        if (bool(current) and current.GetParent() is frame
                                and getattr(current, "word_citations", None) is not None
                                and current.IsModal()):
                            current.EndModal(wx.ID_CANCEL)
            frame.Bind(wx.EVT_TIMER, destroy_tick, destroy_timer)
            destroy_timer.Start(150)
            try:
                # This bridge remains unconnected: capture is revoked before
                # the blocked serial executor can start its queued operation.
                spare = word_com.WordComBridge()
                word_citations.show_word_citations(frame, bridge=spare)
            finally:
                blocker_release.set()
                destroy_timer.Stop()
                frame.Unbind(wx.EVT_TIMER, handler=destroy_tick, source=destroy_timer)
            blocking.result(timeout=15)
            frame.executor.submit(lambda: None).result(timeout=15)
            gui.Yield()
            if failures:
                raise RuntimeError(failures[0])
            owner = destroyed["owner"]
            check("direct native destruction revokes queued inspection and disposes its exact owner",
                  destroyed["stage"] == 2 and owner is not None and owner.closed
                  and owner._unregister is None and spare._closed and spare._app is None)
            report["native_widgets"] = "PASS"
        finally:
            timer.Stop()
            frame.Unbind(wx.EVT_TIMER, handler=tick, source=timer)
            wx.MessageBox = old_message
            if state.get("actions") is not None:
                state["actions"].close()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            gui.Yield()
    del activation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expect-installed", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("modeled", "office"))
    parser.add_argument("--style", default="apa", choices=("apa", "chicago-notes"))
    parser.add_argument("--allow-word-write", action="store_true")
    parser.add_argument("--expected-document")
    parser.add_argument("--fixture-token")
    options = parser.parse_args(argv)
    report = {"schema": "carr-word-native-probe/1", "status": "FAILED",
              "phase": "initial", "checks": [], "mode": options.mode,
              "runtime": sys.version, "platform": sys.platform,
              "office": "NOT_RUN", "native_widgets": "NOT_RUN"}
    evidence = evidence_helpers()
    evidence._checkpoint(options.report, report)
    # Diagnostics only: forcibly exiting an outstanding Word write would not
    # prove that Word stopped, so the watchdog never terminates this process.
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True, exit=False)
    try:
        validate_options(options)
        checkout = source_origin(options)
        run(options, report, evidence, checkout)
        report["status"] = "PASS"
        return 0
    except BaseException:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr, flush=True)
        return 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        evidence._checkpoint(options.report, report)


if __name__ == "__main__":
    raise SystemExit(main())
