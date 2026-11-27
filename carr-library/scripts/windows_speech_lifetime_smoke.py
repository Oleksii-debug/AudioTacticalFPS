"""Actual installed-wx Reader destruction with a blocked source worker.

This probe qualifies native lifetime/cancellation only. It does not synthesize
a model voice, assess audio quality, or claim manual NVDA acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time

if os.name != 'nt':
    raise SystemExit('This required acceptance probe must run on native Windows.')

import wx
import carr_library
from carr_library.ui import speech_dialog


def pump_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Native Reader acceptance exceeded its bounded wait.')
        wx.YieldIfNeeded()
        time.sleep(.01)


def run_case(origin):
    started, release, worker_done = threading.Event(), threading.Event(), threading.Event()
    retired = threading.Event()
    observations = {'destroy_calls': 0, 'native_destroyed': False, 'parent_destroyed': False,
                    'late_callbacks': [], 'uncaught': [], 'cleanup_after_worker': False}
    original_dialog, original_call_after = wx.Dialog, wx.CallAfter
    original_executor = speech_dialog.ThreadPoolExecutor
    original_temporary = speech_dialog.TemporaryDirectory
    original_hook = sys.excepthook
    dialogs, executors, temporary = [], [], []
    parent = wx.Frame(None, title='Reader lifetime acceptance')
    parent.report = lambda message: observations['late_callbacks'].append('parent report') if observations['parent_destroyed'] else None

    def parent_destroyed(event):
        if event.GetId() == parent_id:
            observations['parent_destroyed'] = True
        event.Skip()
    parent_id = parent.GetId()
    parent.Bind(wx.EVT_WINDOW_DESTROY, parent_destroyed)

    class AuditedDialog(original_dialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.acceptance_dead = False
            identity = self.GetId()
            dialogs.append(self)
            def gone(event):
                if event.GetId() == identity:
                    self.acceptance_dead = True
                    observations['native_destroyed'] = True
                event.Skip()
            self.Bind(wx.EVT_WINDOW_DESTROY, gone)

        def Destroy(self):
            observations['destroy_calls'] += 1
            if self.acceptance_dead:
                observations['late_callbacks'].append('second native Destroy')
                raise AssertionError('A deleted Reader was destroyed twice.')
            return super().Destroy()

    def call_after(action, *args, **kwargs):
        if retired.is_set() and 'show_speech_reader.<locals>.run.<locals>.finish' in getattr(action, '__qualname__', ''):
            observations['late_callbacks'].append('worker completion queued after destruction')
        return original_call_after(action, *args, **kwargs)

    class AuditedTemporary:
        def __init__(self, *args, **kwargs):
            self.actual = original_temporary(*args, **kwargs)
            self.name = self.actual.name
            temporary.append(self)
        def cleanup(self):
            if not worker_done.is_set():
                raise AssertionError('Reading audio was retired while its worker was active.')
            self.actual.cleanup()
            observations['cleanup_after_worker'] = True

    def executor_factory(*args, **kwargs):
        executor = original_executor(*args, **kwargs)
        executors.append(executor)
        return executor

    def source_loader(cancelled):
        started.set()
        try:
            if not release.wait(8):
                raise AssertionError('Native acceptance did not release its controlled source worker.')
            if not cancelled():
                raise AssertionError('Actual native destruction did not cancel the source worker.')
            raise InterruptedError('Controlled retained-source load cancelled.')
        finally:
            worker_done.set()

    def retire():
        if not started.is_set():
            wx.CallLater(10, retire)
            return
        if len(dialogs) != 1:
            observations['uncaught'].append('Expected exactly one native Reader dialog.')
            release.set()
            parent.Destroy()
            return
        retired.set()
        if origin == 'owner':
            dialogs[0].Destroy()
        else:
            parent.Destroy()

    wx.Dialog, wx.CallAfter = AuditedDialog, call_after
    speech_dialog.ThreadPoolExecutor = executor_factory
    speech_dialog.TemporaryDirectory = AuditedTemporary
    sys.excepthook = lambda kind, value, traceback: observations['uncaught'].append(kind.__name__ + ': ' + str(value))
    try:
        parent.Show()
        wx.CallLater(10, retire)
        speech_dialog.show_speech_reader(parent, source_loader=source_loader,
            title='Controlled native lifetime source')
        assert started.is_set() and retired.is_set()
        assert not worker_done.is_set(), 'Closing the native Reader blocked on its source worker.'
        assert temporary and Path(temporary[0].name).is_dir()
        release.set()
        pump_until(lambda: worker_done.is_set() and observations['cleanup_after_worker'])
        wx.YieldIfNeeded()
        assert observations['native_destroyed']
        assert observations['destroy_calls'] == (1 if origin == 'owner' else 0), observations
        assert observations['parent_destroyed'] == (origin == 'parent')
        assert not observations['late_callbacks'], observations
        assert not observations['uncaught'], observations
        assert not Path(temporary[0].name).exists()
        return {'origin': origin, 'cancelled': True, 'worker_released': True,
                'cleanup_after_worker': True, 'late_native_calls': 0,
                'destroy_calls': observations['destroy_calls']}
    finally:
        release.set()
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=False)
        wx.Dialog, wx.CallAfter = original_dialog, original_call_after
        speech_dialog.ThreadPoolExecutor = original_executor
        speech_dialog.TemporaryDirectory = original_temporary
        sys.excepthook = original_hook
        if not observations['parent_destroyed']:
            parent.Destroy()
        wx.YieldIfNeeded()


def main():
    package = Path(carr_library.__file__).resolve()
    checkout = Path(__file__).resolve().parents[2]
    if package.is_relative_to(checkout):
        raise AssertionError('Native acceptance must import the installed package.')
    application = wx.App(False)
    cases = [run_case(origin) for origin in ('owner', 'parent')]
    print(json.dumps({'status': 'PASS', 'cases': cases, 'installed_module': str(package),
                      'manual_nvda': 'NOT_RUN', 'voice_quality': 'NOT_RUN'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
