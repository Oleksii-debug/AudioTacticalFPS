"""Native, keyboard-first local script production and narration preparation.

The controller has no wx dependency. Native windows delegate synthesis and
mixing to the reviewed local production service, never execute script text,
and preserve original text separately from explicit performance edits.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..reader.narration_text import NarrationDraft, decode_json, read_document, text_digest
from ..research.export_file import write_export, _destination, _filename
from ..audio.jobs import checked_directory, publish_noreplace
from ..audio.runtime import resolve_ffmpeg
from ..audio.managed import ManagedProjects


def _plain(value, label, *, limit=2048, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        raise ValueError(f'Enter a valid {label}.')
    text_digest(value)
    return value


def _local_path(value, label):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f'Choose a local {label}.')
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//')) or '://' in raw:
        raise ValueError(f'Choose a local {label}; network locations are not enabled.')
    return Path(raw).absolute()


def _path_digest(path, *, budget=64 * 1024 * 1024):
    return hashlib.sha256(read_document(path, byte_budget=budget)).hexdigest()


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'


def _voice_dict(voice):
    if is_dataclass(voice):
        return asdict(voice)
    if isinstance(voice, dict):
        return voice
    return {name: getattr(voice, name, '') for name in ('id', 'name', 'languages', 'engine', 'available', 'reason')}


def _runtime_description(selected):
    try:
        return 'FFmpeg: ' + str(resolve_ffmpeg(selected) or 'not found')
    except (OSError, ValueError, RuntimeError) as exc:
        return 'FFmpeg unavailable: ' + str(exc) + '. Choose a verified local executable to continue.'


def plan_description(plan):
    """Readable plan plus exact validated directions; no source quote claims."""
    value = plan.to_dict() if hasattr(plan, 'to_dict') else asdict(plan)
    lines = ['Local production plan', 'The script is a performance copy. Original source text stays unchanged.',
             'Review voices, pauses and music before choosing Render / resume.', '']

    def explain(item, depth=0, label=None):
        prefix = '  ' * depth
        if isinstance(item, dict):
            if label:
                lines.append(prefix + label.replace('_', ' ').capitalize() + ':')
            for key, child in item.items():
                # Opaque identifiers are retained in the saved plan; users need
                # the actual performance decisions in this preview.
                if key in ('id', 'schema', 'version', 'sha256', 'source_sha256', 'script_sha256'):
                    continue
                explain(child, depth + bool(label), key)
        elif isinstance(item, (list, tuple)):
            if label:
                lines.append(prefix + label.replace('_', ' ').capitalize() + ':')
            if not item:
                lines.append(prefix + '  None')
            for index, child in enumerate(item, 1):
                explain(child, depth + 1, str(index))
        else:
            display = 'yes' if item is True else 'no' if item is False else str(item)
            lines.append(prefix + ((label.replace('_', ' ').capitalize() + ': ') if label else '') + display)
    explain(value)
    return '\n'.join(lines)


class StudioController:
    """Project lifecycle, explicit reading changes and cancellable local renders."""

    def __init__(self, *, private_root, registry=None, parser=None, renderer=None,
                 ffmpeg=None, loading_budget=64 * 1024 * 1024):
        self.private_root = Path(private_root).absolute()
        self.registry = registry
        self._owns_registry = registry is None
        self.parser, self.renderer = parser, renderer
        self.ffmpeg = Path(ffmpeg) if ffmpeg else None
        self.loading_budget = loading_budget
        self.project_path = None
        self.output_dir = None
        self._file_hash = None
        self._saved_value = None
        self._recovery_hash = None
        self._render_lock = threading.Lock()
        self.result = None
        self.new()

    def new(self, script='', *, title='Audio project', source_id='independent-script', source_reference=None,
            input_kind='narration'):
        if input_kind not in ('narration', 'script'):
            raise ValueError('Choose literal narration or a structured production script.')
        draft = NarrationDraft(script, source_id=source_id)
        title = _plain(title, 'project title', limit=256)
        self.draft, self.title = draft, title
        self.input_kind = input_kind
        self.voice, self.language = 'sapi5:default', 'en'
        self.project_id = uuid4().hex
        self.source_reference = source_reference
        self.project_path = self.output_dir = self.result = None
        self._file_hash = self._saved_value = self._recovery_hash = None

    def from_reading_project(self, project, *, saved_digest):
        """Take one saved derivative into literal narration without writing back."""
        from ..reader.narration import reading_project_reference
        reference = reading_project_reference(project, saved_digest=saved_digest)
        source_id = f"reading-project:{project.id}:revision:{reference['revision']}"
        draft = NarrationDraft(project.original_text, source_id=source_id)
        draft.edit_text(project.current_text, reason=f"Saved reading-copy revision {reference['revision']}")
        self.new(project.original_text, title=project.title, source_id=source_id,
                 source_reference=reference, input_kind='narration')
        self.draft = draft
        return self

    def _protect_original(self, selected):
        from ..reader.narration import READING_REFERENCE_KIND, protect_original_path
        reference = self.source_reference
        if isinstance(reference, dict) and reference.get('kind') in ('local-text', READING_REFERENCE_KIND):
            protect_original_path(reference.get('original_path'), selected)

    @property
    def script(self):
        return self.draft.text

    def edit_script(self, text):
        changed = self.draft.edit_text(text, reason='Explicit script edit')
        if changed:
            self.result = None
        return changed

    def configure(self, *, title=None, voice=None, language=None, input_kind=None, output_dir=None, ffmpeg=None):
        # Validate every supplied field before changing any state.
        values = {}
        if title is not None:
            values['title'] = _plain(title, 'project title', limit=256)
        if voice is not None:
            values['voice'] = _plain(voice, 'local voice', limit=2048)
        if language is not None:
            values['language'] = _plain(language, 'language code', limit=80)
        if input_kind is not None:
            if input_kind not in ('narration', 'script'):
                raise ValueError('Choose literal narration or a structured production script.')
            values['input_kind'] = input_kind
        if output_dir is not None:
            values['output_dir'] = self._output_folder(output_dir)
        if ffmpeg is not None:
            candidate = _local_path(ffmpeg, 'FFmpeg executable')
            if not candidate.is_file():
                raise ValueError('The selected FFmpeg executable does not exist.')
            values['ffmpeg'] = candidate
        for key, value in values.items():
            if getattr(self, key) != value and key in ('voice', 'language', 'input_kind'):
                self.result = None
            if key == 'ffmpeg' and self.ffmpeg != value and self._owns_registry:
                # Installed Flite capabilities belong to the selected runtime.
                # An earlier discovery must not retain the previous binary.
                self.registry = None
            setattr(self, key, value)

    def _output_folder(self, path):
        candidate = _local_path(path, 'output folder')
        checked_directory(candidate)
        if candidate.resolve().is_relative_to(self.private_root.resolve()):
            managed = (ManagedProjects(self.private_root).locate(self.project_path, self.project_id)
                       if self.project_path is not None else None)
            if managed is None or candidate.absolute() != managed / 'audio':
                raise ValueError('Choose an audio output folder outside the private library or use this managed project audio folder.')
        elif self.project_path is not None and ManagedProjects(self.private_root).locate(self.project_path, self.project_id):
            raise ValueError('This project saves audio with its library backup. Use Save As for a standalone project before choosing an external audio folder.')
        for parent in (candidate, *candidate.parents):
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Choose an output folder without links or reparse points.')
        return candidate

    def _registry(self):
        if self.registry is None:
            from ..audio.engines import default_registry
            self.registry = default_registry(ffmpeg=resolve_ffmpeg(self.ffmpeg))
        return self.registry

    def voices(self, *, reload=False, cancelled=None):
        if reload:
            from ..audio.engines import default_registry
            self.registry = default_registry(ffmpeg=resolve_ffmpeg(self.ffmpeg))
            self._owns_registry = True
        registry = self._registry()
        voices = registry.voices() if cancelled is None else registry.voices(cancelled=cancelled)
        return tuple(_voice_dict(voice) for voice in voices)

    def voice_status(self):
        registry = self._registry()
        if not hasattr(registry, 'capabilities'):
            return ''
        return '\n'.join(f"{item.get('engine', 'Voice engine')}: " +
                         ('available' if item.get('available') else item.get('reason', 'not installed'))
                         for item in registry.capabilities())

    def plan(self):
        if not self.script.strip():
            raise ValueError('Enter or import a script before previewing or rendering.')
        parser = self.parser
        if parser is None:
            from ..audio.production import parse_script, plan_narration
            parser = plan_narration if self.input_kind == 'narration' else parse_script
        self.draft.assert_source()
        return parser(self.script, title=self.title, voice=self.voice, language=self.language)

    def preview(self):
        return plan_description(self.plan())

    def to_dict(self):
        # Runtime executable paths and credentials are deliberately not imported
        # from projects. A project cannot choose a helper program on reopening.
        return {'kind': 'research-audio-studio', 'version': 2, 'mode': 'LOCAL ONLY',
                'project_id': self.project_id, 'title': self.title, 'voice': self.voice,
                'language': self.language, 'input_kind': self.input_kind,
                'output_dir': str(self.output_dir) if self.output_dir else None,
                'source_reference': self.source_reference, 'draft': self.draft.to_dict()}

    @property
    def dirty(self):
        return self._saved_value != _json_text(self.to_dict())

    def _write_checked(self, selected, value, expected, *, cancelled=lambda: False):
        def unchanged():
            if cancelled():
                return True
            if expected is None:
                if os.path.lexists(selected):
                    raise FileExistsError('The destination appeared while saving. Choose another filename.')
            elif not os.path.lexists(selected) or _path_digest(selected, budget=self.loading_budget) != expected:
                raise ValueError('The saved destination changed while saving. Reopen it or use Save As.')
            return False
        if expected is not None:
            # Keep the expected bytes across write_export's preflight and its
            # final existing-file replacement checks.
            write_export(selected, value, private_root=self.private_root, cancelled=unchanged)
            return
        # A new project/recovery/export must never replace a file that appears
        # after the last preflight check. Validate the real filename and route,
        # then stage through the existing text-export safeguards and publish
        # with the OS's atomic no-replace operation.
        _filename(selected.name)
        target, parent_identity, previous = _destination(selected, self.private_root)
        if previous is not None:
            raise FileExistsError('The destination appeared while saving. Choose another filename.')
        with TemporaryDirectory(prefix='.audio-save-', dir=target.parent) as folder:
            stage = Path(folder) / 'document.json'
            write_export(stage, value, private_root=self.private_root, cancelled=unchanged)
            if unchanged():
                raise TimeoutError('Export cancelled; the destination was not changed.')
            checked, parent, current = _destination(selected, self.private_root)
            if checked != target or parent != parent_identity or current is not None:
                raise ValueError('The save destination changed. Choose another filename.')
            publish_noreplace(stage, target)

    def save(self, path=None, *, overwrite=False, cancelled=lambda: False):
        selected = _local_path(path or self.project_path or '', 'project file')
        self._protect_original(selected)
        same = self.project_path is not None and selected == self.project_path
        expected = None
        if selected.exists():
            expected = _path_digest(selected, budget=self.loading_budget)
            if same:
                if self._file_hash != expected:
                    raise ValueError('The saved project changed outside Audio Studio. Use Save As to preserve both versions.')
            elif not overwrite:
                raise FileExistsError('Choose a new project filename or confirm replacement.')
        elif same and self._file_hash is not None:
            raise ValueError('The saved project was removed. Use Save As to choose a destination.')
        managed = ManagedProjects(self.private_root)
        selected_root = managed.locate(selected, self.project_id)
        value_data = self.to_dict()
        detach_output = (selected_root is None and self.project_path is not None
                         and managed.locate(self.project_path, self.project_id) is not None)
        if detach_output:
            value_data['output_dir'] = None
        value = _json_text(value_data)
        if selected_root is not None:
            managed.write(self.project_id, selected, value,
                          expected_sha256=expected, cancelled=cancelled)
        else:
            self._write_checked(selected, value, expected, cancelled=cancelled)
        if detach_output:
            self.output_dir = None
        self.project_path = selected
        self._file_hash = hashlib.sha256(value.encode('utf-8')).hexdigest()
        self._saved_value = value
        return selected

    def save_in_library(self, *, cancelled=lambda: False):
        """Save a new project and its output in the complete-backup namespace."""
        managed = ManagedProjects(self.private_root)
        _, path, output = managed.paths(self.project_id)
        if self.project_path is not None and managed.locate(self.project_path, self.project_id) is None:
            raise ValueError('An external project remains standalone. Open or create a library project to use complete library backups.')
        if self.output_dir is not None and self.output_dir.absolute() != output:
            raise ValueError('Use Save As for the selected external output folder, or clear it to save this project inside the library.')
        previous = self.output_dir
        self.output_dir = output
        try:
            return self.save(path, cancelled=cancelled)
        except BaseException:
            self.output_dir = previous
            raise

    def _rebind_managed_output(self, path, values):
        root = ManagedProjects(self.private_root).locate(path, values['project_id'])
        if root is not None:
            # A portable library never follows the old absolute output path.
            # Keep original saved bytes/hash for recovery-base authentication.
            values['output_dir'] = root / 'audio'
        return values

    def _validated(self, payload):
        keys = {'kind', 'version', 'mode', 'project_id', 'title', 'voice', 'language',
                'output_dir', 'source_reference', 'draft'}
        if isinstance(payload, dict) and payload.get('version') == 2:
            keys.add('input_kind')
        if (type(payload) is not dict or set(payload) != keys or payload['kind'] != 'research-audio-studio'
                or type(payload['version']) is not int or payload['version'] not in (1, 2)
                or payload['mode'] != 'LOCAL ONLY'):
            raise ValueError('This is not a supported local Audio Studio project.')
        identity = payload['project_id']
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('The project identity is invalid.')
        values = {'project_id': identity,
                  'title': _plain(payload['title'], 'project title', limit=256),
                  'voice': _plain(payload['voice'], 'local voice'),
                  'language': _plain(payload['language'], 'language code', limit=80),
                  'draft': NarrationDraft.from_dict(payload['draft'])}
        output = payload['output_dir']
        # Saved destinations are suggestions until Render revalidates them. The
        # project remains readable when an external drive is disconnected.
        values['output_dir'] = _local_path(output, 'output folder') if output else None
        reference = payload['source_reference']
        if reference is not None:
            if not isinstance(reference, dict) or len(_json_text(reference)) > self.loading_budget:
                raise ValueError('The source reference is invalid.')
            from ..reader.narration import READING_REFERENCE_KIND, validate_reading_project_reference
            if reference.get('kind') == READING_REFERENCE_KIND:
                validate_reading_project_reference(reference, original_text=values['draft'].source_text,
                                                   source_id=values['draft'].source_id)
        values['source_reference'] = reference
        # Legacy independent scripts retain their established interpretation.
        # Imported sources use literal narration, including legacy page breaks.
        legacy_kind = 'narration' if reference is not None else 'script'
        input_kind = payload.get('input_kind', legacy_kind)
        if input_kind not in ('narration', 'script'):
            raise ValueError('The saved narration or script mode is invalid.')
        values['input_kind'] = input_kind
        return values

    def open(self, path):
        selected = _local_path(path, 'project file')
        data = read_document(selected, byte_budget=self.loading_budget)
        payload = decode_json(data.decode('utf-8'))
        values = self._rebind_managed_output(selected, self._validated(payload))
        for key, value in values.items():
            setattr(self, key, value)
        self.project_path = selected
        self._file_hash = hashlib.sha256(data).hexdigest()
        self._saved_value = _json_text(self.to_dict())
        self._recovery_hash = None
        self.result = None
        return self

    def save_recovery(self, *, cancelled=lambda: False):
        if self.project_path is None or self._file_hash is None:
            return None
        path = self.project_path.with_name(self.project_path.name + '.recovery.json')
        self._protect_original(path)
        expected = None
        if path.exists():
            raw = read_document(path, byte_budget=self.loading_budget)
            actual = hashlib.sha256(raw).hexdigest()
            expected = actual
            if self._recovery_hash is None:
                previous = decode_json(raw.decode('utf-8'))
                if (not isinstance(previous, dict) or previous.get('kind') != 'audio-studio-recovery'
                        or previous.get('project_id') != self.project_id):
                    raise ValueError('The recovery filename is already used by another document.')
            elif actual != self._recovery_hash:
                raise ValueError('The recovery draft changed outside Audio Studio.')
        payload = {'kind': 'audio-studio-recovery', 'version': 1, 'project_id': self.project_id,
                   'base_sha256': self._file_hash, 'project': self.to_dict()}
        text = _json_text(payload)
        managed = ManagedProjects(self.private_root)
        if managed.locate(path, self.project_id) is not None:
            managed.write(self.project_id, path, text,
                expected_sha256=expected,
                cancelled=cancelled)
        else:
            self._write_checked(path, text, expected, cancelled=cancelled)
        self._recovery_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        return path

    def recover(self):
        if self.project_path is None:
            raise ValueError('Open the saved project before recovering its draft.')
        path = self.project_path.with_name(self.project_path.name + '.recovery.json')
        raw = read_document(path, byte_budget=self.loading_budget)
        payload = decode_json(raw.decode('utf-8'))
        if (type(payload) is not dict or set(payload) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                or payload['kind'] != 'audio-studio-recovery' or type(payload['version']) is not int
                or payload['version'] != 1 or payload['project_id'] != self.project_id
                or payload['base_sha256'] != self._file_hash
                or _path_digest(self.project_path, budget=self.loading_budget) != self._file_hash):
            raise ValueError('This recovery draft belongs to a different saved project revision.')
        values = self._rebind_managed_output(self.project_path, self._validated(payload['project']))
        if values['project_id'] != self.project_id:
            raise ValueError('The recovery draft belongs to a different project.')
        for key, value in values.items():
            setattr(self, key, value)
        self._recovery_hash = hashlib.sha256(raw).hexdigest()
        self.result = None

    def render(self, *, cancelled=lambda: False, on_status=lambda status: None):
        if not self._render_lock.acquire(blocking=False):
            raise ValueError('A render is already running in this studio.')
        try:
            if cancelled():
                raise InterruptedError('Render cancelled before starting.')
            plan = self.plan()
            if self.output_dir is None:
                raise ValueError('Choose the audio output folder first.')
            folder = self._output_folder(self.output_dir)
            ffmpeg = resolve_ffmpeg(self.ffmpeg)
            if ffmpeg is None or not ffmpeg.is_file():
                raise ValueError('Install a reviewed local FFmpeg build or choose its executable in Production.')
            renderer = self.renderer
            if renderer is None:
                from ..audio.production import render_project
                renderer = render_project
            result = renderer(plan, folder, engine=self._registry(), ffmpeg=ffmpeg,
                              cancelled=cancelled, on_status=on_status)
            if not isinstance(result, dict) or result.get('status') != 'complete':
                raise ValueError('The production service did not confirm a complete audio render.')
            output = _local_path(result.get('output', ''), 'rendered audio file')
            if not output.is_file() or not output.resolve().is_relative_to(folder.resolve()):
                raise ValueError('The render did not produce its expected file in the selected output folder.')
            self.result = result
            return result
        finally:
            self._render_lock.release()

    def export_reading_copy(self, path, *, overwrite=False):
        selected = _local_path(path, 'reading-copy filename')
        self._protect_original(selected)
        if selected.exists() and not overwrite:
            raise FileExistsError('Choose a new reading-copy filename or confirm replacement.')
        expected = _path_digest(selected, budget=self.loading_budget) if selected.exists() else None
        self.draft.assert_source()
        self._write_checked(selected, self.script, expected)


# Preserve the desktop import name while every caller shares one playback adapter.
from ..audio.player import WavePlayer as LocalPlayback

SCRIPT_HELP = (
    'Literal narration reads all entered text using the selected voice. Headings, speaker-like '
    'labels and bracketed text stay spoken text. Source page separators become chapter boundaries.\n'
    'Choose Structured production script explicitly to interpret the following directions:\n'
    'Example directives:\n'
    '[role Narrator voice=sapi5:default pan=center language=en]\n'
    '# Chapter one\n'
    'Narrator: The door was closed.\n'
    '[pause 2]\n\n'
    'Put a music directive before the first spoken line of its chapter, for example '
    '[music mood=calm tempo=90 gain=0.12 seed=0].\n\n'
    'Use installed voice identifiers from the voice list. Unknown directions are rejected. '
    'Natural language directions are read as text unless represented by a supported directive. '
    'Local music capabilities depend on the configured engine. No models download automatically.\n\n'
    'Ctrl+S: Save project. Ctrl+Shift+S: Save project as. Ctrl+O: Open project. '
    'F6: move to Production. Ctrl+Enter: Preview plan. '
    'Ctrl+Alt+P: pause or resume playback. Ctrl+Alt+Left/Right: seek ten seconds. '
    'Ctrl+Alt+T: read the current playback position and device. '
    'Tab and Shift+Tab: named native controls. Escape: close, with unsaved-change protection. '
    'Use your normal text editing shortcuts in Script. '
    'The Preparation page applies explicit literal replacements and has separate undo/redo buttons. '
    'The original is read-only. Save the project before rendering to keep resumable work. '
    'Named projects keep an automatic recovery draft while edited; Recover draft restores it explicitly.'
)


class AudioStudioDialog:
    """Native notebook and readable job status, with one cancellable worker."""

    def __init__(self, parent, controller):
        import wx
        self.wx, self.parent, self.controller = wx, parent, controller
        self.dialog = wx.Dialog(parent, title='Audio Studio — Local only', size=(940, 760),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-studio')
        self.cancelled = threading.Event()
        self.busy = self.closed = self.close_pending = False
        self.editing_dirty = False
        self._script_dirty = False
        self.playback = LocalPlayback()
        self.voice_rows = []
        self.controls = []
        self.windows_playback_controls = []
        self._loading = True
        outer = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self.dialog)
        self.notebook.SetName('Audio Studio pages: Script, Preparation, Production, Playback')
        script_page, preparation_page, production_page, playback_page = (wx.Panel(self.notebook) for _ in range(4))
        for page, title in ((script_page, 'Script'), (preparation_page, 'Preparation'),
                            (production_page, 'Production'), (playback_page, 'Playback')):
            self.notebook.AddPage(page, title)
        outer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        script_layout = wx.BoxSizer(wx.VERTICAL)
        self.title = self._text(script_page, script_layout, 'Project title', controller.title)
        script_layout.Add(wx.StaticText(script_page, label='Text interpretation'), 0, wx.ALL, 5)
        self.input_kind = wx.Choice(script_page, choices=['Literal narration — read every line',
                                                        'Structured production script — interpret directions'])
        self.input_kind.SetName('Text interpretation: literal narration or structured production script')
        self.input_kind.SetSelection(0 if controller.input_kind == 'narration' else 1)
        self.controls.append(self.input_kind)
        script_layout.Add(self.input_kind, 0, wx.EXPAND | wx.ALL, 5)
        voice_row = wx.BoxSizer(wx.HORIZONTAL)
        voice_row.Add(wx.StaticText(script_page, label='Default local voice'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voice = wx.Choice(script_page)
        self.voice.SetName('Default local voice; includes voice identifier for script roles')
        self.controls.append(self.voice)
        voice_row.Add(self.voice, 1, wx.RIGHT, 8)
        self._button(script_page, voice_row, 'Reload local voices', self.reload_voices)
        script_layout.Add(voice_row, 0, wx.EXPAND | wx.ALL, 5)
        self.language = self._text(script_page, script_layout, 'Language code, for example uk, sk or en', controller.language)
        script_layout.Add(wx.StaticText(script_page, label='Editable narration or dialogue script'), 0, wx.ALL, 5)
        self.script = wx.TextCtrl(script_page, value=controller.script, style=wx.TE_MULTILINE | wx.TE_RICH2)
        self.script.SetName('Editable narration or dialogue script')
        self.controls.append(self.script)
        script_layout.Add(self.script, 1, wx.EXPAND | wx.ALL, 5)
        script_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._button(script_page, script_buttons, 'Import text', self.import_text)
        self._button(script_page, script_buttons, 'Script and keyboard help', self.help)
        script_layout.Add(script_buttons, 0, wx.ALL, 5)
        script_page.SetSizer(script_layout)

        prep_layout = wx.BoxSizer(wx.VERTICAL)
        self.find = self._text(preparation_page, prep_layout, 'Exact word or phrase to replace', '')
        self.replacement = self._text(preparation_page, prep_layout, 'Pronunciation or reading replacement', '')
        self.replace_all = wx.CheckBox(preparation_page, label='Replace all matching occurrences')
        self.replace_all.SetName('Replace all matching occurrences')
        self.replace_all.SetValue(True)
        self.controls.append(self.replace_all)
        prep_layout.Add(self.replace_all, 0, wx.ALL, 5)
        prep_buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (('Apply literal replacement', self.replace_literal),
                               ('Undo preparation edit', self.undo), ('Redo preparation edit', self.redo),
                               ('Read immutable original', self.show_original), ('Export reading copy', self.export_copy)):
            self._button(preparation_page, prep_buttons, label, handler)
        prep_layout.Add(prep_buttons, 0, wx.EXPAND | wx.ALL, 5)
        self.differences = wx.TextCtrl(preparation_page, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.differences.SetName('Reading-copy changes; original source remains unchanged')
        prep_layout.Add(self.differences, 1, wx.EXPAND | wx.ALL, 5)
        preparation_page.SetSizer(prep_layout)

        prod_layout = wx.BoxSizer(wx.VERTICAL)
        self.output = self._text(production_page, prod_layout, 'Audio output folder', str(controller.output_dir or ''))
        output_row = wx.BoxSizer(wx.HORIZONTAL)
        self._button(production_page, output_row, 'Choose output folder', self.choose_output)
        self._button(production_page, output_row, 'Choose FFmpeg executable', self.choose_ffmpeg)
        prod_layout.Add(output_row, 0, wx.ALL, 5)
        self.runtime = wx.StaticText(production_page, label=_runtime_description(controller.ffmpeg))
        self.runtime.SetName('Local FFmpeg runtime path')
        prod_layout.Add(self.runtime, 0, wx.ALL, 5)
        self.plan_text = wx.TextCtrl(production_page, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.plan_text.SetName('Production plan; exact roles, directions and local output')
        prod_layout.Add(self.plan_text, 1, wx.EXPAND | wx.ALL, 5)
        production_buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._button(production_page, production_buttons, 'Preview plan', self.preview)
        self.render_button = self._button(production_page, production_buttons, 'Render / resume', self.render)
        self.cancel_button = self._button(production_page, production_buttons, 'Cancel studio operation', self.cancel, busy_safe=True)
        self.cancel_button.Disable()
        self._button(production_page, production_buttons, 'Play rendered master', self.play)
        self.windows_playback_controls.append(self._button(production_page, production_buttons,
            'Pause / resume playback', self.pause_resume))
        self._button(production_page, production_buttons, 'Stop playback', self.stop_playback, busy_safe=True)
        prod_layout.Add(production_buttons, 0, wx.ALL, 5)
        device_row = wx.BoxSizer(wx.HORIZONTAL)
        device_row.Add(wx.StaticText(production_page, label='Playback output device'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.playback_device = wx.Choice(production_page, choices=['Windows default output'])
        self.playback_device.SetName('Playback output device; changes apply on the next Play')
        self.playback_device.SetSelection(0)
        self.playback_device_ids = [None]
        self.controls.append(self.playback_device)
        self.windows_playback_controls.append(self.playback_device)
        device_row.Add(self.playback_device, 1, wx.RIGHT, 8)
        self.windows_playback_controls.append(self._button(production_page, device_row,
            'Reload playback devices', self.reload_playback_devices))
        prod_layout.Add(device_row, 0, wx.EXPAND | wx.ALL, 5)
        timeline = wx.WrapSizer(wx.HORIZONTAL)
        timeline.Add(wx.StaticText(production_page, label='Seek to seconds'), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        self.seek_seconds = wx.TextCtrl(production_page, value='0')
        self.seek_seconds.SetName('Playback position in seconds')
        self.controls.append(self.seek_seconds)
        self.windows_playback_controls.append(self.seek_seconds)
        timeline.Add(self.seek_seconds, 0, wx.ALL, 3)
        for label, handler in (('Go to playback position', self.seek_playback),
                               ('Back 10 seconds', lambda event: self.seek_relative(-10)),
                               ('Forward 10 seconds', lambda event: self.seek_relative(10)),
                               ('Read playback status', self.read_playback_status)):
            control = self._button(production_page, timeline, label, handler)
            if handler != self.read_playback_status:
                self.windows_playback_controls.append(control)
        prod_layout.Add(timeline, 0, wx.EXPAND | wx.ALL, 5)
        self.playback_status = wx.TextCtrl(production_page, value='Playback stopped.', style=wx.TE_READONLY)
        self.playback_status.SetName('Playback state, current time, duration and output device')
        prod_layout.Add(self.playback_status, 0, wx.EXPAND | wx.ALL, 5)
        self._last_playback_error = None
        production_page.SetSizer(prod_layout)

        playback_layout = wx.BoxSizer(wx.VERTICAL)
        playback_layout.Add(wx.StaticText(playback_page, label='Local WAV playlist — up to 256 recordings'), 0, wx.ALL, 5)
        self.playlist_choice = wx.Choice(playback_page)
        self.playlist_choice.SetName('Local playback playlist')
        self.controls.append(self.playlist_choice)
        playback_layout.Add(self.playlist_choice, 0, wx.EXPAND | wx.ALL, 5)
        queue_buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (('Add WAV recordings', self.add_recordings), ('Clear playlist', self.clear_playlist),
                               ('Play selected recording', self.play_selected), ('Previous recording', self.previous_recording),
                               ('Next recording', self.next_recording)):
            self._button(playback_page, queue_buttons, label, handler)
        playback_layout.Add(queue_buttons, 0, wx.ALL, 5)
        self.loop_start = self._text(playback_page, playback_layout, 'A–B repeat start, seconds', '0')
        self.loop_end = self._text(playback_page, playback_layout, 'A–B repeat end, seconds', '1')
        loop_buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._button(playback_page, loop_buttons, 'Repeat A–B range', self.apply_loop)
        self._button(playback_page, loop_buttons, 'Clear A–B repeat', self.clear_loop)
        playback_layout.Add(loop_buttons, 0, wx.ALL, 5)
        self.sleep_minutes = self._text(playback_page, playback_layout, 'Sleep timer minutes; zero turns it off', '0')
        sleep_buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._button(playback_page, sleep_buttons, 'Apply sleep timer', self.apply_sleep)
        self._button(playback_page, sleep_buttons, 'Read playback controls', self.read_playback_status, busy_safe=True)
        playback_layout.Add(sleep_buttons, 0, wx.ALL, 5)
        playback_layout.Add(wx.StaticText(playback_page, label='Rendered chapter navigation'), 0, wx.ALL, 5)
        self.segment_choice = wx.Choice(playback_page)
        self.segment_choice.SetName('Playback chapters and segments')
        self.controls.append(self.segment_choice)
        playback_layout.Add(self.segment_choice, 0, wx.EXPAND | wx.ALL, 5)
        segment_buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (('Go to selected segment', self.selected_segment),
                               ('Previous segment', self.previous_segment), ('Next segment', self.next_segment)):
            self.windows_playback_controls.append(self._button(playback_page, segment_buttons, label, handler))
        playback_layout.Add(segment_buttons, 0, wx.ALL, 5)
        playback_page.SetSizer(playback_layout)

        self.status = wx.TextCtrl(self.dialog, value='Local only. Enter a script, choose a local voice and output folder, then preview or render.',
                                  style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Audio Studio status and render progress')
        self.status.SetMinSize((-1, 80))
        outer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        file_buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (('New project', self.new), ('Saved library projects', self.open_library),
                               ('Open project', self.open), ('Save project', self.save),
                               ('Save project as', self.save_as), ('Recover draft', self.recover),
                               ('Back up audio project', self.backup), ('Restore audio snapshot', self.restore)):
            self._button(self.dialog, file_buttons, label, handler)
        close_button = wx.Button(self.dialog, wx.ID_CANCEL, 'Close Audio Studio')
        close_button.SetName('Close Audio Studio')
        close_button.Bind(wx.EVT_BUTTON, self.close)
        file_buttons.Add(close_button, 0, wx.ALL, 3)
        outer.Add(file_buttons, 0, wx.ALL, 5)
        self.dialog.SetSizer(outer)
        self.dialog.SetMinSize((740, 580))
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self._accelerators()
        for field in (self.title, self.language, self.script, self.output):
            field.Bind(wx.EVT_TEXT, self._changed)
        self.voice.Bind(wx.EVT_CHOICE, self._changed)
        self.input_kind.Bind(wx.EVT_CHOICE, self._changed)
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.autosave, self.timer)
        self.timer.Start(30000)
        self.playback_timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER, self.refresh_playback, self.playback_timer)
        self.playback_timer.Start(500)
        self.reload_playback_devices()
        if self.playback.native is None:
            for control in self.windows_playback_controls:
                control.Disable()
        self._loading = False
        self._refresh_text()

    def _text(self, parent, layout, label, value):
        wx = self.wx
        layout.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        control = wx.TextCtrl(parent, value=value)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        self.controls.append(control)
        return control

    def _button(self, parent, layout, label, handler, *, busy_safe=False):
        wx = self.wx
        button = wx.Button(parent, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, handler)
        layout.Add(button, 0, wx.ALL, 3)
        if not busy_safe:
            self.controls.append(button)
        return button

    def _accelerators(self):
        wx = self.wx
        entries = []
        for flags, key, handler in ((wx.ACCEL_CTRL, ord('S'), self.save),
                                    (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), self.save_as),
                                    (wx.ACCEL_CTRL, ord('O'), self.open),
                                    (wx.ACCEL_CTRL, wx.WXK_RETURN, self.preview),
                                    (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('P'), self.pause_resume),
                                    (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_LEFT, lambda event: self.seek_relative(-10)),
                                    (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_RIGHT, lambda event: self.seek_relative(10)),
                                    (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('T'), self.read_playback_status),
                                    (wx.ACCEL_NORMAL, wx.WXK_F6, lambda event: self.notebook.SetSelection(2)),
                                    (wx.ACCEL_NORMAL, wx.WXK_F1, self.help)):
            identity = int(wx.NewIdRef())
            self.dialog.Bind(wx.EVT_MENU, handler, id=identity)
            entries.append((flags, key, identity))
        self.dialog.SetAcceleratorTable(wx.AcceleratorTable(entries))

    def _changed(self, event=None):
        if not self._loading:
            self.editing_dirty = True
            if event is not None and event.GetEventObject() is self.script:
                self._script_dirty = True
            self.dialog.SetTitle('Audio Studio — Local only — unsaved changes')
        if event is not None:
            event.Skip()

    def _message(self, text, *, focus=False):
        if self.closed:
            return
        self.status.ChangeValue(str(text))
        if focus:
            self.status.SetFocus()

    def _error(self, exc):
        self._message(str(exc), focus=True)

    def _capture(self):
        if self.busy:
            raise ValueError('Wait for the current operation or choose Cancel render.')
        index = self.voice.GetSelection()
        voice = self.voice_rows[index]['id'] if 0 <= index < len(self.voice_rows) else self.controller.voice
        output = self.output.GetValue().strip()
        self.controller.configure(title=self.title.GetValue(), voice=voice, language=self.language.GetValue(),
                                  input_kind='narration' if self.input_kind.GetSelection() == 0 else 'script',
                                  output_dir=output if output else None)
        if not output:
            self.controller.output_dir = None
        # Native multiline controls normalize displayed line endings. Merely
        # saving a source must not turn that display normalization into an edit.
        if self._script_dirty:
            self.controller.edit_script(self.script.GetValue())
            self._script_dirty = False
        self.differences.ChangeValue(self.controller.draft.difference_text())

    def _refresh_text(self):
        self._loading = True
        self.title.ChangeValue(self.controller.title)
        self.language.ChangeValue(self.controller.language)
        self.input_kind.SetSelection(0 if self.controller.input_kind == 'narration' else 1)
        self.script.ChangeValue(self.controller.script)
        self.output.ChangeValue(str(self.controller.output_dir or ''))
        self.differences.ChangeValue(self.controller.draft.difference_text())
        for index, item in enumerate(self.voice_rows):
            if item['id'] == self.controller.voice:
                self.voice.SetSelection(index)
                break
        else:
            self.voice.SetSelection(self.wx.NOT_FOUND)
        self.editing_dirty = self.controller.dirty
        self._script_dirty = False
        self.dialog.SetTitle('Audio Studio — Local only' + (' — unsaved changes' if self.editing_dirty else ''))
        self._loading = False

    def _run(self, label, action, done):
        if self.busy or self.closed:
            return
        try:
            self.playback.stop()
        except Exception as exc:
            self._error(exc)
            return
        self.busy = True
        self.cancelled.clear()
        for control in self.controls:
            control.Disable()
        self.cancel_button.Enable()
        self._message(label)
        future = self.executor.submit(action)

        def complete(completed):
            def apply():
                if self.closed:
                    return
                self.busy = False
                for control in self.controls:
                    control.Enable(self.playback.native is not None or control not in self.windows_playback_controls)
                self.cancel_button.Disable()
                try:
                    result = completed.result()
                    done(result)
                except Exception as exc:
                    self._error(exc)
                if self.close_pending:
                    self.close_pending = False
                    self.close()
            self.wx.CallAfter(apply)
        future.add_done_callback(complete)

    def reload_voices(self, event=None):
        def loaded(value):
            rows, details = value
            self.voice_rows = [item for item in rows if item.get('available', True)]
            self.voice.SetItems([f"{item['name']} — {', '.join(item.get('languages') or ())} — {item['id']}"
                                 for item in self.voice_rows])
            chosen = next((index for index, item in enumerate(self.voice_rows)
                           if item['id'] == self.controller.voice), None)
            if chosen is not None:
                self.voice.SetSelection(chosen)
            elif self.voice_rows:
                # Discovery is an explicit setup action; show the choice and
                # mark it as unsaved, never silently switch a saved voice.
                self.voice.SetSelection(self.wx.NOT_FOUND)
            self._message(('Local voices loaded. Choose a voice for this project.' if rows else
                           'No local voice is available. Install/configure a supported local voice and reload.') +
                          ('\n' + details if details else ''), focus=True)
        self._run('Discovering installed local voices',
                  lambda: (self.controller.voices(reload=event is not None,
                      cancelled=self.cancelled.is_set), self.controller.voice_status()), loaded)

    def _file(self, title, *, save=False, text=False):
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, title,
            wildcard='Text (*.txt)|*.txt|All files (*.*)|*.*' if text else 'Audio project (*.json)|*.json',
            defaultFile='audio-project.json' if save and not text else '',
            style=(wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) if save else (wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
        try:
            return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _unsaved(self):
        if not self.editing_dirty and not self.controller.dirty:
            return True
        wx = self.wx
        prompt = wx.MessageDialog(self.dialog, 'Save this project before continuing?', 'Unsaved Audio Studio changes',
                                  wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION)
        try:
            choice = prompt.ShowModal()
        finally:
            prompt.Destroy()
        if choice == wx.ID_CANCEL:
            return False
        return self.save() if choice == wx.ID_YES else True

    def new(self, event=None):
        if self.busy or not self._unsaved():
            return
        if not self._stop_for_project_change():
            return
        self.controller.new()
        self.plan_text.ChangeValue('')
        self._refresh_text()
        self.script.SetFocus()

    def open(self, event=None):
        if self.busy or not self._unsaved():
            return
        path = self._file('Open local Audio Studio project')
        if path is None:
            return
        try:
            if not self._stop_for_project_change():
                return
            self.controller.open(path)
            self._refresh_text()
            self.plan_text.ChangeValue('')
            self._message('Project opened. Choose Recover draft if you want its saved recovery copy.', focus=True)
        except Exception as exc:
            self._error(exc)

    def save(self, event=None):
        if self.busy:
            return False
        try:
            self._capture()
            if self.controller.project_path is None:
                if self.controller.output_dir is not None:
                    return self.save_as()
                self.controller.save_in_library()
            else:
                self.controller.save()
            self._refresh_text()
            self._message('Project saved, including the original and current reading copy.', focus=True)
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def open_library(self, event=None):
        if self.busy or not self._unsaved():
            return
        try:
            projects = ManagedProjects(self.controller.private_root).projects()
            if not projects:
                self._message('There are no saved audio projects in this library yet.', focus=True)
                return
            labels = [f'{title} — project {index}' for index, (title, _path) in enumerate(projects, 1)]
            chooser = self.wx.SingleChoiceDialog(self.dialog, 'Choose a saved audio project.',
                                                'Library audio projects', labels)
            try:
                if chooser.ShowModal() != self.wx.ID_OK:
                    return
                selected = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._stop_for_project_change():
                return
            self.controller.open(projects[selected][1])
            self._refresh_text()
            self.plan_text.ChangeValue('')
            self._message('Library audio project opened. Its output stays with this library.', focus=True)
        except Exception as exc:
            self._error(exc)

    def save_as(self, event=None):
        if self.busy:
            return False
        try:
            self._capture()
            path = self._file('Save Audio Studio project as', save=True)
            if path is None:
                return False
            self.controller.save(path, overwrite=True)
            self._refresh_text()
            self._message('Project saved to ' + str(path), focus=True)
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def recover(self, event=None):
        if self.busy:
            return
        try:
            if self.editing_dirty and not self._unsaved():
                return
            self.controller.recover()
            self._refresh_text()
            self._message('Recovery draft restored. Review it and press Ctrl+S to commit it.', focus=True)
        except Exception as exc:
            self._error(exc)

    def backup(self, event=None):
        if self.busy or not self.save():
            return
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Create a new Audio Studio snapshot',
            defaultFile='audio-project-snapshot.zip', wildcard='Audio snapshot (*.zip)|*.zip', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            destination = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        from ..audio.backup import backup_project
        source, folder = self.controller.project_path, self.controller.output_dir
        self._run('Backing up the idle audio project, recovery draft, assets, cache and completed output.',
            lambda: backup_project(source, destination, project_dir=folder, cancelled=self.cancelled.is_set,
                on_status=lambda message: self.wx.CallAfter(self._message, message)),
            lambda result: self._message(f"Audio project snapshot saved: {result['snapshot']}\n"
                f"{result['files']} files; {result['bytes']} bytes.\n"
                'Research libraries, API profiles and installed voice/music models require their separate backups.', focus=True))

    def restore(self, event=None):
        if self.busy or not self._unsaved():
            return
        wx = self.wx
        chooser = wx.FileDialog(self.dialog, 'Choose an Audio Studio snapshot',
            wildcard='Audio snapshot (*.zip)|*.zip', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            snapshot = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        parent = wx.DirDialog(self.dialog, 'Choose the parent folder for a new restored project',
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if parent.ShowModal() != wx.ID_OK:
                return
            directory = Path(parent.GetPath())
        finally:
            parent.Destroy()
        name = wx.TextEntryDialog(self.dialog, 'Name of a new folder; existing folders cannot be replaced.',
                                  'Restored audio project folder', 'restored-audio-project')
        try:
            if name.ShowModal() != wx.ID_OK:
                return
            basename = name.GetValue()
        finally:
            name.Destroy()
        try:
            from ..research.export_file import _filename
            _filename(basename)
            destination = directory / basename
            if destination.resolve().is_relative_to(self.controller.private_root.resolve()):
                raise ValueError('Restore the audio project outside the private research-library folder.')
        except Exception as exc:
            self._error(exc)
            return
        from ..audio.backup import restore_project

        def restored(result):
            self.controller.open(result['project_file'])
            self._refresh_text()
            self.plan_text.ChangeValue('')
            self._message(f"Audio project verified and restored: {result['directory']}\n"
                'Output now points to the restored audio folder. Render / resume verifies reusable completed chunks. '
                'Use Recover draft for any saved recovery copy. Installed voices and models must be configured locally.', focus=True)
        self._run('Verifying the snapshot before restoring into a new folder.',
            lambda: restore_project(snapshot, destination, cancelled=self.cancelled.is_set,
                on_status=lambda message: self.wx.CallAfter(self._message, message)), restored)

    def autosave(self, event=None):
        if self.busy or self.closed or not self.editing_dirty:
            return
        try:
            self._capture()
            if self.controller.project_path is None:
                if self.controller.output_dir is not None:
                    self._message('Use Save As to keep the draft for the selected standalone output folder.')
                    return
                self.controller.save_in_library()
                self._refresh_text()
                self._message('New audio project saved in the library, including its current draft.')
                return
            self.controller.save_recovery()
            self._message('Recovery draft saved. Ctrl+S commits the project.')
        except Exception as exc:
            self._message('Recovery draft was not saved: ' + str(exc))

    def import_text(self, event=None):
        if self.busy or not self._unsaved():
            return
        path = self._file('Import original UTF-8 or Unicode text for narration', text=True)
        if path is None:
            return
        try:
            payload = read_document(path, byte_budget=self.controller.loading_budget)
            encoding = 'utf-16' if payload.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
            source = payload.decode(encoding)
            if not self._stop_for_project_change():
                return
            self.controller.new(source, title=path.stem, source_id='text-file:' + text_digest(source),
                                source_reference={'kind': 'local-text', 'filename': path.name,
                                                  'original_path': str(path.absolute()),
                                                  'bytes_sha256': hashlib.sha256(payload).hexdigest()})
            self._refresh_text()
            self._message('Text imported as an immutable original with a separate editable reading copy.', focus=True)
        except Exception as exc:
            self._error(exc)

    def replace_literal(self, event=None):
        try:
            self._capture()
            count = self.controller.draft.replace_literal(self.find.GetValue(), self.replacement.GetValue(),
                                                          all_occurrences=self.replace_all.GetValue())
            self.controller.result = None
            self._refresh_text()
            self._message(f'{count} literal replacement(s) applied to the reading copy. Original text unchanged.', focus=True)
        except Exception as exc:
            self._error(exc)

    def undo(self, event=None):
        try:
            self._capture()
            changed = self.controller.draft.undo()
            self.controller.result = None
            self._refresh_text()
            self._message('Reading edit undone.' if changed else 'No reading edit to undo.', focus=True)
        except Exception as exc:
            self._error(exc)

    def redo(self, event=None):
        try:
            self._capture()
            changed = self.controller.draft.redo()
            self.controller.result = None
            self._refresh_text()
            self._message('Reading edit restored.' if changed else 'No reading edit to redo.', focus=True)
        except Exception as exc:
            self._error(exc)

    def _show_text(self, title, value):
        wx = self.wx
        dialog = wx.Dialog(self.dialog, title=title, size=(800, 600),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        reader = wx.TextCtrl(dialog, value=value, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reader.SetName(title)
        layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
        button = wx.Button(dialog, wx.ID_CANCEL, 'Close')
        button.SetName('Close ' + title)
        layout.Add(button, 0, wx.ALL, 8)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)
        reader.SetInsertionPoint(0)
        reader.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def show_original(self, event=None):
        self._show_text('Immutable original text — not edited by narration preparation', self.controller.draft.source_text)

    def help(self, event=None):
        self._show_text('Audio Studio script and keyboard guide', SCRIPT_HELP)

    def export_copy(self, event=None):
        try:
            self._capture()
            path = self._file('Export separate reading copy', save=True, text=True)
            if path is not None:
                self.controller.export_reading_copy(path, overwrite=True)
                self._message('Reading copy exported. Original sources and quotations are unchanged.', focus=True)
        except Exception as exc:
            self._error(exc)

    def choose_output(self, event=None):
        wx = self.wx
        dialog = wx.DirDialog(self.dialog, 'Choose local audio output folder', style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.output.SetValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def choose_ffmpeg(self, event=None):
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Choose reviewed local FFmpeg executable', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.controller.configure(ffmpeg=dialog.GetPath())
                self.runtime.SetLabel('FFmpeg: ' + str(self.controller.ffmpeg))
        except Exception as exc:
            self._error(exc)
        finally:
            dialog.Destroy()

    def preview(self, event=None):
        try:
            self._capture()
            self.plan_text.ChangeValue(self.controller.preview())
            self.notebook.SetSelection(2)
            self.plan_text.SetInsertionPoint(0)
            self.plan_text.SetFocus()
        except Exception as exc:
            self._error(exc)

    def render(self, event=None):
        try:
            self._capture()
            preview = self.controller.preview()
            self.plan_text.ChangeValue(preview)
            self.notebook.SetSelection(2)
            if self.controller.project_path is None and self.controller.output_dir is None:
                if not self.save():
                    return
            if self.controller.output_dir is None:
                self.choose_output()
                self._capture()
            if self.controller.output_dir is None:
                return
            if not self.save():
                return

            def progress(status):
                self.wx.CallAfter(self._message, str(status))

            def finished(result):
                self._message('Audio render completed.\nMaster: ' + result['output'] +
                              '\nDuration: ' + str(result.get('duration_seconds', 'see production report')) +
                              ' seconds.\nChoose Play rendered master to listen.', focus=True)
            self._run('Rendering locally. Completed chunks can be reused after cancellation or restart.',
                      lambda: self.controller.render(cancelled=self.cancelled.is_set, on_status=progress), finished)
        except Exception as exc:
            self._error(exc)

    def cancel(self, event=None):
        self.cancelled.set()
        self._message('Cancellation requested. Waiting for the local worker to checkpoint and stop.', focus=True)

    def play(self, event=None):
        try:
            if self.controller.result is None:
                raise ValueError('Render or resume the current script before playing its master.')
            if self.editing_dirty:
                raise ValueError('The script has unsaved edits. Preview and render it before playing a current result.')
            result_file = Path(self.controller.result['output'])
            selected = self.playback_device.GetSelection()
            device = self.playback_device_ids[selected] if 0 <= selected < len(self.playback_device_ids) else None
            self.playback.play(result_file.parent / 'master.wav' if result_file.suffix.lower() != '.wav' else result_file,
                               device_id=device)
            chapters = self.controller.result.get('chapters', ())
            if chapters:
                from ..audio.ffmpeg import wav_info
                rate = wav_info(self.playback.path).rate
                self.playback.set_segments([(row['start_frame'] / rate, row['title']) for row in chapters])
            else:
                self.playback.set_segments([])
            self._refresh_playback_choices()
            self._last_playback_error = None
            self.refresh_playback()
            self._message('Starting the rendered master. Use Pause / resume, timeline controls or Stop playback.', focus=True)
        except Exception as exc:
            self._error(exc)

    def stop_playback(self, event=None):
        try:
            self.playback.stop()
            self.refresh_playback()
            self._message('Studio playback stopped.', focus=True)
        except Exception as exc:
            self._error(exc)

    def reload_playback_devices(self, event=None):
        from ..audio.playback import playback_capabilities
        capabilities = playback_capabilities()
        self.playback_device_ids = [None] + [row['id'] for row in capabilities['devices']]
        self.playback_device.SetItems(['Windows default output'] +
            [f"Output {row['id'] + 1}: {row['name']}" for row in capabilities['devices']])
        self.playback_device.SetSelection(0)
        if event is not None:
            message = ('Playback devices refreshed. Select an output before Play.' if capabilities['available']
                       else capabilities['reason'])
            self._message(message, focus=True)

    def _stop_for_project_change(self):
        try:
            self.playback.stop()
            self.refresh_playback()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def pause_resume(self, event=None):
        try:
            status = self.playback.status()
            if self.playback.native is None:
                raise ValueError('Pause and resume use the native Windows player.')
            if status.state == 'paused':
                self.playback.resume()
                self._message('Playback resume requested.')
            elif status.state in ('playing', 'starting'):
                self.playback.pause()
                self._message('Playback pause requested.')
            else:
                raise ValueError('Start playback before pausing or resuming.')
        except Exception as exc:
            self._error(exc)

    def seek_playback(self, event=None):
        try:
            self.playback.seek(float(self.seek_seconds.GetValue().strip()))
            self._message('Playback position change requested.')
        except Exception as exc:
            self._error(exc)

    def seek_relative(self, delta):
        try:
            status = self.playback.status()
            if status is None or status.state not in ('playing', 'paused', 'starting'):
                raise ValueError('Start playback before moving through the audio.')
            target = min(status.duration_seconds, max(0, status.position_seconds + delta))
            self.playback.seek(target)
            self.seek_seconds.ChangeValue(str(round(target, 3)))
            self._message('Playback position change requested.')
        except Exception as exc:
            self._error(exc)

    def refresh_playback(self, event=None):
        if self.closed:
            return
        try:
            action = self.playback.poll()
            if action == 'next':
                self._refresh_playback_choices()
            status = self.playback.status()
            if status is None:
                return
            def clock(seconds):
                hours, remainder = divmod(int(seconds), 3600)
                minutes, seconds = divmod(remainder, 60)
                return f'{hours:02}:{minutes:02}:{seconds:02}'
            text = (f'{status.state.capitalize()}: {clock(status.position_seconds)} / '
                    f'{clock(status.duration_seconds)}. Output: {status.device_name or "Windows default"}.')
            if status.position_accuracy == 'external_player':
                text = (f'{status.state.capitalize()}. Duration: {clock(status.duration_seconds)}. '
                        'The local external player does not report its playback position. Output: system default.')
            if self.playback.playlist_index is not None:
                text += f' Playlist {self.playback.playlist_index + 1} of {len(self.playback.playlist)}.'
            if self.playback.ab_loop is not None:
                text += f' Repeating {self.playback.ab_loop[0]:.2f} to {self.playback.ab_loop[1]:.2f} seconds.'
            remaining = self.playback.sleep_remaining_seconds
            if remaining is not None:
                text += f' Sleep in {remaining:.0f} seconds.'
            if action == 'sleep':
                text += ' Sleep timer stopped playback.'
            error = status.error
            if error:
                text += ' ' + error
        except Exception as exc:
            error = str(exc)
            text = 'Playback device error: ' + error
        if error and self._last_playback_error != error:
            self._last_playback_error = error
            self._message(error)
        if self.playback_status.GetValue() != text:
            self.playback_status.ChangeValue(text)

    def read_playback_status(self, event=None):
        self.refresh_playback()
        self._message(self.playback_status.GetValue(), focus=True)

    def _refresh_playback_choices(self):
        self.playlist_choice.SetItems([f'{index + 1}. {path.name}' for index, path in enumerate(self.playback.playlist)])
        if self.playback.playlist_index is not None:
            self.playlist_choice.SetSelection(self.playback.playlist_index)
        elif self.playback.playlist:
            self.playlist_choice.SetSelection(0)
        self.segment_choice.SetItems([f'{seconds:.2f} seconds — {title}' for seconds, title in self.playback.segments])
        if self.playback.segments:
            self.segment_choice.SetSelection(0)

    def add_recordings(self, event=None):
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Add local WAV recordings to the playlist',
                               wildcard='PCM WAV (*.wav)|*.wav', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.playback.enqueue([Path(path) for path in dialog.GetPaths()])
                self._refresh_playback_choices()
                self._message(f'Playlist contains {len(self.playback.playlist)} recordings. Choose Play selected recording.', focus=True)
        except Exception as exc:
            self._error(exc)
        finally:
            dialog.Destroy()

    def _playback_action(self, action):
        try:
            result = action()
            self._refresh_playback_choices()
            self.read_playback_status()
            if result is False or result is None and action in (self.playback.next_segment, self.playback.previous_segment):
                self._message('No further recording or segment in that direction.', focus=True)
        except Exception as exc:
            self._error(exc)

    def clear_playlist(self, event=None):
        self._playback_action(lambda: self.playback.set_playlist([]))

    def play_selected(self, event=None):
        selected = self.playback_device.GetSelection()
        device = self.playback_device_ids[selected] if 0 <= selected < len(self.playback_device_ids) else None
        self._playback_action(lambda: self.playback.play_index(self.playlist_choice.GetSelection(), device_id=device))

    def previous_recording(self, event=None):
        self._playback_action(self.playback.previous_track)

    def next_recording(self, event=None):
        self._playback_action(self.playback.next_track)

    def apply_loop(self, event=None):
        self._playback_action(lambda: self.playback.set_ab_loop(float(self.loop_start.GetValue()), float(self.loop_end.GetValue())))

    def clear_loop(self, event=None):
        self._playback_action(self.playback.clear_ab_loop)

    def apply_sleep(self, event=None):
        def apply():
            minutes = float(self.sleep_minutes.GetValue())
            self.playback.set_sleep_timer(None if minutes == 0 else minutes * 60)
        self._playback_action(apply)

    def selected_segment(self, event=None):
        self._playback_action(lambda: self.playback.seek_segment(self.segment_choice.GetSelection()))

    def previous_segment(self, event=None):
        self._playback_action(self.playback.previous_segment)

    def next_segment(self, event=None):
        self._playback_action(self.playback.next_segment)

    def close(self, event=None):
        if self.closed:
            return
        if self.busy:
            self.close_pending = True
            self.cancel()
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if not self._unsaved():
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        try:
            self.playback.close()
        except Exception as exc:
            self._error(exc)
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self.closed = True
        self.cancelled.set()
        self.timer.Stop()
        self.playback_timer.Stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)
        else:
            self.dialog.Hide()

    def show(self):
        try:
            self.reload_voices()
            self.script.SetFocus()
            self.dialog.ShowModal()
        finally:
            self.closed = True
            self.cancelled.set()
            self.timer.Stop()
            self.playback_timer.Stop()
            try:
                self.playback.close()
            finally:
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.dialog.Destroy()


class AudioStudioActions:
    def __init__(self, frame, *, studio_factory=AudioStudioDialog):
        self.frame, self.studio_factory = frame, studio_factory

    def open(self, event=None):
        frame = self.frame
        if getattr(frame, '_closing', False):
            return
        if getattr(frame, 'working', False) or getattr(frame, 'busy', False):
            frame.report('Wait for the library operation or cancel it before opening Audio Studio.')
            return
        try:
            controller = StudioController(private_root=frame.library.root)
            self.studio_factory(frame, controller).show()
        except Exception as exc:
            frame.report('Audio Studio could not open: ' + str(exc))

    def from_selected_book(self, event=None):
        from ..reader.reading_desk import ReadingDesk
        frame = self.frame
        try:
            if getattr(frame, '_closing', False) or frame.working or getattr(frame, 'busy', False):
                raise ValueError('Wait for the library operation or cancel it first.')
            workspace = frame._workspace()
            library = frame.library
            panel = frame.book.GetPage(1) if frame.book is not None else None
            if panel is None or panel.workspace != workspace or not panel.ready:
                raise ValueError('Select a book in Books and Collections first.')
            selection = panel._selection()
            edition = selection[2]
            if edition is None:
                raise ValueError('Select a book in Books and Collections first.')

            def current():
                return (not getattr(frame, '_closing', False) and frame.library is library
                        and frame.active is not None and frame.active[0] == workspace
                        and frame.book is not None and frame.book.GetPage(1) is panel
                        and panel._selection() == selection)

            def read():
                desk = ReadingDesk(library, workspace, edition, cancelled=frame.cancel_event.is_set)
                texts, references, offset = [], [], 0
                for ordinal in range(1, len(desk.pages) + 1):
                    if frame.cancel_event.is_set():
                        raise InterruptedError('Narration import cancelled.')
                    page = desk.page(ordinal)
                    references.append({'location': asdict(page.location), 'source_start': offset,
                                       'source_end': offset + len(page.raw_text), 'verified': page.verified})
                    texts.append(page.raw_text)
                    offset += len(page.raw_text) + 1
                text = '\f'.join(texts)
                controller = StudioController(private_root=library.root)
                controller.new(text, title=desk.title,
                    source_id=f'library:{workspace}:edition:{edition}:revision:{desk.revision}',
                    source_reference={'kind': 'library-source-reading-copy', 'workspace': workspace,
                                      'edition': edition, 'revision': desk.revision, 'pages': references,
                                      'separator': 'form-feed between exact source pages or text segments'})
                return controller

            def show(controller):
                if not current() or frame.cancel_event.is_set():
                    frame.report('Narration import discarded because the selected source changed or was cancelled.')
                    return
                self.studio_factory(frame, controller).show()
            frame.run_job('Preparing a separate narration copy of the selected source', read, show)
        except Exception as exc:
            frame.report('Narration source was not opened: ' + str(exc))


def install_audio_studio_actions(frame):
    import wx
    actions = AudioStudioActions(frame)
    menu = wx.Menu()
    for label, handler in (('Open Audio Studio...\tCtrl+Alt+A', actions.open),
                           ('Prepare selected book for narration...', actions.from_selected_book)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.menu_bar.Append(menu, 'A&udio')
    frame.audio_studio_actions = actions
    return actions
