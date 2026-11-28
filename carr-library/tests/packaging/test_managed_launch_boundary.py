"""Actual lease interleavings at the managed application's launch boundary.

The marker and environment are synthetic public fixtures. Native shared and
exclusive lease acquisition is real on both supported source-test platforms;
these cases do not stand in for running an Inno installer.
"""
from contextlib import contextmanager

import pytest

from carr_library import installation_lease as leases


def _marker(root, state):
    (root / leases.MARKER).write_text(
        "[installation]\n"
        "schema=1\n"
        "product=Resonance.StandardInstall.v1\n"
        f"state={state}\n"
        f"bundle_sha256={'1' * 64}\n"
        f"setup_sha256={'2' * 64}\n"
        f"owner_sha256={'3' * 64}\n",
        encoding="utf-8",
    )


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "Managed Resonance Ω"
    environment = root / "app" / ".venv"
    environment.mkdir(parents=True)
    _marker(root, "complete")
    (environment / ".installation-complete").write_bytes(b"")
    with leases.installation_lease(root, exclusive=True):
        pass
    return root, environment


@pytest.mark.parametrize("change", ["pending-marker", "missing-readiness"])
def test_launch_rechecks_state_after_intervening_real_maintenance_lease(
    managed, monkeypatch, change
):
    root, environment = managed
    original = leases._process_application_hold
    transitions = []

    @contextmanager
    def after_initial_selection(selected):
        # An idle maintenance owner wins the native lease immediately before
        # this application acquires its shared lease, then loses its process.
        # No native lock is mocked, and no updater overlaps a live app lease.
        with leases.installation_lease(selected, exclusive=True):
            if change == "pending-marker":
                _marker(selected, "updating")
            else:
                (environment / ".installation-complete").unlink()
            transitions.append(selected)
        with original(selected):
            yield

    monkeypatch.setattr(leases, "_process_application_hold", after_initial_selection)
    entered = False
    with pytest.raises(RuntimeError, match="pending maintenance|incomplete"):
        with leases.application_lease([], prefix=environment):
            entered = True
    assert not entered
    assert transitions == [root]
    # A failed launch must settle its real shared handle before returning.
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize("state", ["preparing", "updating"])
def test_only_explicit_bootstrap_self_test_borrows_pending_installation(managed, state):
    root, environment = managed
    _marker(root, state)
    with leases.installation_lease(root, exclusive=True):
        with leases.application_lease(
            ["--self-test"], prefix=environment, allow_bootstrap_self_test=True
        ):
            assert (root / leases.MARKER).is_file()
        # The exception must not release the actual installer's native lease.
        with pytest.raises(RuntimeError, match="Close Resonance"):
            with leases.installation_lease(root):
                pytest.fail("The bootstrap exception released the installer lease.")


@pytest.mark.parametrize(
    "arguments, opt_in",
    [([], True), (["--self-test", "--desktop"], True), (["--self-test"], False)],
    ids=["ordinary-launch", "extra-bootstrap-option", "secondary-entrypoint"],
)
def test_pending_installation_has_no_general_launch_exception(managed, arguments, opt_in):
    root, environment = managed
    _marker(root, "updating")
    with pytest.raises(RuntimeError, match="pending maintenance"):
        with leases.application_lease(
            arguments, prefix=environment, allow_bootstrap_self_test=opt_in
        ):
            pytest.fail("A pending installation became an ordinary application.")


def test_application_body_retains_shared_native_lease_and_busy_launch_refuses(managed):
    root, environment = managed
    with leases.application_lease([], prefix=environment):
        with pytest.raises(RuntimeError, match="Close Resonance"):
            with leases.installation_lease(root, exclusive=True):
                pytest.fail("Maintenance entered a live ordinary application.")
    with leases.installation_lease(root, exclusive=True):
        with pytest.raises(RuntimeError, match="Close Resonance"):
            with leases.application_lease([], prefix=environment):
                pytest.fail("An ordinary application entered live maintenance.")
    with leases.application_lease([], prefix=environment):
        pass


# Application/cleanup composition regressions use the current real native lease.
# The final case additionally owns an actual child Python process.
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import inspect
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

import carr_library
from carr_library.audio import cleanup
from carr_library.audio.process import owned_process


def _exclusive_refused(root):
    with pytest.raises(RuntimeError, match="Close Resonance"):
        with leases.installation_lease(root, exclusive=True):
            pytest.fail("Maintenance entered before the existing owner became quiet.")


