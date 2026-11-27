"""Small speech.runtime provider delegating to the reviewed dordiai bridge.

No second worker or synthesizer lives here. The shared registry owns document
chunking and the final render; this provider frames each delegated PCM stream
as a bounded, atomically published WAV utterance.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event, Lock
from time import monotonic
import wave

from .dordiai_voices import (ADAPTER_API_VERSION, DordiaiCancelled, DordiaiTimeout, DordiaiVoicesAdapter,
                             REVIEWED_WORKER_SHA256, SAMPLE_RATE)


class DordiaiSpeechProvider:
    name = "dordiai-voices (local)"
    cancellable_discovery = True

    def __init__(self, runtime_directory, *, python_executable=None,
                 expected_worker_sha256=REVIEWED_WORKER_SHA256):
        self.adapter = DordiaiVoicesAdapter(runtime_directory, python_executable=python_executable,
                                            expected_worker_sha256=expected_worker_sha256)
        self.executable = self.adapter.python
        self._tokens = {}
        self._installation_revision = None
        self._worker_revision = None
        self._render_lock = Lock()
        self._cancel = Event()
        self._installation_id = sha256(os.path.normcase(str(self.adapter.runtime)).encode("utf-8")).hexdigest()

    def voices(self, *, cancelled=lambda: False):
        from ..speech._io import check_cancelled
        from ..speech.runtime import Voice

        check_cancelled(cancelled)
        if not self._render_lock.acquire(blocking=False):
            raise RuntimeError("This dordiai-voices provider already has an active operation.")
        self._cancel.clear()
        stop = lambda: self._cancel.is_set() or bool(cancelled())
        try:
            result, tokens = [], {}
            for item in self.adapter.voices(refresh=True, cancelled=stop):
                check_cancelled(stop)
                for language in item.languages:
                    key = json.dumps([self._installation_id, item.id, language], ensure_ascii=True,
                                     separators=(",", ":"))
                    identity = "dordiai-voices:" + sha256(key.encode("ascii")).hexdigest()
                    tokens[identity] = (item.id, language)
                    result.append(Voice(identity, f"{item.name} ({language})", "dordiai-voices", language,
                                        ("rate", "volume", "pitch"), vendor="dordiai-voices",
                                        license="Separate installed dordiai-voices resource licenses apply.",
                                        timing="PCM duration measured; word timings and pronunciation acceptance unavailable."))
                    if len(result) > 10000:
                        raise ValueError("The installed voice-language inventory exceeds its budget.")
            check_cancelled(stop)
            self._tokens = tokens
            return tuple(result)
        except DordiaiCancelled:
            raise InterruptedError("Voice discovery cancelled.") from None
        finally:
            self._render_lock.release()

    @staticmethod
    def _signature(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _asset_digest(self, path, *, cancelled=lambda: False):
        from ..speech._io import check_cancelled, local_file

        check_cancelled(cancelled)
        path = local_file(path)
        signature = self._signature(path)
        checksum = sha256()
        with path.open("rb") as stream:
            while True:
                check_cancelled(cancelled)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                checksum.update(block)
        check_cancelled(cancelled)
        result = checksum.hexdigest()
        if signature != self._signature(path):
            raise ValueError("The dordiai-voices installation changed while fingerprinting it.")
        return result

    def fingerprint(self, voice_id, *, cancelled=lambda: False):
        """Content identity verified from installed asset bytes.

        Hashing can read a large private installation and remains cancellable.
        File timestamps are not write counters, so each boundary reads the bytes.
        No private asset names or content are returned.
        A changed, added or removed model/profile invalidates render-cache reuse.
        """
        from ..speech._io import check_cancelled, linked

        check_cancelled(cancelled)
        if voice_id not in self._tokens:
            raise ValueError("Reload installed dordiai-voices before selecting this voice.")
        root = self.adapter.runtime
        paths = {self.executable, Path(__file__).resolve(), Path(__file__).with_name("dordiai_voices.py").resolve(),
                 Path(__file__).with_name("_dordiai_probe.py").resolve()}
        manifest = root.parent / "manifest.ini"
        if manifest.is_file():
            paths.add(manifest)
        for path in root.iterdir():
            check_cancelled(cancelled)
            if path.is_file() and path.suffix in {".py", ".json"}:
                paths.add(path)
        def unreadable(_error):
            check_cancelled(cancelled)
            raise ValueError("The dordiai-voices asset inventory could not be read completely.") from None

        for directory in (root / "models", root / "python"):
            check_cancelled(cancelled)
            if not directory.exists():
                continue
            for folder, directories, files in os.walk(directory, followlinks=False, onerror=unreadable):
                check_cancelled(cancelled)
                for name in directories:
                    entry = Path(folder) / name
                    if linked(entry.lstat()) or not entry.resolve().is_relative_to(root):
                        raise ValueError("The dordiai-voices asset tree contains a linked directory.")
                for name in files:
                    paths.add(Path(folder) / name)
                if len(paths) > 100000:
                    raise ValueError("The dordiai-voices installation exceeds the asset inventory budget.")
        identity = sha256()
        for path in sorted(paths, key=lambda value: str(value)):
            check_cancelled(cancelled)
            label = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            identity.update(label.encode("utf-8") + b"\0"
                            + self._asset_digest(path, cancelled=cancelled).encode("ascii") + b"\0")
        check_cancelled(cancelled)
        self._installation_revision = identity.hexdigest()
        return sha256(json.dumps({"adapter": ADAPTER_API_VERSION, "voice": self._tokens[voice_id],
                                  "installation": self._installation_revision}, sort_keys=True).encode("utf-8")).hexdigest()

    def render(self, text, voice, output, settings, cancelled):
        from ..speech._io import check_cancelled, new_output, text_value
        from ..speech.runtime import SpeechSettings

        text_value(text)
        if not isinstance(settings, SpeechSettings):
            raise ValueError("Validated SpeechSettings are required.")
        if settings.punctuation:
            raise ValueError("The dordiai-voices worker does not expose punctuation reading.")
        try:
            profile, language = self._tokens[voice.id]
        except (AttributeError, KeyError):
            raise ValueError("Choose a currently installed dordiai-voices voice and language.") from None
        if voice.engine != "dordiai-voices" or voice.language != language:
            raise ValueError("The selected voice-language pair changed; reload the registry.")
        check_cancelled(cancelled)
        if not self._render_lock.acquire(blocking=False):
            raise RuntimeError("This dordiai-voices provider already has an active render.")
        self._cancel.clear()
        deadline = monotonic() + settings.timeout_seconds
        previous_timeout = self.adapter.request_timeout
        stream = None

        def require_active():
            check_cancelled(cancelled)
            check_cancelled(self._cancel.is_set)
            if monotonic() >= deadline:
                raise TimeoutError("dordiai-voices exceeded the utterance time budget.")

        def stop():
            require_active()
            return False

        try:
            self.adapter.request_timeout = min(float(settings.timeout_seconds), 1800.0)
            before = self.fingerprint(voice.id, cancelled=stop)
            if self._worker_revision is not None and self._worker_revision != self._installation_revision:
                # Base legacy assets can be replaced without a package-index
                # update. Do not render changed assets through stale warm models.
                self.adapter.voices(refresh=True, cancelled=stop)
            self._worker_revision = self._installation_revision
            require_active()
            with new_output(output, cancelled=stop) as staged:
                frames = 0
                try:
                    with wave.open(str(staged), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(SAMPLE_RATE)
                        stream = self.adapter.stream_pcm(text, profile, language,
                                                         speed=2 ** (settings.rate / 10),
                                                         pitch=50 if settings.pitch is None else settings.pitch,
                                                         volume=settings.volume, cancelled=stop)
                        for block in stream:
                            require_active()
                            if (block.sample_rate, block.channels, block.sample_width) != (SAMPLE_RATE, 1, 2):
                                raise ValueError("dordiai-voices changed its required PCM format.")
                            if not block.data or len(block.data) % 2:
                                raise ValueError("dordiai-voices returned invalid 16-bit PCM.")
                            frames += block.frames
                            if frames * 2 + 44 > settings.max_output_bytes:
                                raise ValueError("Speech exceeds the selected audio budget. Split it into chapters.")
                            audio.writeframesraw(block.data)
                finally:
                    if stream is not None:
                        closing, stream = stream, None
                        closing.close()
                require_active()
                if not frames:
                    raise ValueError("dordiai-voices produced no audio.")
                if before != self.fingerprint(voice.id, cancelled=stop):
                    raise ValueError("The dordiai-voices installation changed during rendering; retry.")
                require_active()
        except DordiaiCancelled:
            require_active()
            raise InterruptedError("Speech operation cancelled; no output was published.") from None
        except DordiaiTimeout:
            raise TimeoutError("dordiai-voices exceeded the utterance time budget.") from None
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                self.adapter.request_timeout = previous_timeout
                self._render_lock.release()

    def cancel(self):
        self._cancel.set()
        self.adapter.cancel()

    def close(self):
        self._cancel.set()
        self.adapter.close()


def install_dordiai_provider(registry, configuration=None, *, cancelled=None):
    """Register an explicitly enabled saved local installation, without downloads.

    A missing/disabled configuration does not inspect the filesystem or start a
    process. An enabled configuration is validated before registration.
    """
    if configuration is None:
        return None
    if not isinstance(configuration, dict) or type(configuration.get("enabled")) is not bool:
        raise ValueError("A dordiai-voices configuration needs an explicit enabled boolean.")
    allowed = {"enabled", "runtime_directory", "python_executable", "expected_worker_sha256"}
    if not set(configuration) <= allowed:
        raise ValueError("Unknown dordiai-voices configuration field.")
    if not configuration["enabled"]:
        return None
    if not isinstance(configuration.get("runtime_directory"), str) or not configuration["runtime_directory"]:
        raise ValueError("Choose the installed dordiai-voices kx47Runtime directory.")
    provider = DordiaiSpeechProvider(configuration["runtime_directory"],
                                    python_executable=configuration.get("python_executable"),
                                    expected_worker_sha256=configuration.get("expected_worker_sha256", REVIEWED_WORKER_SHA256))
    try:
        if cancelled is None:
            registry.add_provider(provider)
        else:
            registry.add_provider(provider, cancelled=cancelled)
    except BaseException:
        provider.close()
        raise
    return provider
