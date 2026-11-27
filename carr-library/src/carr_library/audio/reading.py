"""Bounded literal reading through the existing production and playback services."""
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from ..durable_state import local_path, regular
from .jobs import RenderCancelled, checked_directory, publish_noreplace
from .model_manifest import digest_file
from .production import plan_narration
from .ffmpeg import wav_info

MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_READING_BYTES = 256 * 1024 * 1024


def capture_reading_text(editor, mode):
    """Use native ranges: Windows control positions are not Python code points."""
    if mode == 'all':
        return editor.GetValue()
    if mode == 'selection':
        selected = editor.GetStringSelection()
        return selected if selected else editor.GetValue()
    if mode == 'cursor':
        return editor.GetRange(editor.GetInsertionPoint(), editor.GetLastPosition())
    raise ValueError('Choose all text, the selection, or text from the cursor.')


def reading_plan(text, *, voice, language, rate=0, volume=100):
    if (not isinstance(text, str) or not text.strip()
            or len(text.encode('utf-8')) > MAX_TEXT_BYTES):
        raise ValueError('Choose nonempty reading text within the 16 MiB narration budget.')
    if type(rate) is not int or not -10 <= rate <= 10:
        raise ValueError('Reading rate must be an integer from -10 to 10.')
    if type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError('Reading volume must be an integer from 0 to 100.')
    plan = plan_narration(text, title='Reading-copy playback', voice=voice, language=language)
    return replace(plan, roles=(replace(plan.roles[0], rate=rate, volume=volume / 100),),
                   sample_rate=24000, output_format='wav', export_stems=False,
                   disk_budget_bytes=MAX_READING_BYTES)


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink, value.st_mode,
            getattr(value, 'st_file_attributes', 0))


@dataclass
class ReadingAudio:
    directory: Path
    path: Path
    sha256: str
    text_sha256: str
    duration_seconds: float
    closed: bool = False

    def verify(self, *, cancelled=lambda: False):
        if self.closed:
            raise ValueError('This reading audio has been released. Read the text again.')
        path = local_path(self.path, root=self.directory, exists=True)
        before = regular(path)
        if before.st_size > MAX_READING_BYTES:
            raise ValueError('Reading audio exceeds its byte budget.')
        checksum = digest_file(path, cancelled=cancelled)
        if checksum != self.sha256 or _stamp(regular(path)) != _stamp(before):
            raise ValueError('Reading audio changed. Read the current copy again.')
        wav_info(path)
        return path

    def save_wav(self, destination, *, cancelled=lambda: False):
        source = self.verify(cancelled=cancelled)
        target = local_path(destination)
        if target.suffix.lower() != '.wav':
            raise ValueError('Choose a WAV filename.')
        checked_directory(target.parent)
        if target.is_relative_to(self.directory):
            raise ValueError('Choose a separate export folder.')
        if os.path.lexists(target):
            raise FileExistsError('The WAV destination already exists. Choose a new filename.')
        before = regular(source)
        descriptor, temporary = tempfile.mkstemp(prefix='.reading-export-', dir=target.parent)
        temporary = Path(temporary)
        checksum = sha256()
        total = 0
        try:
            with os.fdopen(descriptor, 'wb') as writer, source.open('rb') as reader:
                opened = os.fstat(reader.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_nlink) != (
                        before.st_dev, before.st_ino, before.st_size, before.st_nlink):
                    raise ValueError('Reading audio changed while opening.')
                while block := reader.read(1024 * 1024):
                    if cancelled():
                        raise RenderCancelled('Reading WAV export cancelled.')
                    total += len(block)
                    if total > MAX_READING_BYTES:
                        raise ValueError('Reading WAV export exceeds its byte budget.')
                    checksum.update(block)
                    writer.write(block)
                if _stamp(os.fstat(reader.fileno())) != _stamp(opened):
                    raise ValueError('Reading audio changed during export.')
                writer.flush()
                os.fsync(writer.fileno())
            if (_stamp(regular(source)) != _stamp(before) or checksum.hexdigest() != self.sha256
                    or total != before.st_size):
                raise ValueError('Reading audio changed during export.')
            if cancelled():
                raise RenderCancelled('Reading WAV export cancelled before publication.')
            publish_noreplace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        """Caller must first confirm that its producer and player have stopped."""
        if not self.closed:
            checked_directory(self.directory)
            shutil.rmtree(self.directory)
            self.closed = True


def render_reading(private_root, text, *, registry, voice, language, rate=0, volume=100,
                   ffmpeg=None, cancelled=lambda: False, on_status=lambda message: None,
                   renderer=None):
    """Keep all temporary output private; no Audio Studio project is silently saved."""
    from ..data_paths import guard_data_directory
    from ..operations.studio import render_with_resources
    plan = reading_plan(text, voice=voice, language=language, rate=rate, volume=volume)
    if cancelled():
        raise RenderCancelled('Reading cancelled before synthesis.')
    root = checked_directory(guard_data_directory(private_root), create=True)
    directory = Path(tempfile.mkdtemp(prefix='.reading-preview-', dir=root))
    try:
        result = render_with_resources(root, plan, directory, engine=registry, ffmpeg=ffmpeg,
            cancelled=cancelled, on_status=on_status, operation_id='reading:' + uuid4().hex,
            renderer=renderer)
        if cancelled():
            raise RenderCancelled('Reading cancelled before playback.')
        target = local_path(result['output'], root=directory, exists=True)
        if target.suffix.lower() != '.wav' or regular(target).st_size > MAX_READING_BYTES:
            raise ValueError('The reading renderer did not return a bounded local WAV file.')
        info = wav_info(target)
        checksum = digest_file(target, cancelled=cancelled)
        audio = ReadingAudio(directory, target, checksum, plan.source_sha256, info.seconds)
        audio.verify(cancelled=cancelled)
        return audio
    except BaseException:
        shutil.rmtree(directory)
        raise