@pytest.mark.parametrize("argument_name", ["argv", "arguments"])
@pytest.mark.parametrize("style", ["positional", "keyword", "default", "generator"])
def test_entrypoint_keeps_real_signature_arguments_and_injected_services(
    managed, monkeypatch, argument_name, style
):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    values = ["literal", "Україна"]
    service, delivery, returned = object(), object(), object()
    calls = []

    def with_argv(argv=None, *, service=None, delivery=None):
        calls.append((list(argv), service, delivery))
        _exclusive_refused(root)
        return returned

    def with_arguments(arguments=None, *, service=None, delivery=None):
        calls.append((list(arguments), service, delivery))
        _exclusive_refused(root)
        return returned

    callback = with_argv if argument_name == "argv" else with_arguments
    command = leases.installed_entrypoint(callback)
    assert inspect.signature(command) == inspect.signature(callback)
    assert command.__wrapped__ is callback
    injected = dict(service=service, delivery=delivery)
    if style == "default":
        monkeypatch.setattr(sys, "argv", ["synthetic-cli", *values])
        actual = command(**injected)
    elif style == "keyword":
        actual = command(**{argument_name: values, **injected})
    elif style == "generator":
        actual = command((value for value in values), **injected)
    else:
        actual = command(values, **injected)
    assert actual is returned
    assert calls == [(values, service, delivery)]
    with leases.installation_lease(root, exclusive=True):
        pass


def test_bad_keyword_binding_never_enters_application_or_holds_a_lease(managed, monkeypatch):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    calls = []

    @leases.installed_entrypoint
    def command(arguments=None, *, service=None):
        calls.append(arguments)

    with pytest.raises(TypeError):
        command(arguments=[], argv=[])
    with pytest.raises(TypeError):
        command([], arguments=[])
    assert calls == []
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize("kind", ["caught", "propagated", "masked", "unregistered-causal"])
def test_managed_return_retains_real_lease_through_existing_pending_owner(
    managed, monkeypatch, kind
):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    ready, release = threading.Event(), threading.Event()
    pending, returned = [], object()

    @leases.installed_entrypoint
    def command(argv=None):
        if kind == "unregistered-causal":
            error = cleanup.AudioCleanupPending("Explicit causal fixture owner",
                cleanup_owner=cleanup.CleanupOwner(release.is_set))
        else:
            error = cleanup.pending_cleanup(release.is_set,
                message="Existing registered fixture owner")
        pending.append(error)
        ready.set()
        try:
            raise error
        except cleanup.AudioCleanupPending:
            if kind == "caught":
                return returned
            if kind == "masked":
                raise OSError("Original operation failure remains visible.") from error
            raise

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command, [])
        try:
            assert ready.wait(5)
            assert not future.done()
            assert not pending[0].cleanup_owner.quiescent
            _exclusive_refused(root)
        finally:
            release.set()
        if kind == "caught":
            assert future.result(5) is returned
        elif kind == "masked":
            with pytest.raises(OSError, match="Original operation failure") as caught:
                future.result(5)
            assert caught.value.__cause__ is pending[0]
        else:
            with pytest.raises(cleanup.AudioCleanupPending) as caught:
                future.result(5)
            assert caught.value is pending[0]
    assert pending[0].cleanup_owner.quiescent
    with leases.installation_lease(root, exclusive=True):
        pass


def test_managed_return_waits_for_both_registered_owners(managed, monkeypatch):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    release = [threading.Event(), threading.Event()]
    ready = threading.Event()
    owners = []

    @leases.installed_entrypoint
    def command(argv=None):
        for event in release:
            owners.append(cleanup.pending_cleanup(event.is_set,
                message="One of two actual registered fixture owners").cleanup_owner)
        ready.set()
        return "original-result"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command, [])
        try:
            assert ready.wait(5)
            _exclusive_refused(root)
            release[0].set()
            assert owners[0].quiescent_event.wait(5)
            assert not owners[1].quiescent and not future.done()
            _exclusive_refused(root)
        finally:
            for event in release:
                event.set()
        assert future.result(5) == "original-result"
    assert all(owner.quiescent for owner in owners)
    with leases.installation_lease(root, exclusive=True):
        pass


