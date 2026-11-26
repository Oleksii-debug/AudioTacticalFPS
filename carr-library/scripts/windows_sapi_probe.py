"""Native SAPI phase evidence; run under the CI runtime's existing firewall rules.

This probe never changes execution policy, signature/CRL settings or network
rules. Run it BEFORE other SAPI calls to observe the first host on that VM; the
second fresh process observes warmed OS caches, not a guaranteed cold machine.
Only fixed phase names, timings, counts and measured synthetic PCM are printed.
No voice tokens/names, source text, local paths or raw engine stderr are logged.
"""
import argparse
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import wave


def _emit(event):
    print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)


def _record(provider, operation, attempt, *, status, **extra):
    value = {'event': 'request-finished', 'architecture': provider.architecture,
             'operation': operation, 'attempt': attempt, 'status': status,
             'diagnostic': provider.last_request_diagnostic, **extra}
    diagnostic = provider.last_request_diagnostic
    if diagnostic and diagnostic['phases'] and diagnostic['phases'][0]['phase'] == 'script-entered':
        # Wall-clock comparison across processes distinguishes host startup from
        # the worker's monotonic per-stage timings; retain raw UTC values too.
        value['script_entry_after_launch_ms'] = (
            diagnostic['phases'][0]['utc_ms'] - diagnostic['started_utc_ms'])
    _emit(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, choices=(1, 2), default=2)
    parser.add_argument('--discovery-only', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise RuntimeError('This probe requires actual Windows SAPI and PowerShell 5.1.')
    from carr_library.speech.runtime import SapiProvider, SpeechSettings, _wave_info
    from carr_library.speech._io import windows_directory
    failures, hosts = 0, 0
    root = windows_directory()
    with TemporaryDirectory(prefix='carr-sapi-probe-') as temporary:
        for architecture, folder in (('64', 'System32'), ('32', 'SysWOW64')):
            executable = root / folder / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
            if not executable.is_file():
                _emit({'event': 'host-unavailable', 'architecture': architecture})
                continue
            hosts += 1
            provider = SapiProvider(executable, architecture=architecture)
            for attempt in range(1, args.repeats + 1):
                _emit({'event': 'request-started', 'architecture': architecture,
                       'operation': 'voices', 'attempt': attempt, 'timeout_seconds': 90})
                try:
                    voices = provider.voices()
                    if not voices:
                        raise ValueError('No installed voices were returned.')
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    failures += 1
                    _record(provider, 'voices', attempt, status='FAIL', error_type=type(exc).__name__)
                    continue
                _record(provider, 'voices', attempt, status='PASS', voice_count=len(voices))
                if args.discovery_only or attempt != 1:
                    continue
                output = Path(temporary) / f'synthetic-{architecture}.wav'
                # This fixed synthetic sentence starts with XML-looking input and
                # includes shell-looking text; it must remain plain SAPI text.
                text = '<voice>Literal text.</voice> Offline speech. $(1+1); quote " and apostrophe \'.'
                _emit({'event': 'request-started', 'architecture': architecture,
                       'operation': 'render', 'attempt': attempt, 'timeout_seconds': 90})
                try:
                    provider.render(text, voices[0], output,
                                    SpeechSettings(volume=65, timeout_seconds=90), lambda: False)
                    rate, channels, frames, width = _wave_info(output)
                    if width != 2 or frames <= rate // 5:
                        raise ValueError('The native voice must produce measurable PCM16 speech.')
                    peak = 0
                    with wave.open(str(output), 'rb') as audio:
                        while raw := audio.readframes(65536):
                            peak = max(peak, max(abs(item[0]) for item in struct.iter_unpack('<h', raw)))
                    if peak <= 100:
                        raise ValueError('The installed voice produced silent audio.')
                except (OSError, ValueError, RuntimeError, TimeoutError, wave.Error) as exc:
                    failures += 1
                    _record(provider, 'render', attempt, status='FAIL', error_type=type(exc).__name__)
                else:
                    _record(provider, 'render', attempt, status='PASS', frames=frames,
                            sample_rate=rate, channels=channels, peak_pcm16=peak)
    passed = hosts > 0 and failures == 0
    _emit({'event': 'summary', 'status': 'PASS' if passed else 'FAIL', 'hosts': hosts,
           'failed_requests': failures, 'security_policy_changes': False,
           'native_windows_execution': True})
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
