"""Importable targets for the resource tests' real spawned Python children.

Only this tests directory is added to the parent's temporary import path. Spawn
copies that path without exposing the checkout root or src; the application and
broker must still resolve to the exact same files as in the parent.
"""
import os
from pathlib import Path
import sys
import time

import carr_library
from carr_library.operations import ResourceBroker, ResourceRequest


def _assert_application_origin(expected_package, expected_broker):
    actual_package = Path(carr_library.__file__).resolve()
    actual_broker = Path(sys.modules[ResourceBroker.__module__].__file__).resolve()
    assert actual_package == Path(expected_package), (actual_package, expected_package)
    assert actual_broker == Path(expected_broker), (actual_broker, expected_broker)


def _hold(directory, request, queue, stop, *, expected_package, expected_broker, crash=False):
    _assert_application_origin(expected_package, expected_broker)
    try:
        broker = ResourceBroker(directory)
        with broker.reserve(ResourceRequest.from_dict(request), operation_id='child', timeout=2) as lease:
            queue.put(('held', lease.id))
            if not stop.wait(10):
                raise TimeoutError('Parent did not stop fixture process.')
            if crash:
                os._exit(73)
        queue.put(('released', None))
    except BaseException as exc:
        queue.put(('error', type(exc).__name__))
        raise


def _contend(directory, label, queue, start, *, expected_package, expected_broker):
    _assert_application_origin(expected_package, expected_broker)
    broker = ResourceBroker(directory)
    if not start.wait(10):
        raise TimeoutError('Parent did not start fixture process.')
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id=label, timeout=5):
        queue.put((label, 'entered', time.monotonic()))
        time.sleep(.12)
        queue.put((label, 'exiting', time.monotonic()))


