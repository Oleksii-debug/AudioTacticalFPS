"""One optional owner-configured admission boundary for new domain operations.

No policy is discovered from files, environment variables, entry points or an
LLM plan. A trusted host passes its existing TrustController. Context bindings
only forward that object through synchronous approved tool calls; they do not
verify licenses, persist grants or start a network task.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re

from ..delivery_contract import USER_DATA_ACTIONS
from .entitlements import OperationLease

FRESH_OPERATION_ACTIONS = frozenset({'audio.render', 'transcription.run', 'assistant.plan'})

_controller = ContextVar('carr_operation_trust_controller', default=None)
_active = ContextVar('carr_active_entitlement_leases', default=())


def checked_controller(value):
    if value is not None:
        from .controller import TrustController
        if not isinstance(value, TrustController):
            raise TypeError('Use the explicitly configured shared trust controller.')
    return value


def _action(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value) is None:
        raise ValueError('An explicit typed operation action is required.')
    return value


@contextmanager
def operation_context(controller):
    """Forward one already selected authority; never replace a nested owner."""
    controller = checked_controller(controller)
    current = _controller.get()
    if controller is None:
        yield current
        return
    if current is not None and current is not controller:
        raise ValueError('Nested operations cannot change their configured authority.')
    token = _controller.set(controller)
    try:
        yield controller
    finally:
        _controller.reset(token)


def admit_new_operation(controller, action):
    """Capture admission once; callers retain it while the real operation runs.

    Call only after the domain has handled a verified completed-result replay.
    Protected data actions remain available even if the configured provider is
    unavailable, expired or raises. No callback polls this lease mid-operation.
    """
    action = _action(action)
    if action in USER_DATA_ACTIONS:
        return OperationLease(action, 0)
    controller = checked_controller(controller) or _controller.get()
    if controller is None:
        return OperationLease(action, 0)  # Existing owner-private behavior.
    for owner, admitted_action, lease in reversed(_active.get()):
        if owner is controller and admitted_action == action:
            return lease
    return controller.begin_operation(action)


@contextmanager
def admitted_operation(controller, action):
    """Retain one immutable admission across a nested approved domain call."""
    action = _action(action)
    with operation_context(controller) as owner:
        lease = admit_new_operation(owner, action)
        token = _active.set((*_active.get(), (owner, action, lease)))
        try:
            yield lease
        finally:
            _active.reset(token)
