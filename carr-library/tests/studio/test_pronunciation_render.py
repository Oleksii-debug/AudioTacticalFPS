"""Actual FFmpeg renders before/after a spoken-line edit; fixture tones are not speech."""
from hashlib import sha256
import math
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import wave

from carr_library.application import LibraryApp
from carr_library.speech.reading import PronunciationRule
from carr_library.studio.project import parse_script
from carr_library.studio.pronunciation import StudioPronunciations
from carr_library.studio.render import ProductionService
from carr_library.ui.pronunciation_dialog import ReadingSnapshot, apply_pronunciation_preview, preview_pronunciations


class ToneVoices:
    def __init__(self):
        self.received = []
    def voices(self):
        return (SimpleNamespace(id='synthetic-tone', name='Synthetic protocol tone', language='en',
                                engine='test fixture'),)
    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id == 'synthetic-tone' and not cancelled()
        self.received.append(text)
        with wave.open(str(destination), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * index / 24000)))
                                       for index in range(4800)))


def test_saved_pronunciation_is_rendered_in_new_job_and_old_verified_outputs_stay_unchanged(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the reviewed FFmpeg/ffprobe test runtime.'
    library = LibraryApp(tmp_path / 'library')
    editor = StudioPronunciations(library.root)
    script = '[Scene: Original]\nNarrator: Dr. First keeps the original.\nReader: Unchanged second line.'
    project = parse_script(script, title='Synthetic pronunciation render', voice_ids=('synthetic-tone',), mode='authored')
    editor.projects.save(project)
    tones = ToneVoices()
    service = ProductionService(library.root / 'state' / 'studio', voices=tones)
    output = tmp_path / 'rendered'; output.mkdir()
    first = service.run(service.submit(project, output, stems=False))
    assert first['status'] == 'complete'
    original_outputs = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in first['artifacts']}
    draft = editor.open_line(project, project.scenes[0].id, project.scenes[0].lines[0].id)
    snapshot = ReadingSnapshot.capture(draft.document, (0, 0), voice_id='synthetic-tone')
    preview = preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'),))
    apply_pronunciation_preview(draft.document, preview, snapshot)
    changed = editor.save_line(draft)
    second = service.run(service.submit(changed.after, output, stems=False))
    assert second['status'] == 'complete' and first['id'] != second['id']
    assert second['fingerprint'] == changed.after.fingerprint != first['fingerprint']
    assert [row['text'] for row in first['result']['lines']] == [
        'Dr. First keeps the original.', 'Unchanged second line.']
    assert [row['text'] for row in second['result']['lines']] == [
        'Doctor First keeps the original.', 'Unchanged second line.']
    assert 'Doctor First keeps the original.' in tones.received
    assert first['result']['source_sha256'] == second['result']['source_sha256'] == sha256(script.encode()).hexdigest()
    assert second['project']['original_script'] == first['project']['original_script'] == script
    assert original_outputs == {path: sha256(Path(path).read_bytes()).hexdigest() for path in original_outputs}
    for artifact in second['artifacts']:
        assert sha256(Path(artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
    with wave.open(str(Path(second['destination']) / 'master.wav'), 'rb') as audio:
        assert audio.getnframes() > 0 and audio.getnchannels() == 2
    assert service.get(first['id']) == first


def test_script_pronunciation_keeps_subtitles_and_reuses_unchanged_line(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe')
    library = LibraryApp(tmp_path / 'library')
    editor = StudioPronunciations(library.root)
    script = 'Narrator: На горі стоїть замок.\n[Вимова: На горі стоїть за́мок.]\nДалі.'
    project = parse_script(script, voice_ids=('synthetic-tone',))
    editor.projects.save(project)
    scene = project.scenes[0]
    draft = editor.open_line(project, scene.id, scene.lines[0].id)
    assert draft.document.text == 'На горі стоїть за́мок.' and not draft.dirty
    tones = ToneVoices()
    service = ProductionService(library.root / 'state' / 'studio', voices=tones)
    output = tmp_path / 'rendered'
    output.mkdir()
    first = service.run(service.submit(project, output, stems=False))
    assert tones.received == ['На горі стоїть за́мок.', 'Далі.']
    draft.document.set_text('На горі стоїть за́мок!', label='New pronunciation take')
    changed = editor.save_line(draft)
    saved = editor.projects.load(project.id)
    assert saved == changed.after and saved.original_script == script
    second = service.run(service.submit(saved, output, stems=False))
    assert [row['text'] for row in second['result']['lines']] == ['На горі стоїть замок.', 'Далі.']
    assert tones.received[2:] == ['На горі стоїть за́мок!']
    assert service.get(first['id']) == first
    reverted = editor.undo_line(changed)
    assert reverted.after.scenes[0].lines[0] == scene.lines[0]
