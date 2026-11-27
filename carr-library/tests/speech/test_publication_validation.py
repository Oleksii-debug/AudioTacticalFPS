"""Real publication boundaries after retained-source validation.

All audio is small synthetic PCM; these cases require no voice engine, audio
device, symbolic-link privilege, or network access.
"""
from hashlib import sha256
import os
import wave

import pytest

from carr_library.speech.runtime import Voice, VoiceRegistry


class SyntheticPCM:
    name = 'Synthetic publication fixture'

    def voices(self, *, cancelled=lambda: False):
        return [Voice('publication-fixture', 'Synthetic PCM', self.name, 'en')]

    def render(self, text, voice, destination, settings, cancelled):
        with wave.open(str(destination), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            audio.writeframes(b'\x01\x00' * 220)


@pytest.fixture
def registry():
    result = VoiceRegistry([SyntheticPCM()])
    try:
        yield result
    finally:
        result.close()


def test_valid_callback_publishes_the_exact_measured_wave(tmp_path, registry):
    target = tmp_path / 'speech.wav'
    observed = []

    def validate(staged):
        assert not target.exists()
        with wave.open(str(staged), 'rb') as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                    audio.getnframes()) == (1, 2, 22050, 220)
            assert audio.readframes(220) == b'\x01\x00' * 220
        observed.append((staged, sha256(staged.read_bytes()).hexdigest()))

    result = registry.synthesize('A synthetic publication.', 'publication-fixture',
                                 target, before_publish=validate)

    assert len(observed) == 1
    assert result.path == target and result.frames == 220 and result.chunks == 1
    assert result.sha256 == observed[0][1] == sha256(target.read_bytes()).hexdigest()
    assert not observed[0][0].exists()
    assert not list(tmp_path.glob('.speech-*'))


def test_callback_parent_replacement_rejects_identical_staging_bytes(tmp_path, registry):
    parent = tmp_path / 'selected-output'
    parent.mkdir()
    target = parent / 'speech.wav'
    moved = tmp_path / 'moved-output'
    proof = {}

    def replace_parent(staged):
        relative = staged.relative_to(parent)
        payload = staged.read_bytes()
        proof['sha256'] = sha256(payload).hexdigest()
        proof['original_parent'] = (parent.stat().st_dev, parent.stat().st_ino)
        parent.rename(moved)
        parent.mkdir()
        (parent / 'unrelated.txt').write_bytes(b'Keep the replacement directory intact.')
        staged.parent.mkdir()
        staged.write_bytes(payload)
        proof['replacement_parent'] = (parent.stat().st_dev, parent.stat().st_ino)
        # The production synthesis checksum still matches. The destination
        # directory authority must independently refuse this replacement.
        assert sha256(staged.read_bytes()).hexdigest() == proof['sha256']
        assert (moved / relative).read_bytes() == payload

    with pytest.raises(ValueError, match='output folder changed'):
        registry.synthesize('A synthetic publication.', 'publication-fixture',
                            target, before_publish=replace_parent)

    assert proof['original_parent'] != proof['replacement_parent']
    assert not target.exists() and not (moved / 'speech.wav').exists()
    assert (parent / 'unrelated.txt').read_bytes() == b'Keep the replacement directory intact.'


def test_callback_staging_hardlink_alias_cannot_be_published(tmp_path, registry):
    target = tmp_path / 'speech.wav'
    alias = tmp_path / 'retained-alias.wav'
    retained = []

    def alias_staging(staged):
        retained.append(staged.read_bytes())
        os.link(staged, alias)
        assert staged.stat().st_nlink == 2
        assert alias.read_bytes() == retained[0]

    with pytest.raises(ValueError, match='must not have links'):
        registry.synthesize('A synthetic publication.', 'publication-fixture',
                            target, before_publish=alias_staging)

    assert not target.exists()
    assert alias.read_bytes() == retained[0]
    assert not list(tmp_path.glob('.speech-*'))