def test_close_origin_owner_is_observed_before_managed_process_can_return(managed, monkeypatch):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    release = [threading.Event(), threading.Event()]
    ready, second = threading.Event(), threading.Event()
    owners = []

    def followup():
        owners.append(cleanup.pending_cleanup(release[1].is_set,
            message="Actual owner introduced by the first close operation").cleanup_owner)
        second.set()

    @leases.installed_entrypoint
    def command(argv=None):
        def finish_first():
            if not release[0].is_set():
                return False
            if not second.is_set():
                followup()
            return True
        first = cleanup.pending_cleanup(finish_first,
            message="Original registered fixture owner").cleanup_owner
        owners.insert(0, first)
        ready.set()
        return 23

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command, [])
        try:
            assert ready.wait(5)
            release[0].set()
            assert second.wait(5)
            assert owners[0].quiescent_event.wait(5)
            assert len(owners) == 2 and owners[0].quiescent
            assert not owners[1].quiescent and not future.done()
            _exclusive_refused(root)
        finally:
            for event in release:
                event.set()
        assert future.result(5) == 23
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["initial-status", "attempt"])
def test_signal_during_cleanup_wait_is_delivered_only_after_quiescence(
    managed, monkeypatch, signal_type, phase
):
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    interrupted, release = threading.Event(), threading.Event()
    signal = signal_type("Purpose-written interruption")
    calls = []

    def finish():
        calls.append("attempt")
        if phase == "attempt" and not interrupted.is_set():
            interrupted.set()
            raise signal
        return release.is_set()

    owner = cleanup.CleanupOwner(finish)
    if phase == "initial-status":
        original_event = owner.quiescent_event

        class FirstStatusSignal:
            def is_set(self):
                if not interrupted.is_set():
                    interrupted.set()
                    raise signal
                return original_event.is_set()

            def set(self):
                return original_event.set()

            def wait(self, timeout=None):
                return original_event.wait(timeout)

        owner.quiescent_event = FirstStatusSignal()

    @leases.installed_entrypoint
    def command(argv=None):
        raise cleanup.AudioCleanupPending("Actual causal cleanup fixture", cleanup_owner=owner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command, [])
        try:
            assert interrupted.wait(5)
            assert not future.done() and not owner.quiescent
            _exclusive_refused(root)
        finally:
            release.set()
        with pytest.raises(signal_type) as caught:
            future.result(5)
        assert caught.value is signal
    assert owner.quiescent and len(calls) >= (2 if phase == "attempt" else 1)
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize("kind", ["portable", "bootstrap"])
def test_nonmanaged_or_externally_owned_entry_keeps_previous_return_behavior(
    managed, tmp_path, monkeypatch, kind
):
    root, environment = managed
    if kind == "portable":
        prefix = tmp_path / "portable/.venv"
        prefix.mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", str(prefix))
        outer = nullcontext()
        arguments = []
    else:
        monkeypatch.setattr(sys, "prefix", str(environment))
        _marker(root, "updating")
        outer = leases.installation_lease(root, exclusive=True)
        arguments = ["--self-test"]
    release = threading.Event()
    owners = []

    @leases.installed_entrypoint(allow_bootstrap_self_test=True)
    def command(argv=None):
        owners.append(cleanup.pending_cleanup(release.is_set,
            message="Externally retained bootstrap or portable fixture owner").cleanup_owner)
        return "unchanged"

    with outer:
        try:
            assert command(arguments) == "unchanged"
            assert not owners[0].quiescent
            if kind == "bootstrap":
                with pytest.raises(RuntimeError, match="Close Resonance"):
                    with leases.installation_lease(root):
                        pytest.fail("The external installer's exclusive lease was lost.")
            else:
                assert not (prefix.parent.parent / leases.MARKER).exists()
        finally:
            release.set()
            assert owners[0].quiescent_event.wait(5)


