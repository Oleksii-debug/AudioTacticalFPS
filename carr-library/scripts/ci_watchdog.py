"""Bounded per-case Python diagnostics; acceptance deadlines stay with callers.

Scheduled asynchronous C traceback walks can race live frames (CPython issues
116008 and 140815). This diagnostic runs as ordinary Python with owned frame
references. It cannot preempt a native call that continuously holds the GIL.
Normal fatal faulthandler reporting is deliberately left enabled.
"""
import math
import os
import sys
import threading

DEFAULT_DIAGNOSTIC_SECONDS = 90.0
MAX_THREADS = 8
MAX_FRAMES = 16
MAX_DIAGNOSTIC_BYTES = 16 * 1024


def _bounded_text(value, maximum):
    return ''.join(char if 32 <= ord(char) < 127 else '?'
                   for char in str(value)[:maximum])


def _case_label(nodeid):
    return _bounded_text(str(nodeid).partition('[')[0], 240)


def without_scheduled_faulthandler(arguments):
    """Override only the scheduled dump, after options and before a '--'."""
    result = list(arguments)
    position = result.index('--') if '--' in result else len(result)
    result[position:position] = ['-o', 'faulthandler_timeout=0']
    return result


def _thread_snapshot(label, timeout):
    lines = [f'CASE_DIAGNOSTIC after={timeout:.3f}s {label}',
             'Python thread snapshot; no locals, source lines or parameter values:']
    current = threading.get_ident()
    main = threading.main_thread().ident
    frames = sys._current_frames()
    frame = None
    try:
        identifiers = sorted((value for value in frames if value != current),
                             key=lambda value: (value != main, value))
        for ident in identifiers[:MAX_THREADS]:
            lines.append('Thread main:' if ident == main else f'Thread {ident}:')
            frame = frames[ident]
            for _ in range(MAX_FRAMES):
                if frame is None:
                    break
                code = frame.f_code
                filename = code.co_filename[-240:].replace('\\', '/').rsplit('/', 1)[-1]
                lines.append('  ' + _bounded_text(filename, 96) + ':' +
                             str(frame.f_lineno) + ' in ' + _bounded_text(code.co_name, 96))
                frame = frame.f_back
            if frame is not None:
                lines.append('  ... remaining frames omitted')
        if len(identifiers) > MAX_THREADS:
            lines.append('... remaining threads omitted')
    finally:
        frame = None
        frames.clear()
    text = '\n'.join(lines) + '\n'
    suffix = '\n... diagnostic output truncated\n'
    if len(text) > MAX_DIAGNOSTIC_BYTES:
        text = text[:MAX_DIAGNOSTIC_BYTES - len(suffix)] + suffix
    return text


class CaseWatchdog:
    """One diagnostic per active token; stale callbacks cannot write after close."""

    def __init__(self, *, timeout=DEFAULT_DIAGNOSTIC_SECONDS, emit=None):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise ValueError('The diagnostic interval must be a positive finite number.')
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._generation = 0
        self._active = None
        self._reported = False
        self._timer = None
        self._closed = False
        self._emit = emit
        # Construct before pytest.main: retain the original diagnostic stream
        # while pytest redirects fd 2 to individual test capture files.
        self._fd = os.dup(2) if emit is None else None

    def _cancel_locked(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def start(self, nodeid):
        with self._lock:
            if self._closed:
                raise RuntimeError('The case watchdog is closed.')
            self._cancel_locked()
            self._generation += 1
            token = self._generation
            self._active = (token, _case_label(nodeid))
            self._reported = False
            timer = threading.Timer(self.timeout, self._fire, args=(token,))
            timer.daemon = True
            timer.name = 'carr-case-watchdog'
            self._timer = timer
            timer.start()
            return token

    def finish(self, token):
        with self._lock:
            if self._active is None or self._active[0] != token:
                return False
            self._active = None
            self._cancel_locked()
            return True

    def _fire(self, token):
        with self._lock:
            if (self._closed or self._active is None
                    or self._active[0] != token or self._reported):
                return
            label = self._active[1]
            self._reported = True
        # Capture outside the lifecycle lock: completion and the next case may
        # proceed while this ordinary Python thread gathers bounded frames.
        try:
            text = _thread_snapshot(label, self.timeout)
        except Exception as error:
            text = ('CASE_DIAGNOSTIC ' + label + ': snapshot unavailable (' +
                    _bounded_text(type(error).__name__, 80) + ')\n')
        with self._lock:
            if self._closed or self._active is None or self._active[0] != token:
                return
            # The final epoch check, write and descriptor close share this lock.
            # A stale callback therefore cannot write to a reused descriptor.
            try:
                if self._emit is not None:
                    self._emit(text)
                elif self._fd is not None:
                    data = text.encode('ascii')
                    while data:
                        written = os.write(self._fd, data)
                        if written <= 0:
                            break
                        data = data[written:]
            except Exception:
                # Diagnostic delivery cannot change pytest's acceptance verdict.
                pass

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._active = None
            self._generation += 1
            self._cancel_locked()
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
