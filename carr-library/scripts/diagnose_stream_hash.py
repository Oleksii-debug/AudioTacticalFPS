"""Observe the exact pinned stream-hash bytes using synthetic local audio only."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import wave

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio import metadata
from carr_library.audio.process import native_audio_environment, run_process


def main():
    evidence = {"platform": os.name, "files": [], "module": sha256(Path(metadata.__file__).read_bytes()).hexdigest()}
    for executable in ("ffmpeg", "ffprobe"):
        path = Path(shutil.which(executable)).resolve()
        version = subprocess.run([str(path), "-version"], capture_output=True, timeout=15, check=True)
        evidence[executable] = {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                                "version": version.stdout.decode("utf-8", "replace").splitlines()[0]}
    failed = False
    with tempfile.TemporaryDirectory(prefix="carr-stream-hash-") as temporary:
        root = Path(temporary)
        source = root / "synthetic.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            stream.writeframes(b"".join(struct.pack("<h", (n % 100 - 50) * 100) for n in range(24000)))
        output = root / "outputs"
        output.mkdir()
        runtime = AudioRuntime(output, input_roots=(root,), budget=AudioBudget(reserve_disk_bytes=0))
        for suffix in (".flac", ".mp3", ".m4a"):
            encoded = output / ("synthetic" + suffix)
            runtime.convert(source, encoded)
            args = [*runtime._base_args(), *runtime._input_args(encoded),
                    "-map", "0:a:0", "-c:a", "copy", "-f", "streamhash", "-hash", "sha256", "pipe:1"]
            owned = run_process(args, budget=runtime.budget)
            direct = subprocess.run(args, capture_output=True, timeout=30, env=native_audio_environment())
            row = {"suffix": suffix, "source_sha256": sha256(encoded.read_bytes()).hexdigest(),
                   "owned_stdout": repr(owned.stdout), "owned_stderr": repr(owned.stderr),
                   "direct_stdout": repr(direct.stdout), "direct_stderr": repr(direct.stderr),
                   "direct_exit": direct.returncode, "same_stdout": owned.stdout == direct.stdout}
            try:
                row["parsed"] = metadata._stream_hash(runtime, encoded, lambda: False)
            except Exception as exc:
                row["parse_error"] = type(exc).__name__ + ": " + str(exc)
                failed = True
            evidence["files"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
