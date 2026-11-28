"""Independent negative controls for native loopback evidence."""
import importlib.util
from pathlib import Path
import wave

import pytest

PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_recording_device_probe.py'
spec = importlib.util.spec_from_file_location('recording_device_probe_fixture', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('frequency,accepted', [(997, True), (440, False)])
def test_native_measurement_requires_the_actual_declared_tone(tmp_path, frequency, accepted):
    path = tmp_path / 'independent.wav'
    probe._write_tone(path, frequency=frequency, seconds=1)
    result = probe.measure_tone(path)
    assert result['frames'] == 48000
    assert result['matches_generated_tone'] is accepted
    assert result['rms_pcm16'] > 4000


def test_silent_pcm_cannot_qualify_virtual_capture(tmp_path):
    path = tmp_path / 'silence.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\\0\\0' * 48000)
    result = probe.measure_tone(path)
    assert result['rms_pcm16'] == 0 and result['matches_generated_tone'] is False


def test_truncated_device_pcm_is_not_capture_evidence(tmp_path):
    path = tmp_path / 'truncated.wav'
    probe._write_tone(path, seconds=1)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(ValueError, match='truncated'):
        probe.measure_tone(path)
