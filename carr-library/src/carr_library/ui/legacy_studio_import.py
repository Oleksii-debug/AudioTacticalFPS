"""Native explicit import flow for canonical Studio project recovery.

All document parsing, asset hashing/copying and managed publication use the
existing Studio executor. Native choices authorize exact current input files.
"""
from pathlib import Path

from ..audio.legacy_project import inspect_project, review_project, import_reviewed


def import_canonical_project(dialog, event=None):
    if dialog.busy or dialog.closed or not dialog._unsaved():
        return
    if not dialog._stop_for_project_change():
        return
    wx = dialog.wx
    picker = wx.FileDialog(dialog.dialog, 'Choose a canonical Studio project or retained project JSON',
        wildcard='Project JSON (*.json)|*.json|All files|*',
        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if picker.ShowModal() != wx.ID_OK:
            return
        selected = Path(picker.GetPath()).absolute()
    finally:
        picker.Destroy()
    original = dialog.controller
    original_id, original_root = original.project_id, original.private_root

    def current():
        return (not dialog.closed and not dialog.close_pending
                and dialog.controller is original
                and original.project_id == original_id
                and original.private_root == original_root)

    def inspected(result):
        if not current() or dialog.cancelled.is_set():
            return
        path, digest, source = result
        # A retained path is only a label. Every sound file is selected anew;
        # the chooser never starts in an archived or disconnected directory.
        input_sources, roots = {}, set()
        for scene in source['scenes']:
            for cue in scene['cues']:
                if not current():
                    return
                picker = wx.FileDialog(dialog.dialog,
                    f"Choose current PCM sound for {scene['title']} / {cue['id']}",
                    wildcard='PCM WAV/RF64 (*.wav)|*.wav|All files|*',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    file = Path(picker.GetPath()).absolute()
                finally:
                    picker.Destroy()
                input_sources[scene['id'] + ':' + cue['id']] = file
                roots.add(file.parent)
        if not current():
            return
        selection = dialog.voice.GetSelection()
        available = tuple(row for row in dialog.voice_rows if row.get('available', True))
        voice = (dialog.voice_rows[selection]['id'] if 0 <= selection < len(dialog.voice_rows)
                 else original.voice)
        language = dialog.language.GetValue()
        voices, known = {}, {row['id'] for row in available}
        for role in source['roles']:
            if not role['voice_id'] or role['voice_id'] in known or not available:
                continue
            choices = ['Keep the saved voice identifier; configure it before rendering']
            choices.extend(row['name'] + ' — ' + row['id'] for row in available)
            chooser = wx.SingleChoiceDialog(dialog.dialog,
                'The saved voice for ' + role['name'] + ' is not currently available.\n'
                'Keep its exact identifier or explicitly choose a local replacement.',
                'Imported role voice', choices)
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                chosen = chooser.GetSelection()
                if not 0 <= chosen < len(choices):
                    raise ValueError('Choose a valid local voice mapping.')
                if chosen:
                    voices[role['id']] = available[chosen - 1]['id']
            finally:
                chooser.Destroy()
        if not current():
            return
        dialog._run('Reviewing the selected canonical project and sound inputs',
            lambda: review_project(path, private_root=original_root, default_voice=voice,
                default_language=language, input_roots=tuple(sorted(roots)),
                asset_sources=input_sources, voice_overrides=voices,
                expected_sha256=digest, cancelled=dialog.cancelled.is_set), reviewed)

    def reviewed(proposal):
        if not current() or dialog.cancelled.is_set():
            return
        dialog._show_text('Review imported production script', proposal.script)
        if not current():
            return
        question = wx.MessageDialog(dialog.dialog,
            'Create a new library audio project from this reviewed script?\n\n'
            'The original JSON and source text will be retained exactly. Selected sound '
            'files will be copied independently. The original project and render jobs '
            'remain unchanged. Rendering will use the current local pipeline after '
            'you review its voices and production settings.\n\n'
            f'Default for previously unspecified voices: {proposal.default_voice}\n'
            f'Default for previously unspecified languages: {proposal.default_language}\n'
            f'Selected sound inputs: {len(proposal.assets)}',
            'Import canonical Studio project', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            accepted = question.ShowModal() == wx.ID_YES
        finally:
            question.Destroy()
        if not accepted or not current():
            return

        def publish():
            from .audio_studio import StudioController
            path = import_reviewed(proposal, cancelled=dialog.cancelled.is_set)
            replacement = StudioController(private_root=original_root, registry=original.registry,
                parser=original.parser, renderer=original.renderer, ffmpeg=original.ffmpeg,
                loading_budget=original.loading_budget)
            replacement._owns_registry = original._owns_registry
            return replacement.open(path)

        def opened(replacement):
            if not current():
                return
            dialog.controller = replacement
            dialog._refresh_text()
            dialog.plan_text.ChangeValue('')
            dialog._message('Canonical project imported as a new library audio project. '
                            'Its original JSON and script are retained. Review the production '
                            'settings before rendering.', focus=True)
            dialog.script.SetFocus()

        dialog._run('Importing the reviewed canonical project', publish, opened)

    dialog._run('Inspecting the selected canonical Studio document',
                lambda: inspect_project(selected, cancelled=dialog.cancelled.is_set), inspected)

