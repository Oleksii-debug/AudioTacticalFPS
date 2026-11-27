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