def test_actual_managed_python_stays_alive_until_its_owned_worker_is_quiet(
    managed, tmp_path
):
    root, environment = managed
    work = tmp_path / "real managed child proof"
    work.mkdir()
    release = work / "release"
    waiting = work / "entry-wait-entered"
    script = work / "managed_child.py"
    script.write_text(r'''
import json
import os
from pathlib import Path
import sys
import subprocess
import threading

package_parent, root, work = map(Path, sys.argv[1:])
sys.path.insert(0, str(package_parent))
from carr_library import installation_lease as leases
from carr_library.audio import cleanup
from carr_library.audio.process import owned_process, _stop_owned

sys.prefix = str(root / "app/.venv")
main_thread = threading.get_ident()
release = work / "release"
state = {}

@leases.installed_entrypoint
def main(arguments=None):
    context = owned_process([sys.executable, "-I", "-B", "-c",
        "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = context.__enter__()
    state["pid"] = worker.pid
    state["initial_returncode"] = worker.poll()

    def finish():
        if threading.get_ident() == main_thread:
            (work / "entry-wait-entered").write_bytes(b"actual synchronous lease barrier")
        if not release.exists():
            return False
        if _stop_owned(worker) is not True:
            return False
        context.__exit__(None, None, None)
        state["final_returncode"] = worker.poll()
        (work / "owner-quiet.json").write_text(json.dumps(state), encoding="utf-8")
        return True

    try:
        raise cleanup.pending_cleanup(finish, message="Actual owned worker must remain owned.")
    except cleanup.AudioCleanupPending:
        (work / "callback-returned").write_bytes(b"the callback catches pending cleanup")
        return 17

result = main(arguments=[])
(work / "entry-returned").write_text(str(result), encoding="utf-8")
raise SystemExit(result)
''', encoding="utf-8")
    package_parent = Path(carr_library.__file__).resolve().parent.parent
    with (work / "child.log").open("wb") as output:
        with owned_process([sys.executable, "-I", "-B", str(script),
                str(package_parent), str(root), str(work)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output) as child:
            try:
                deadline = time.monotonic() + 15
                while not waiting.exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail("The actual managed child never entered its synchronous cleanup barrier.")
                    time.sleep(.02)
                assert (work / "callback-returned").exists()
                assert child.poll() is None
                assert not (work / "owner-quiet.json").exists()
                assert not (work / "entry-returned").exists()
                _exclusive_refused(root)
            finally:
                release.write_bytes(b"release the same actual owner")
            assert child.wait(timeout=15) == 17
    proof = json.loads((work / "owner-quiet.json").read_text(encoding="utf-8"))
    assert proof["pid"] > 0 and proof["initial_returncode"] is None
    assert proof["final_returncode"] is not None
    assert (work / "entry-returned").read_text(encoding="utf-8") == "17"
    with leases.installation_lease(root, exclusive=True):
        pass



@pytest.mark.parametrize("state", ["preparing", "updating", "complete"])
def test_actual_native_check_entry_admits_only_its_fixed_bootstrap_under_exclusive_owner(
    managed, monkeypatch, state
):
    from carr_library import native_checks
    root, environment = managed
    _marker(root, state)
    (environment / ".installation-complete").unlink()
    monkeypatch.setattr(sys, "prefix", str(environment))
    calls = []

    def diagnostic(bundle_root):
        calls.append(bundle_root)
        # These are synthetic manifests, so no DLL/model is claimed here. The
        # real public parser/callback and external native lease are exercised.
        assert bundle_root == root / "app"
        with pytest.raises(RuntimeError, match="Close Resonance"):
            with leases.installation_lease(root):
                pytest.fail("The diagnostic released the installer's exclusive lease.")
        return {"status": "PASS", "checks": ["explicit diagnostic fixture"]}

    monkeypatch.setattr(native_checks, "check_native_dependencies", diagnostic)
    with leases.installation_lease(root, exclusive=True):
        assert native_checks.main(["--bundle-root", str(root / "app")]) == 0
        assert calls == [root / "app"]
        with pytest.raises(RuntimeError, match="Close Resonance"):
            with leases.application_lease([], prefix=environment):
                pytest.fail("The fixed diagnostic admitted an ordinary application.")
    assert not (environment / ".installation-complete").exists()
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize("kind", [
    "other-root", "relative-root", "parent-traversal", "extra-argument",
    "equals-form", "reordered", "duplicate-root", "empty", "self-test",
])
def test_native_bootstrap_has_no_general_diagnostic_or_path_bypass(
    managed, tmp_path, monkeypatch, kind
):
    from carr_library import native_checks
    root, environment = managed
    _marker(root, "preparing")
    (environment / ".installation-complete").unlink()
    monkeypatch.setattr(sys, "prefix", str(environment))
    current = str(root / "app")
    arguments = {
        "other-root": ["--bundle-root", str(tmp_path / "another-program")],
        "relative-root": ["--bundle-root", "app"],
        "parent-traversal": ["--bundle-root", str(root / "unused" / ".." / "app")],
        "extra-argument": ["--bundle-root", current, "--desktop"],
        "equals-form": ["--bundle-root=" + current],
        "reordered": [current, "--bundle-root"],
        "duplicate-root": ["--bundle-root", current, "--bundle-root", current],
        "empty": [],
        "self-test": ["--self-test"],
    }[kind]
    monkeypatch.setattr(native_checks, "check_native_dependencies",
        lambda *_: pytest.fail("Unapproved diagnostic arguments reached native inspection."))
    with leases.installation_lease(root, exclusive=True):
        assert native_checks.main(arguments) == 1
    assert not (environment / ".installation-complete").exists()


@pytest.mark.parametrize("entry", ["ordinary-wrapper", "primary-self-test"])
def test_other_entrypoints_cannot_borrow_the_native_diagnostic_shape(
    managed, monkeypatch, entry
):
    from carr_library import __main__
    root, environment = managed
    _marker(root, "updating")
    (environment / ".installation-complete").unlink()
    monkeypatch.setattr(sys, "prefix", str(environment))

    @leases.installed_entrypoint
    def ordinary(arguments=None):
        pytest.fail("An ordinary entrypoint borrowed the native diagnostic exception.")

    command = ordinary if entry == "ordinary-wrapper" else __main__.main
    with leases.installation_lease(root, exclusive=True):
        assert command(["--bundle-root", str(root / "app")]) == 1


@pytest.mark.parametrize("state", ["ready", "uninstalled"])
def test_fixed_native_diagnostic_cannot_bypass_maintenance_after_bootstrap(
    managed, monkeypatch, state
):
    from carr_library import native_checks
    root, environment = managed
    if state == "uninstalled":
        _marker(root, "uninstalled")
        (environment / ".installation-complete").unlink()
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(native_checks, "check_native_dependencies",
        lambda *_: pytest.fail("Non-bootstrap diagnostic entered live maintenance."))
    with leases.installation_lease(root, exclusive=True):
        assert native_checks.main(["--bundle-root", str(root / "app")]) == 1


def test_ready_native_diagnostic_holds_the_ordinary_shared_application_lease(managed, monkeypatch):
    from carr_library import native_checks
    root, environment = managed
    monkeypatch.setattr(sys, "prefix", str(environment))
    calls = []

    def diagnostic(bundle_root):
        calls.append(bundle_root)
        _exclusive_refused(root)
        return {"status": "PASS", "checks": ["explicit ready-state fixture"]}

    monkeypatch.setattr(native_checks, "check_native_dependencies", diagnostic)
    assert native_checks.main(["--bundle-root", str(root / "app")]) == 0
    assert calls == [root / "app"]
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize('kind', ['caught', 'propagated'])
def test_managed_entry_retains_actual_lease_until_retained_stage_notification_finishes(
        managed, tmp_path, monkeypatch, kind):
    from tempfile import TemporaryDirectory
    root, environment = managed
    monkeypatch.setattr(sys, 'prefix', str(environment))
    allow, prepared, entered, release = (threading.Event() for _ in range(4))
    pending, stages, errors, result = [], [], [], object()
    @contextmanager
    def stage_context():
        with TemporaryDirectory(prefix='.managed-callback-', dir=tmp_path) as temporary:
            stage = Path(temporary)
            (stage / 'original').write_bytes(b'exact retained original')
            stages.append(stage)
            yield stage
            entered.set()
            if not release.wait(10):
                errors.append('The retained-stage callback was not released.')
    @leases.installed_entrypoint
    def command(arguments=None):
        error = cleanup.pending_cleanup(allow.is_set, message='Existing managed native owner')
        pending.append(error)
        try:
            with cleanup.retained_context(stage_context()):
                raise error
        except cleanup.AudioCleanupPending:
            prepared.set()
            if kind == 'caught':
                return result
            raise
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command, [])
        try:
            assert prepared.wait(10)
            allow.set()
            assert entered.wait(10)
            owner = pending[0].cleanup_owner
            assert owner.quiescent and not owner.notifications_complete_event.is_set()
            assert stages[0].is_dir() and not future.done()
            _exclusive_refused(root)
        finally:
            allow.set()
            release.set()
        if kind == 'caught':
            assert future.result(10) is result
        else:
            with pytest.raises(cleanup.AudioCleanupPending) as raised:
                future.result(10)
            assert raised.value is pending[0]
    assert pending[0].cleanup_owner.notifications_complete_event.is_set()
    assert not stages[0].exists() and not errors
    with leases.installation_lease(root, exclusive=True):
        pass
