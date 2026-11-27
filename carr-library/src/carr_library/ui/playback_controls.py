"""Prepared playback controls borrowing the existing Studio player and executor."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
from time import monotonic_ns

from ..audio.capture_types import AudioBudget, AudioCleanupPending
from ..audio.cleanup import pending_cleanup
from ..audio.jobs import checked_directory, file_identity, regular_file, verified_reader
from ..audio.playback_processing import PlaybackControls, prepare_playback
from ..native_file_metadata import stream_change_time


@dataclass
class _Selection:
    prepared: object
    source_owner: object
    source_stream: object
    source_change: object


class PlaybackWorkspace:
    """One borrowed current player; no device, thread or durable state registry."""
    def __init__(self, player, source, *, library_root, current=lambda: True,
                 ffmpeg=None, device_id=None):
        if not callable(current):
            raise TypeError('Playback requires the current selection guard.')
        self.player, self.source = player, Path(source).absolute()
        self.root = checked_directory(library_root)
        info = self.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.source_identity = file_identity(regular_file(self.source))
        self.current, self.ffmpeg, self.device_id = current, ffmpeg, device_id
        self.snapshot = self.selection = None
        self.preferred_controls = PlaybackControls()
        self.closed = False
        self.budget = AudioBudget()
        self._deadline = None
        self._close_lock = threading.Lock()

    def guard(self):
        if self.closed or not self.current():
            raise InterruptedError('The selected library or playback recording changed.')
        info = checked_directory(self.root).stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise InterruptedError('The selected library changed.')

    def _check(self, cancelled):
        self.guard()
        if cancelled():
            raise InterruptedError('Playback preparation cancelled.')
        if self._deadline is not None and monotonic_ns() >= self._deadline:
            raise TimeoutError('Playback preparation reached its selected time allowance.')
        return False

    def begin(self, budget, *, cancelled=lambda: False):
        if type(budget) is not AudioBudget or self.selection is not None:
            raise ValueError('Discard the prepared derivative before changing playback settings.')
        self.budget = budget
        numerator, denominator = float(budget.timeout).as_integer_ratio()
        self._deadline = monotonic_ns() + numerator * 1000000000 // denominator
        self._check(cancelled)
        if self.snapshot is None:
            # This whole-file hash work is on the Studio operation worker.
            # Capture before progress notifications can stop/clear a sleep timer.
            self.snapshot = self.player.capture_configuration(cancelled=lambda: self._check(cancelled))
        if file_identity(regular_file(self.source)) != self.source_identity:
            raise ValueError('The selected recording changed before playback preparation.')
        self._check(cancelled)

    def prepare(self, controls, *, cancelled=lambda: False, progress=lambda message: None):
        self._check(cancelled)
        if self.snapshot is None or self.selection is not None:
            raise ValueError('Open the checked playback workspace before preparing controls.')
        owner = verified_reader(self.source, expected_identity=self.source_identity)
        stream = owner.__enter__()
        derivative = None
        try:
            native = stream_change_time(stream)
            derivative = prepare_playback(self.source, controls, library_root=self.root,
                ffmpeg=self.ffmpeg, budget=self.budget,
                cancelled=lambda: self._check(cancelled), progress=progress)
            self._check(cancelled)
            selection = _Selection(derivative, owner, stream, native)
            self.selection = selection
            self.preferred_controls = controls
            return derivative
        except BaseException:
            try:
                if derivative is not None:
                    try:
                        derivative.release()
                    finally:
                        derivative.close()
            finally:
                owner.__exit__(None, None, None)
            raise

    def ready(self):
        self.guard()
        if self.selection is None:
            raise ValueError('Prepare the reviewed playback controls first.')
        selected = self.selection
        if (file_identity(regular_file(self.source)) != self.source_identity
                or file_identity(os.fstat(selected.source_stream.fileno())) != self.source_identity
                or stream_change_time(selected.source_stream) != selected.source_change):
            raise ValueError('The source recording changed; discard this playback derivative.')
        return selected.prepared.ready()

    def play(self, source_seconds=0):
        path = self.ready()
        self.player.play(path, start_seconds=self.selection.prepared.source_to_audio(source_seconds),
                         device_id=self.device_id)

    def pause_resume(self):
        self.ready()
        if self.player.native is None:
            raise ValueError('Pause and resume require the current native Windows player.')
        state = self.player.status().state
        if state == 'paused':
            return self.player.resume()
        if state in ('playing', 'starting'):
            return self.player.pause()
        raise ValueError('Start prepared playback before pausing or resuming.')

    def seek(self, seconds):
        self.ready()
        if self.player.path != self.selection.prepared.audio:
            raise ValueError('Start the prepared recording before seeking.')
        return self.player.seek(self.selection.prepared.source_to_audio(seconds))

    def stop(self):
        # A failure keeps both immutable readers and the private PCM alive.
        self.player.stop()

    def reset(self):
        """Worker cleanup. No byte owner is released before native Stop."""
        if self.snapshot is None and self.selection is None:
            return []
        self.player.set_playlist([])
        selected = self.selection
        warnings = []
        if selected is not None:
            try:
                selected.prepared.release()
            except Exception:
                warnings.append('The private playback copy changed and was discarded.')
            owner, selected.source_owner = selected.source_owner, None
            selected.source_stream = None
            if owner is not None:
                try:
                    owner.__exit__(None, None, None)
                except Exception:
                    warnings.append('The original recording changed; its playback copy was discarded.')
            # A cleanup failure retains this exact object for an explicit retry.
            selected.prepared.close()
            self.selection = None
        return warnings

    def close(self, *, restore=True):
        """Worker-only close; restore original checked settings, never playback."""
        with self._close_lock:
            return self._close(restore=restore)

    def _close(self, *, restore):
        if self.closed:
            return []
        warnings = self.reset()
        if restore and self.snapshot is not None:
            numerator, denominator = float(self.budget.timeout).as_integer_ratio()
            until = monotonic_ns() + numerator * 1000000000 // denominator
            def checked():
                if monotonic_ns() >= until:
                    raise TimeoutError('Restoring the checked playlist reached its selected time allowance.')
                if not self.current():
                    raise InterruptedError('The original playback scope is no longer selected.')
                return False
            try:
                self.player.restore_configuration(self.snapshot, cancelled=checked)
            except (OSError, ValueError, RuntimeError, InterruptedError, TimeoutError):
                # The borrowed temporary queue was cleared before validation.
                # A changed original cannot be silently adopted or played.
                warnings.append('The original playlist could not be revalidated. Add its current recordings again.')
        self.snapshot, self.closed = None, True
        return warnings


@contextmanager
def _observed(progress, token):
    manager, entered = None, False
    if progress is not None and token is not None:
        try:
            manager = progress.bind(token)
            manager.__enter__()
            entered = True
        except Exception:
            pass
    try:
        yield
    except BaseException:
        if entered:
            try:
                manager.__exit__(*sys.exc_info())
            except Exception:
                pass
        raise
    else:
        if entered:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


class PlaybackControlsDialog:
    """Native typed controls; the parent retains its player registration."""
    def __init__(self, parent, workspace, *, executor, progress=None, alive=lambda: True):
        import wx
        self.wx, self.workspace, self.executor = wx, workspace, executor
        self.progress, self.alive = progress, alive
        self.cancelled = threading.Event()
        self.busy = self.closed = self.close_requested = self._destroyed = self._close_failed = False
        self._future = self._content_request = self._retained = None
        self._cleanup_lock = threading.Lock()
        self._generation = 0
        self.dialog = wx.Dialog(parent, title='Independent playback controls', size=(720, 770),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Independent playback controls for the selected recording')
        try:
            self._build()
            self.timer = wx.Timer(self.dialog)
            self.dialog.Bind(wx.EVT_TIMER, self.tick, self.timer)
            self.dialog.Bind(wx.EVT_CLOSE, self.close)
            self._dialog_id = self.dialog.GetId()
            self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed_event)
            self.dialog.SetEscapeId(wx.ID_CANCEL)
            self.timer.Start(250)
            self.controls()
        except BaseException:
            self.dialog.Destroy()
            raise

    def _build(self):
        wx = self.wx
        layout = wx.BoxSizer(wx.VERTICAL)
        body = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL)
        body.SetScrollRate(0, 12)
        content = wx.BoxSizer(wx.VERTICAL)
        content.Add(wx.StaticText(body, label='Recording: ' + self.workspace.source.name), 0, wx.ALL, 6)
        explanation = wx.StaticText(body, label=
            'Prepare a private local PCM copy, then choose Play. Speed and pitch are independent of '
            'synthesis voice settings. The source recording stays unchanged. Time stretching gives '
            'an estimated original-time position; the output device keeps its existing capabilities.')
        explanation.Wrap(650)
        content.Add(explanation, 0, wx.EXPAND | wx.ALL, 6)
        self.fields = {}
        for key, label, initial, minimum, maximum, increment, digits in (
            ('speed', 'Playback speed multiplier', self.workspace.preferred_controls.speed, .25, 4, .05, 2),
            ('pitch', 'Playback pitch in semitones', self.workspace.preferred_controls.pitch_semitones, -12, 12, .5, 2),
            ('volume', 'Playback volume; zero mutes', self.workspace.preferred_controls.volume, 0, 1, .05, 2),
            ('pan', 'Stereo balance; minus one left, plus one right', self.workspace.preferred_controls.pan, -1, 1, .05, 2),
            ('bytes', 'Temporary PCM allowance in MiB', 1024, 1, 1048576, 128, 0),
            ('seconds', 'Preparation and restore allowance in seconds', 3600, .05, 31536000, 60, 2)):
            content.Add(wx.StaticText(body, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
            control = wx.SpinCtrlDouble(body, min=minimum, max=maximum, initial=initial, inc=increment)
            control.SetDigits(digits)
            control.SetName(label)
            self.fields[key] = control
            content.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        self.buttons = {}
        row = wx.WrapSizer(wx.HORIZONTAL)
        for name, label, action in (
            ('prepare', '&Prepare playback copy', self.prepare),
            ('reset', '&New playback settings', self.reset),
            ('cancel', 'Cancel preparation', self.cancel)):
            button = wx.Button(body, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, action)
            self.buttons[name] = button
            row.Add(button, 0, wx.ALL, 4)
        content.Add(row, 0, wx.EXPAND)
        self.position = wx.TextCtrl(body, value='0')
        self.position.SetName('Original recording position in seconds')
        content.Add(wx.StaticText(body, label='Original recording position in seconds'), 0, wx.ALL, 6)
        content.Add(self.position, 0, wx.EXPAND | wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for name, label, action in (
            ('play', '&Play prepared copy', self.play),
            ('pause', 'Pause / &resume', self.pause_resume),
            ('seek', 'Go to original &position', self.seek),
            ('stop', '&Stop playback', self.stop)):
            button = wx.Button(body, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, action)
            self.buttons[name] = button
            row.Add(button, 0, wx.ALL, 4)
        content.Add(row, 0, wx.EXPAND)
        self.status = wx.TextCtrl(body, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Prepared playback status and source-time qualification')
        self.status.SetMinSize((-1, 120))
        content.Add(self.status, 1, wx.EXPAND | wx.ALL, 6)
        body.SetSizer(content)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, 6)
        close = wx.Button(self.dialog, wx.ID_CANCEL, 'Close playback controls')
        close.SetName('Close playback controls and restore the stopped playlist')
        close.Bind(wx.EVT_BUTTON, self.close)
        self.buttons['close'] = close
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.dialog.SetSizer(layout)

    def report(self, message):
        if not self._destroyed:
            self.status.ChangeValue(str(message)[:4000])

    def current(self):
        return not self.closed and not self._destroyed and self.alive() and self.workspace.current()

    def controls(self):
        if self._destroyed:
            return
        prepared = self.workspace.selection is not None
        idle = not self.busy and not self.closed and not self._close_failed
        for field in self.fields.values():
            field.Enable(idle and not prepared)
        self.position.Enable(idle and prepared)
        for name, button in self.buttons.items():
            if name == 'close':
                button.Enable(not self.closed)
            elif name == 'cancel':
                button.Enable(self.busy and not self.close_requested)
            elif name == 'prepare':
                button.Enable(idle and not prepared)
            elif name == 'reset':
                button.Enable(idle and prepared)
            elif name in ('pause', 'seek'):
                button.Enable(idle and prepared and self.workspace.player.native is not None)
            else:
                button.Enable(idle and prepared)

    def _cancel_content(self):
        request, self._content_request = self._content_request, None
        if request is not None:
            request.close()

    def _progress_call(self, method, *args, **kwargs):
        if self.progress is not None:
            try:
                return getattr(self.progress, method)(*args, **kwargs)
            except Exception:
                pass
        return None

    def _run(self, label, operation, done, *, observed=False, cleanup=False):
        if self.busy or self.closed:
            return False
        self._cancel_content()
        self.busy = True
        self._generation += 1
        generation = self._generation
        if not cleanup:
            self.cancelled.clear()
        snapshot = self._progress_call('start', label, unit='steps') if observed else None
        token = getattr(snapshot, 'token', None)
        self.report(label)
        self.controls()
        def work():
            with _observed(self.progress, token):
                return operation()
        try:
            future = self.executor.submit(work)
            self._future = future
        except Exception as exc:
            self.busy = False
            if token is not None:
                self._progress_call('fail', exc, token=token)
            self.report(str(exc))
            self.controls()
            return False
        def completed(value):
            if self._destroyed:
                self._retain_cleanup()
                return
            try:
                self.wx.CallAfter(self._arrived, value, generation, done, token, cleanup)
            except RuntimeError:
                self._retain_cleanup()
        future.add_done_callback(completed)
        return True

    def _arrived(self, future, generation, done, token, cleanup):
        if self._destroyed:
            self._retain_cleanup()
            return
        if self.closed or generation != self._generation:
            return
        self.busy, self._future = False, None
        try:
            result = future.result()
            if token is not None:
                self._progress_call('finish_result', result, cancelled=self.cancelled.is_set(), token=token)
                self._progress_call('tick')
            if cleanup:
                done(result)
            elif self.close_requested or not self.current():
                self.close_requested = True
            elif self.cancelled.is_set():
                self._run('Discarding cancelled playback preparation', self.workspace.reset,
                    lambda warnings: self.report('Preparation cancelled. ' + ' '.join(warnings)))
            else:
                done(result)
        except Exception as exc:
            if token is not None:
                self._progress_call('cancel' if isinstance(exc, InterruptedError) else 'fail',
                                    str(exc) if isinstance(exc, InterruptedError) else exc, token=token)
            self.report(str(exc))
            if cleanup:
                self.close_requested, self._close_failed = False, True
        self.controls()
        if self.close_requested and not self.busy and not self.closed:
            self._close_worker()

    def prepare(self, event=None):
        if self.busy or not self.current():
            return
        try:
            controls = PlaybackControls(self.fields['speed'].GetValue(), self.fields['pitch'].GetValue(),
                                        self.fields['volume'].GetValue(), self.fields['pan'].GetValue())
            budget = AudioBudget(max_output_bytes=round(self.fields['bytes'].GetValue() * 1024**2),
                                 timeout=self.fields['seconds'].GetValue())
            def prepared(_):
                def update(message):
                    if self.progress is not None:
                        try:
                            self.progress.update(message=message)
                        except Exception:
                            pass
                self._run('Preparing independent playback controls',
                    lambda: self.workspace.prepare(controls, cancelled=self.cancelled.is_set, progress=update),
                    lambda value: self.report('Prepared ' + str(round(value.audio_duration, 3)) +
                        ' seconds. Original duration: ' + str(round(value.source_duration, 3)) +
                        ' seconds. Choose Play explicitly. Mapping: ' + value.source_time_accuracy + '.'),
                    observed=True)
            self._run('Verifying the original playlist and stopping its player',
                      lambda: self.workspace.begin(budget, cancelled=self.cancelled.is_set), prepared)
        except Exception as exc:
            self.report(str(exc))

    def reset(self, event=None):
        self._run('Discarding the private playback copy after Stop', self.workspace.reset,
                  lambda warnings: self.report('Choose new playback settings. ' + ' '.join(warnings)))

    def cancel(self, event=None):
        self.cancelled.set()
        self._progress_call('request_cancel')
        self.report('Cancellation requested. Waiting for owned preparation and file checks.')

    def _media(self, action):
        self._cancel_content()
        selected = self.workspace.selection
        if selected is None:
            self.report('Prepare playback controls first.')
            return
        def alive():
            return (self.current() and not self.busy and not self.close_requested
                    and self.workspace.selection is selected)
        def run():
            if alive():
                self.workspace.ready()
                action()
        try:
            if self.progress is None:
                run()
            else:
                self._content_request = self.progress.media_action(run, alive=alive,
                                                                   failed=lambda error: self.report(str(error)))
        except Exception as exc:
            self.report(str(exc))

    def play(self, event=None):
        try:
            seconds = float(self.position.GetValue().strip())
            self._media(lambda: self.workspace.play(seconds))
        except Exception as exc:
            self.report(str(exc))

    def pause_resume(self, event=None):
        try:
            if self.workspace.player.status().state == 'paused':
                self._media(self.workspace.pause_resume)
            else:
                self.workspace.pause_resume()
        except Exception as exc:
            self.report(str(exc))

    def seek(self, event=None):
        try:
            seconds = float(self.position.GetValue().strip())
            self._media(lambda: self.workspace.seek(seconds))
        except Exception as exc:
            self.report(str(exc))

    def stop(self, event=None):
        self._cancel_content()
        try:
            self.workspace.stop()
            self.report('Prepared playback stopped; its checked copy remains available.')
        except Exception as exc:
            self.report(str(exc))

    def tick(self, event=None):
        if self.closed or self._destroyed or self.busy or self._close_failed:
            return
        if not self.current():
            self.close()
            return
        selected = self.workspace.selection
        if selected is None:
            if self.workspace.snapshot is None:
                try:
                    self.workspace.player.poll()
                except Exception as exc:
                    self.report(str(exc))
            return
        try:
            self.workspace.ready()
            self.workspace.player.poll()
            status = self.workspace.player.status()
            if self.workspace.player.path != selected.prepared.audio:
                return
            if status.position_accuracy == 'external_player':
                text = (status.state.capitalize() + '. This backend does not report a playhead. '
                        'Prepared duration: ' + str(round(selected.prepared.audio_duration, 3)) + ' seconds.')
            else:
                position = selected.prepared.audio_to_source(min(selected.prepared.audio_duration,
                                                                  max(0, status.position_seconds)))
                text = (status.state.capitalize() + ': original ' + str(round(position, 3)) + ' / ' +
                        str(round(selected.prepared.source_duration, 3)) + ' seconds; prepared ' +
                        str(round(status.position_seconds, 3)) + ' seconds. Position: ' +
                        status.position_accuracy + '; source mapping: ' +
                        selected.prepared.source_time_accuracy + '.')
            if status.error:
                text += ' ' + status.error
            self.report(text)
        except Exception as exc:
            self._cancel_content()
            try:
                self.workspace.stop()
            except Exception as stop_error:
                self.report(str(exc) + ' Stop also needs attention: ' + str(stop_error))
            else:
                self.report(str(exc))

    def close(self, event=None):
        if self.closed:
            return
        self.close_requested, self._close_failed = True, False
        self.cancelled.set()
        self._cancel_content()
        if event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
            event.Veto()
        if self.busy:
            self.report('Closing after the current preparation and owned child have stopped.')
            return
        self._close_worker()

    def _close_worker(self):
        def done(warnings):
            if warnings:
                self.report(' '.join(warnings))
            self.closed = True
            self.timer.Stop()
            if self.dialog.IsModal():
                self.dialog.EndModal(self.wx.ID_CANCEL)
            else:
                self.dialog.Hide()
        self._run('Stopping playback and revalidating the original playlist',
                  self.workspace.close, done, cleanup=True)

    def _retain_cleanup(self):
        # Forced native destruction cannot release private PCM behind a live
        # player. The existing retained owner, not a new supervisor, retries.
        with self._cleanup_lock:
            if self._retained is None and not self.workspace.closed:
                def cleanup():
                    future = self._future
                    if future is not None and not future.done():
                        return False
                    return self.workspace.close(restore=False)
                self._retained = pending_cleanup(cleanup,
                    message='Prepared playback cleanup still owns its player and private PCM.').cleanup_owner

    def _destroyed_event(self, event):
        if event.GetId() == self._dialog_id:
            self._destroyed = True
            self.cancelled.set()
            self._cancel_content()
            self._retain_cleanup()
        event.Skip()

    def show(self, *, on_ready=None):
        if on_ready is not None:
            self.wx.CallAfter(on_ready, self)
        self.report('Choose independent playback controls and a local preparation allowance. Prepare does not play audio.')
        try:
            return self.dialog.ShowModal()
        finally:
            self.cancelled.set()
            self._cancel_content()
            self.timer.Stop()
            self._destroyed = True
            if not self.closed:
                self._retain_cleanup()
            try:
                self.dialog.Destroy()
            finally:
                if self._retained is not None:
                    # The caller owns the same player and executor. It must not
                    # re-enable either until this exact owner's event is set.
                    raise AudioCleanupPending(
                        'Playback window closed before its borrowed player and worker settled.',
                        cleanup_owner=self._retained)


def show_playback_controls(parent, workspace, *, executor, progress=None, alive=lambda: True,
                           on_ready=None):
    return PlaybackControlsDialog(parent, workspace, executor=executor,
                                  progress=progress, alive=alive).show(on_ready=on_ready)
