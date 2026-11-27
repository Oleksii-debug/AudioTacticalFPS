"""Actual installed Windows MCI pause/seek/resume/close acceptance.

Uses a silent synthetic PCM file and the canonical WavePlayer. No alternate
player, speech provider, physical recording input or listening-quality claim.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows MCI is required'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    from carr_library.speech import player as implementation
    origin = Path(implementation.__file__).resolve()
    if 'site-packages' not in [part.casefold() for part in origin.parts]:
        raise RuntimeError('The player acceptance gate must load the installed package.')
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)
    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError('Actual MCI did not reach its measured playback state.')
            time.sleep(.02)
    try:
        with TemporaryDirectory(prefix='resonance-native-player-') as temporary:
            path = Path(temporary) / 'Silent synthetic PCM — Україна.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 72000)
            original = path.read_bytes()
            player = implementation.WavePlayer()
            actual_mci, commands = player._mci, []
            def observed(command):
                commands.append(command)
                return actual_mci(command)
            player._mci = observed
            try:
                player.play(path)
                check('canonical measured MCI backend', player.backend == 'Windows MCI'
                      and player.position_is_approximate is False)
                wait_for(lambda: player.position_seconds >= .08)
                check('native pause accepted', player.pause() is True)
                before = player.position_seconds
                time.sleep(.15)
                check('native pause position remains stable', player.state == 'paused'
                      and abs(player.position_seconds - before) <= .02)
                first = len(commands)
                player.seek(.7)
                seek_commands = commands[first:]
                check('paused seek issues no native Play or Resume',
                      seek_commands == [f'seek {player._alias} to 700 wait'])
                check('paused seek preserves logical pause and measured target',
                      player.state == 'paused' and abs(player.position_seconds - .7) <= .02)
                position = player.position_seconds
                time.sleep(.15)
                check('seek remains silent until explicit resume',
                      player._mci(f'status {player._alias} mode') == 'stopped'
                      and abs(player.position_seconds - position) <= .02)
                check('explicit resume accepted', player.resume() is True)
                wait_for(lambda: player.position_seconds >= .78)
                player.stop()
                check('native Stop ends playback', player.state == 'stopped')
            finally:
                player.close()
            check('native Close confirms alias closure', player._open is False and player.state == 'stopped')
            renamed = path.with_name('Closed native file.wav')
            path.rename(renamed)
            check('closed PCM is unchanged and releasable', renamed.read_bytes() == original)
            renamed.unlink()
            report = {'result': 'PASS', 'checks': checks, 'package_origin': str(origin),
                'actual_windows_mci': 'PASS', 'synthetic_pcm_sha256': sha256(original).hexdigest(),
                'paused_seek_no_play_command': 'PASS', 'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
