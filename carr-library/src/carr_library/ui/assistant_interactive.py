"""UI-thread bridge to one actually open source reader and its existing player."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from ..assistant.interactive_tools import (
    RECEIPT_SCHEMA, TARGET_SCHEMA, _EFFECTS,
    validate_arguments, validate_pcm, validate_result, validate_saved_scope, validate_settings,
)


class NativeReaderBridge:
    """No discovery or reconnect: this object is bound to one native window.

    The service persists its one-time intent before calling execute_step. This
    bridge additionally refuses duplicate operation IDs in the current process.
    Every widget/player operation is marshalled to the existing event thread.
    """
    def __init__(self, frame, reader, *, dispatch=None, is_main_thread=None, clock=time.monotonic):
        if dispatch is None or is_main_thread is None:
            import wx
            dispatch = wx.CallAfter if dispatch is None else dispatch
            is_main_thread = wx.IsMainThread if is_main_thread is None else is_main_thread
        if not is_main_thread():
            raise RuntimeError('Capture a native reader bridge on its UI thread.')
        self.frame, self.reader = frame, reader
        self.dispatch, self.is_main_thread, self.clock = dispatch, is_main_thread, clock
        self.library = frame.library
        self.root = Path(self.library.root).absolute()
        info = self.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.closed = False
        self._execution = threading.Lock()
        self._used_operations = set()
        self._dispatched = False
        if not getattr(reader, '_assistant_session_id', None):
            reader._assistant_session_id = uuid4().hex
        self.session_id = reader._assistant_session_id
        self.target = self._capture_target()

    def _current(self):
        if (self.closed or getattr(self.frame, '_closing', False) or self.frame.library is not self.library
                or self.reader.closed or self.reader.close_pending or self.reader.destroyed
                or self.reader._assistant_session_id != self.session_id
                or Path(self.library.root).absolute() != self.root or self.reader.root != self.root):
            raise ValueError('The selected native reader is closed or its library owner changed.')
        info = self.root.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The selected library directory changed.')
        self.reader.guard()

    def _capture_target(self):
        self._current()
        location = self.reader.location
        if location is None or location.text_sha256 != sha256(self.reader.source.encode('utf-8')).hexdigest():
            raise ValueError('Choose an unchanged source-bound reader in the current workspace.')
        value = dict(schema=TARGET_SCHEMA, session_id=self.session_id,
                     location=asdict(location), source_chars=len(self.reader.source))
        from ..assistant.interactive_tools import validate_target
        validate_target(value)
        return value

    def _settings(self):
        selected, playback = self.reader._settings(), self.reader._playback_settings()
        return validate_settings({**selected.engine_options(), 'playback_tempo': playback.tempo,
                                  'playback_pitch': playback.pitch})

    def _pcm(self):
        session = self.reader.session
        excerpt = None if session is None else session.current
        if excerpt is None:
            return None
        return validate_pcm(dict(sha256=excerpt.sha256, frames=excerpt.frames,
                                 sample_rate=excerpt.sample_rate,
                                 start=excerpt.selection.start, end=excerpt.selection.end), self.target)

    def arguments(self, tool, action, **options):
        """Capture an unapproved JSON draft from native controls on the UI thread."""
        if not self.is_main_thread():
            raise RuntimeError('Capture the selected native controls on their UI thread.')
        self._current()
        value = dict(target=json.loads(json.dumps(self.target)), action=action, **options)
        if tool == 'reader.control' and action in ('select', 'prepare', 'read'):
            from .live_reading import selected_source_offsets
            first, last = selected_source_offsets(self.reader.text, self.reader.source)
            value.setdefault('start', first)
            value.setdefault('end', last)
            if action in ('prepare', 'read'):
                value['settings'] = self._settings()
        if tool == 'player.control':
            value['pcm'] = self._pcm()
            if action == 'seek':
                value.setdefault('seconds', float(self.reader.seek_seconds.GetValue()))
        step = SimpleNamespace(tool=tool, arguments=value)
        validate_arguments(step)
        self._validate_ui(step, {'workspace_id': self.target['location']['workspace']})
        return value

    def _invoke_ui(self, action, *, cancelled=lambda: False, allow_closed=False):
        if self.is_main_thread():
            if cancelled() or self.closed and not allow_closed:
                raise InterruptedError('The interactive command was cancelled before UI dispatch.')
            return action()
        event, gate = threading.Event(), threading.Lock()
        ticket = dict(abandoned=False, started=False, result=None, error=None)
        def apply():
            with gate:
                if ticket['abandoned'] or (self.closed and not allow_closed) or cancelled():
                    ticket['error'] = InterruptedError('The interactive command was cancelled before UI dispatch.')
                    event.set()
                    return
                ticket['started'] = True
            try:
                ticket['result'] = action()
            except BaseException as exc:
                ticket['error'] = exc
            finally:
                event.set()
        self.dispatch(apply)
        deadline = self.clock() + 10
        while not event.wait(.025):
            if cancelled() or self.clock() >= deadline or self.closed and not allow_closed:
                with gate:
                    if not ticket['started']:
                        ticket['abandoned'] = True
                        raise InterruptedError('The queued interactive action was discarded before UI dispatch.')
                # Once dispatch starts, settle that short native callback. It
                # never waits for synthesis: the existing reader owns that work.
        if ticket['error'] is not None:
            raise ticket['error']
        return ticket['result']

    def _validate_ui(self, step, scope):
        if not self.is_main_thread():
            raise RuntimeError('Native reader validation must run on the UI thread.')
        validate_saved_scope(step, scope, str(self.root))
        if self._capture_target() != self.target or step.arguments['target'] != self.target:
            raise ValueError('The source/window changed; capture and review a new native command.')
        active = getattr(self.frame, 'active', None)
        if not active or active[0] != scope['workspace_id']:
            raise PermissionError('The command no longer has the current workspace selected.')
        reader, args = self.reader, step.arguments
        if reader.future is not None or reader.cleanup_pending:
            raise ValueError('Wait for the current reader operation to settle before reviewing a command.')
        action = args['action']
        if step.tool == 'reader.control':
            if action in ('select', 'prepare', 'read') and reader.session is not None:
                raise ValueError('Stop the current reading session before changing its selected text.')
            if action in ('prepare', 'read'):
                if not reader.source[args['start']:args['end']].strip():
                    raise ValueError('Select non-whitespace text for speech preparation.')
                if self._settings() != validate_settings(args['settings']):
                    raise ValueError('The selected voice or playback settings changed after capture.')
            if action == 'read' and reader.player.native is None:
                raise ValueError('Interactive playback requires the existing native Windows player.')
            if action == 'stop' and reader.session is None:
                raise ValueError('There is no active reader operation to stop.')
        else:
            if self._pcm() != args['pcm']:
                raise ValueError('The prepared utterance changed; capture its current PCM identity again.')
            if reader.player.native is None:
                raise ValueError('Interactive player control requires the existing native Windows player.')
            state = reader.player.status().state
            if action == 'play' and reader.playing:
                raise ValueError('The selected utterance is already playing.')
            if action == 'pause' and state not in ('playing', 'starting'):
                raise ValueError('Only an actively playing utterance can be paused.')
            if action == 'resume' and state != 'paused':
                raise ValueError('Only the selected paused utterance can be resumed.')
            if action == 'seek' and (reader.player.path is None or state not in ('playing', 'starting', 'paused')):
                raise ValueError('Seek requires the selected utterance to be open in the native player.')
        return step

    def validate_step(self, step, scope):
        scope_dict = scope.to_dict() if hasattr(scope, 'to_dict') else scope
        validate_saved_scope(step, scope_dict, str(self.root))
        self._invoke_ui(lambda: self._validate_ui(step, scope_dict))
        return step

    def _begin(self, step, scope, operation_id):
        self._validate_ui(step, scope)
        if operation_id in self._used_operations:
            raise PermissionError('This interactive dispatch was already attempted. Review a new plan.')
        self._used_operations.add(operation_id)
        self._dispatched = True
        reader, args = self.reader, step.arguments
        marker = getattr(reader, '_assistant_error_sequence', 0)
        action = args['action']
        if step.tool == 'reader.control':
            if action in ('select', 'prepare', 'read'):
                reader._select((args['start'], args['end']))
                reader.mode.SetSelection(reader.modes.index('selection'))
                if action != 'select':
                    reader.read(play=action == 'read')
            else:
                reader.stop()
        elif action == 'play':
            reader.play()
        elif action in ('pause', 'resume'):
            reader.pause()
        elif action == 'seek':
            reader.seek_seconds.ChangeValue(str(args['seconds']))
            reader.seek()
        else:
            reader.stop()
        return marker

    def _observed(self, step, marker):
        self._current()
        reader, args = self.reader, step.arguments
        if self._capture_target() != self.target:
            raise ValueError('The source changed while the interactive command was executing.')
        if getattr(reader, '_assistant_error_sequence', 0) != marker:
            raise RuntimeError('The native reader refused the command: ' +
                               str(getattr(reader, '_assistant_last_error', 'unknown native error')))
        if reader.future is not None or reader.cleanup_pending:
            return None
        from .live_reading import selected_source_offsets
        selected = list(selected_source_offsets(reader.text, reader.source))
        pcm, playback = self._pcm(), None
        settings = validate_settings(args['settings']) if args.get('settings') is not None else None
        action = args['action']
        if action == 'stop':
            if reader.session is not None:
                return None
            pcm = None
        elif step.tool == 'reader.control' and action == 'select':
            if selected != [args['start'], args['end']]:
                raise RuntimeError('The native control did not apply the requested exact source selection.')
        else:
            if pcm is None:
                raise RuntimeError('The native command did not retain its prepared utterance.')
            if step.tool == 'player.control' and pcm != args['pcm']:
                raise ValueError('The selected PCM changed during native player control.')
            if step.tool == 'reader.control' and action == 'prepare':
                if reader.playing:
                    raise RuntimeError('Prepare unexpectedly started playback.')
            else:
                status = reader.player.status()
                if status.state in ('error', 'unavailable', 'closed'):
                    raise RuntimeError(status.error or 'The native player could not complete this command.')
                if (action in ('read', 'play', 'resume') and not reader.playing):
                    if reader.media_request is not None and not reader.media_request.closed:
                        return None
                    raise RuntimeError('Playback was not actually started.')
                if action == 'pause' and status.state != 'paused':
                    raise RuntimeError('The native player did not actually pause.')
                if action == 'seek' and abs(status.position_seconds - args['seconds']) > .25:
                    raise RuntimeError('The native player did not report the requested seek position.')
                playback = {key: getattr(status, key) for key in
                    ('state', 'position_frames', 'total_frames', 'sample_rate', 'position_accuracy')}
        return dict(selection=selected, pcm=pcm, playback=playback, settings=settings)

    def _cancel_owned(self):
        # Used after a dispatched async prepare/read. This never reconnects a
        # different window or source, and it leaves settlement to its real owner.
        def stop():
            reader = self.reader
            if (self._dispatched and not reader.closed and not reader.destroyed
                    and reader._assistant_session_id == self.session_id
                    and reader.location is not None and asdict(reader.location) == self.target['location']):
                reader.stop()
        self._invoke_ui(stop, allow_closed=True)
        deadline = self.clock() + 30
        while self.clock() < deadline:
            settled = self._invoke_ui(lambda: self.reader.closed or (
                self.reader.future is None and self.reader.session is None
                and not self.reader.cleanup_pending), allow_closed=True)
            if settled:
                return
            time.sleep(.025)
        raise TimeoutError('Reader cancellation is still settling; do not repeat the interactive command.')

    def execute_step(self, step, scope, *, operation_id, cancelled, progress):
        if self.is_main_thread():
            raise RuntimeError('Assistant execution waits on a worker; native controls stay on the UI thread.')
        validate_arguments(step)
        if not callable(cancelled) or not isinstance(operation_id, str) or len(operation_id) != 32:
            raise ValueError('A cancellable one-time interactive operation is required.')
        from ..assistant.interactive_tools import _ID
        if not _ID.fullmatch(operation_id):
            raise ValueError('Invalid interactive operation identity.')
        scope_dict = scope.to_dict() if hasattr(scope, 'to_dict') else scope
        validate_saved_scope(step, scope_dict, str(self.root))
        if not self._execution.acquire(blocking=False):
            raise RuntimeError('Another interactive command is still settling.')
        self._dispatched = False
        try:
            if cancelled():
                raise InterruptedError('Interactive command cancelled before dispatch.')
            marker = self._invoke_ui(lambda: self._begin(step, scope_dict, operation_id), cancelled=cancelled)
            deadline = self.clock() + min(float(scope_dict.get('max_seconds', 3600)), 3600)
            while True:
                observation = self._invoke_ui(lambda: self._observed(step, marker))
                if observation is not None:
                    result = dict(artifacts=[], interactive_receipt=dict(
                        schema=RECEIPT_SCHEMA, operation_id=operation_id, tool=step.tool,
                        action=step.arguments['action'], target=json.loads(json.dumps(self.target)),
                        effect=_EFFECTS[(step.tool, step.arguments['action'])], observation=observation))
                    return validate_result(step, result, scope_dict, {'interactive_dispatch': operation_id})
                if cancelled() or self.closed:
                    raise InterruptedError('Interactive command cancelled; waiting for its native owner.')
                if self.clock() >= deadline:
                    raise TimeoutError('Interactive native command exceeded the reviewed time budget.')
                time.sleep(.025)
        except BaseException:
            if self._dispatched:
                self._cancel_owned()
            raise
        finally:
            self._dispatched = False
            self._execution.release()

    def close(self):
        """Stop only an unsettled owned command; completed playback stays native."""
        self.closed = True
        if self._execution.locked() and self._dispatched and not self.reader.closed:
            self.reader.stop()


def show_reader_assistant(reader):
    """Open from the selected modal reader, so the target is actually reachable."""
    import wx
    from .assistant_dialog import show_assistant
    frame = reader.progress.frame if reader.progress is not None else reader.dialog.GetParent()
    seen = set()
    while frame is not None and not hasattr(frame, 'library') and id(frame) not in seen:
        seen.add(id(frame))
        frame = frame.GetParent()
    if frame is None:
        raise ValueError('The selected reader has no current library owner.')
    bridge = NativeReaderBridge(frame, reader)
    choices, arguments = [], []
    for tool, action, label in (
            ('reader.control', 'select', 'Apply an exact source selection or cursor'),
            ('reader.control', 'prepare', 'Prepare the selected first excerpt, without playback'),
            ('reader.control', 'read', 'Start reading the selected source range'),
            ('reader.control', 'stop', 'Stop the current reading session'),
            ('player.control', 'play', 'Play the exact prepared utterance'),
            ('player.control', 'pause', 'Pause the current prepared utterance'),
            ('player.control', 'resume', 'Resume the current prepared utterance'),
            ('player.control', 'seek', 'Seek using the explicitly entered seconds'),
            ('player.control', 'stop', 'Stop the selected player and release its utterance')):
        try:
            value = bridge.arguments(tool, action)
        except (ValueError, PermissionError, RuntimeError):
            continue
        choices.append(label)
        arguments.append(dict(tool=tool, arguments=value))
    if not choices:
        bridge.close()
        raise ValueError('No native command is currently available. Settle the reader, select text and choose a voice explicitly.')
    picker = wx.SingleChoiceDialog(reader.dialog,
        'Choose a command draft for this exact open source. Preview and Run are still required.',
        'Native reader/player assistant', choices)
    try:
        if picker.ShowModal() != wx.ID_OK:
            return
        index = picker.GetSelection()
        if not 0 <= index < len(arguments):
            raise ValueError('Choose an available native command.')
        draft = dict(schema='carr-assistant-plan/1', steps=[arguments[index]],
            summary='One reviewed command for this exact open source and native reader session. '
                    'Playback start does not mean the entire selected text finished.')
        show_assistant(frame, parent=reader.dialog, interactive=bridge,
                       initial_goal=json.dumps(draft, ensure_ascii=False, indent=2))
    finally:
        picker.Destroy()
        bridge.close()
