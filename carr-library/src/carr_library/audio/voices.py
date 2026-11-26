"""Installed Windows speech voices, isolated plain-text synthesis and WAV export.

Uses the Windows .NET System.Speech interface to SAPI 5. SAPI 4, proprietary
voice installation, cloning and neural-model downloads are not implemented by
this adapter. Voice capabilities come from the installed engine, not a promised
fixed voice catalogue. User text is JSON data; it never becomes PowerShell code
or SSML, and no remote provider is called by the adapter.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import wave


RATE = 24000
MAX_TEXT = 12000  # Per synthesis chunk, not a whole book/project limit.
MAX_REPLY = 256 * 1024
MAX_WAVE = 128 * 1024 * 1024


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str
    gender: str
    age: str
    description: str
    engine: str = 'sapi5'


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    voice_id: str
    sample_rate: int
    frames: int
    sha256: str
    text_sha256: str


# Constant application code only. No input is interpolated into this program.
_SAPI = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($speech.GetInstalledVoices() | Where-Object { $_.Enabled })
    if ($request.operation -eq 'voices') {
        $rows = @($voices | ForEach-Object {
            @{ id=$_.VoiceInfo.Id; name=$_.VoiceInfo.Name;
               language=$_.VoiceInfo.Culture.Name;
               gender=$_.VoiceInfo.Gender.ToString(); age=$_.VoiceInfo.Age.ToString();
               description=$_.VoiceInfo.Description }
        })
        @{ voices=$rows } | ConvertTo-Json -Depth 5 -Compress
    } elseif ($request.operation -eq 'render') {
        $selected = @($voices | Where-Object { $_.VoiceInfo.Id -ceq $request.voice_id })
        if ($selected.Count -ne 1) { throw 'Selected voice is no longer installed or enabled.' }
        if ([IO.File]::Exists($request.output)) { throw 'The staged audio output already exists.' }
        $speech.SelectVoice($selected[0].VoiceInfo.Name)
        if ($speech.Voice.Id -cne $selected[0].VoiceInfo.Id) {
            throw 'Installed voice names are ambiguous; the requested voice was not selected.'
        }
        $speech.Rate = [int]$request.rate
        $speech.Volume = [int]$request.volume
        $format = New-Object -TypeName System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList @(
            24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono)
        $speech.SetOutputToWaveFile([string]$request.output, $format)
        $speech.Speak([string]$request.text)
        $speech.SetOutputToNull()
        @{ voice_id=$selected[0].VoiceInfo.Id; status='complete' } |
            ConvertTo-Json -Compress
    } else { throw 'Unknown local speech operation.' }
} finally { $speech.Dispose() }
'''


def _plain_directory(value):
    path = Path(value).absolute()
    if '..' in path.parts or str(path).startswith(('\\\\', '//')):
        raise ValueError('Choose a local audio output directory without parent traversal.')
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Audio directories must be local directories without links.')
    return path


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Audio files must be regular files without links.')
    return info


def _powershell():
    if sys.platform != 'win32':
        raise RuntimeError('Installed SAPI 5 voices require Windows. No speech was generated.')
    root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    executable = root / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError('The Windows speech host is unavailable.')
    return executable


def _run(request, *, cancelled, timeout):
    if (not callable(cancelled) or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Choose a positive local speech time budget.')
    if cancelled():
        raise InterruptedError('Speech generation cancelled.')
    binary = _powershell()
    raw = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_REPLY:
        raise ValueError('Speech request exceeds its per-chunk size budget.')
    # Files bound captured output and avoid pipe deadlocks for large chunks.
    with tempfile.TemporaryDirectory(prefix='research-speech-host-') as temp:
        root = Path(temp)
        incoming, outgoing, errors = root/'request.json', root/'reply.json', root/'error.txt'
        incoming.write_bytes(raw)
        start = time.monotonic()
        with incoming.open('rb') as inp, outgoing.open('wb') as out, errors.open('wb') as err:
            process = subprocess.Popen(
                [str(binary), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _SAPI],
                stdin=inp, stdout=out, stderr=err, cwd=root,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                while process.poll() is None:
                    if cancelled():
                        raise InterruptedError('Speech generation cancelled.')
                    if time.monotonic() - start > timeout:
                        raise TimeoutError('Local speech generation exceeded its time budget.')
                    if outgoing.stat().st_size > MAX_REPLY or errors.stat().st_size > MAX_REPLY:
                        raise ValueError('The speech engine response exceeded its size budget.')
                    output = request.get('output')
                    if output and Path(output).exists() and Path(output).stat().st_size > MAX_WAVE:
                        raise ValueError('Speech chunk exceeds its audio size budget; split the text.')
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        if cancelled():
            raise InterruptedError('Speech generation cancelled.')
        if outgoing.stat().st_size > MAX_REPLY or errors.stat().st_size > MAX_REPLY:
            raise ValueError('The speech engine response exceeded its size budget.')
        if process.returncode:
            raise RuntimeError('The selected Windows speech engine could not complete the operation.')
        try:
            reply = json.loads(outgoing.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError('The Windows speech engine returned an invalid response.') from exc
        if type(reply) is not dict:
            raise ValueError('The Windows speech engine returned an invalid response.')
        return reply


def list_voices(*, cancelled=lambda: False, timeout=20.0):
    """Discover enabled voices only after an explicit user action; no downloads."""
    reply = _run({'operation': 'voices'}, cancelled=cancelled, timeout=timeout)
    rows = reply.get('voices')
    if set(reply) != {'voices'} or type(rows) is not list or len(rows) > 4096:
        raise ValueError('Invalid installed voice catalogue.')
    result, identities = [], set()
    fields = {'id', 'name', 'language', 'gender', 'age', 'description'}
    for row in rows:
        if (type(row) is not dict or set(row) != fields
                or any(type(value) is not str or '\0' in value or len(value) > 4096
                       for value in row.values())
                or not row['id'] or not row['name'] or row['id'] in identities):
            raise ValueError('Invalid or ambiguous installed voice entry.')
        identities.add(row['id'])
        result.append(Voice(**row))
    return tuple(result)


def render_speech(text, output, *, voice_id, rate=0, volume=100,
                  cancelled=lambda: False, timeout=180.0):
    """Render one plain-text chunk to a new WAV; never overwrite source/output.

    Book-sized work belongs in the durable scene queue. Rate is SAPI's native
    -10..10 control; it is not a promise of exact words or seconds per minute.
    """
    if (type(text) is not str or not text.strip() or len(text) > MAX_TEXT
            or '\0' in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)):
        raise ValueError('Enter nonempty Unicode text within the 12,000-character chunk budget.')
    if type(voice_id) is not str or not voice_id or len(voice_id) > 4096 or '\0' in voice_id:
        raise ValueError('Choose an installed voice.')
    if type(rate) is not int or not -10 <= rate <= 10 or type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError('Speech rate must be -10 to 10 and volume 0 to 100.')
    output = Path(output).absolute()
    if output.suffix.casefold() != '.wav' or not output.name or ':' in output.name:
        raise ValueError('Choose a new local WAV filename.')
    parent = _plain_directory(output.parent)
    if output.exists() or output.is_symlink():
        raise FileExistsError('The speech output already exists; choose a new filename.')
    if not callable(cancelled) or cancelled():
        raise InterruptedError('Speech generation cancelled.')
    with tempfile.TemporaryDirectory(prefix='.speech-', dir=parent) as stage:
        staged = Path(stage)/'speech.wav'
        reply = _run({'operation': 'render', 'voice_id': voice_id, 'text': text,
                      'rate': rate, 'volume': volume, 'output': str(staged)},
                     cancelled=cancelled, timeout=timeout)
        if reply != {'voice_id': voice_id, 'status': 'complete'}:
            raise ValueError('The speech reply does not match the selected voice.')
        info = _regular(staged)
        if not 44 < info.st_size <= MAX_WAVE:
            raise ValueError('The speech output is empty or exceeds its chunk budget.')
        try:
            with wave.open(str(staged), 'rb') as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                        audio.getcomptype()) != (1, 2, RATE, 'NONE') or audio.getnframes() <= 0:
                    raise ValueError('The speech engine returned an unsupported audio format.')
                frames = audio.getnframes()
                remaining = frames
                while remaining:
                    chunk = audio.readframes(min(remaining, 65536))
                    if not chunk or len(chunk) % 2:
                        raise ValueError('The speech output is truncated.')
                    remaining -= len(chunk)//2
        except (wave.Error, EOFError) as exc:
            raise ValueError('The speech output is not a valid WAV file.') from exc
        with staged.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if cancelled():
            raise InterruptedError('Speech generation cancelled before publication.')
        with staged.open('r+b') as stream:
            os.fsync(stream.fileno())
        # A same-filesystem hard link publishes atomically and fails on races.
        os.link(staged, output)
    return SpeechResult(output, voice_id, RATE, frames, digest,
                        hashlib.sha256(text.encode('utf-8')).hexdigest())
