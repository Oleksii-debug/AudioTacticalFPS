"""Explicit runtime acquisition cannot publish partial or rebound downloads."""
import hashlib
import importlib.util
import io
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[2] / 'packaging/acquire_audio_runtime.py'
spec = importlib.util.spec_from_file_location('carr_acquire_audio_runtime', path)
acquire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acquire)


class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/pinned-fixture'


def record(data=b'reviewed public fixture'):
    return {'name': 'fixture.zip', 'url': 'https://example.invalid/never-contacted',
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def test_exact_download_is_atomically_published_and_subsequently_reused(tmp_path, monkeypatch):
    value = b'reviewed public fixture'
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', lambda *a, **k: Response(value))
    first = acquire.download_one(record(value), tmp_path)
    assert first['reused'] is False
    assert (tmp_path / 'fixture.zip').read_bytes() == value
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Reused input must not request network.'))
    assert acquire.download_one(record(value), tmp_path)['reused'] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == ['fixture.zip']


@pytest.mark.parametrize('data', [b'wrong', b'x' * 1000])
def test_wrong_or_over_budget_download_leaves_no_published_or_partial_file(tmp_path, monkeypatch, data):
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', lambda *a, **k: Response(data))
    with pytest.raises(ValueError):
        acquire.download_one(record(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_existing_wrong_input_is_preserved_and_never_downloaded_over(tmp_path, monkeypatch):
    target = tmp_path / 'fixture.zip'
    target.write_bytes(b'important existing file')
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Must not download.'))
    with pytest.raises(ValueError, match='Existing'):
        acquire.download_one(record(), tmp_path)
    assert target.read_bytes() == b'important existing file'


def test_concurrent_destination_creation_is_not_overwritten(tmp_path, monkeypatch):
    value = b'reviewed public fixture'
    def response(*args, **kwargs):
        (tmp_path / 'fixture.zip').write_bytes(b'concurrent owner')
        return Response(value)
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', response)
    with pytest.raises(FileExistsError):
        acquire.download_one(record(value), tmp_path)
    assert (tmp_path / 'fixture.zip').read_bytes() == b'concurrent owner'
    assert len(list(tmp_path.iterdir())) == 1


def test_plain_http_redirect_is_rejected(tmp_path, monkeypatch):
    value = Response(b'reviewed public fixture')
    value.geturl = lambda: 'http://example.invalid/insecure'
    monkeypatch.setattr(acquire.urllib.request, 'urlopen', lambda *a, **k: value)
    with pytest.raises(ValueError, match='insecure'):
        acquire.download_one(record(), tmp_path)
    assert not list(tmp_path.iterdir())
