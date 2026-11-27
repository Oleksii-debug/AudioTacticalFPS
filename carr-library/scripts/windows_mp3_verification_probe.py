"""Measure MP3 encoder/decoder framing using short synthetic PCM only.

This diagnostic does not weaken the production exact-duration acceptance gate.
It publishes counts and packet trim metadata, never an audio artifact.
"""
from array import array
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


def run():
    ffmpeg = Path(os.environ['CARR_FFMPEG']).absolute()
    ffprobe = ffmpeg.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    reports = []
    with tempfile.TemporaryDirectory(prefix='carr-mp3-framing-') as temporary:
        root = Path(temporary)
        for rate in (8000, 16000, 24000, 32000, 44100, 48000):
            frames = rate + 321
            source = root / 'synthetic.wav'
            samples = array('h', (int(5000 * math.sin(index * 2 * math.pi * 440 / rate))
                                 for index in range(frames) for _ in range(2)))
            if sys.byteorder != 'little':
                samples.byteswap()
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((2, 2, rate, frames, 'NONE', 'not compressed'))
                stream.writeframes(samples.tobytes())
            for bitrate in ('128k', '192k'):
                output = root / f'synthetic-{rate}-{bitrate}.mp3'
                common = [str(ffmpeg), '-hide_banner', '-nostdin', '-v', 'error']
                subprocess.run([*common, '-protocol_whitelist', 'file', '-i', str(source),
                    '-map', '0:a:0', '-c:a', 'libmp3lame', '-ar', str(rate),
                    '-b:a', bitrate, '-f', 'mp3', '-n', str(output)],
                    stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30)
                raw = subprocess.run([*common, '-xerror', '-err_detect', 'explode',
                    '-protocol_whitelist', 'file', '-i', str(output), '-map', '0:a:0',
                    '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
                    stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30).stdout
                probe = subprocess.run([str(ffprobe), '-v', 'error', '-protocol_whitelist', 'file',
                    '-show_packets', '-show_streams', '-of', 'json', str(output)],
                    stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30)
                metadata = json.loads(probe.stdout)
                packets = metadata.get('packets', [])
                reports.append({'rate': rate, 'bitrate': bitrate, 'source_frames': frames,
                    'decoded_frames': len(raw) // 4, 'packet_count': len(packets),
                    'trim': [p['side_data_list'] for p in packets if 'side_data_list' in p],
                    'streams': [{k: value for k, value in stream.items() if k in
                                 ('codec_name', 'sample_rate', 'start_time', 'duration', 'bit_rate')}
                                for stream in metadata.get('streams', [])]})
    print(json.dumps({'kind': 'synthetic-mp3-framing-diagnostic', 'measurements': reports}, sort_keys=True))


if __name__ == '__main__':
    run()
