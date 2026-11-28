"""Actual POSIX FIFO substitution must fail before any blocking model read."""
from hashlib import sha256
import os
import threading

from carr_library.extract import language_detection as lid


def test_regular_model_replaced_by_real_fifo_at_open_cannot_block(tmp_path, monkeypatch):
    assert os.name == 'posix', 'Select this native capability only on POSIX.'
    path = tmp_path / 'model'
    raw = b'actual reviewed model bytes'
    path.write_bytes(raw)
    real_open = lid._open_json_file
    opened, failures = [], []
    def substitute_before_open(value):
        path.unlink()
        os.mkfifo(path)
        stream = real_open(value)
        opened.append(stream)
        return stream
    monkeypatch.setattr(lid, '_open_json_file', substitute_before_open)
    def verify():
        try:
            lid._read_pin(path, len(raw), sha256(raw).hexdigest(), lambda: None)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=verify, daemon=True)
    worker.start()
    worker.join(3)
    assert not worker.is_alive(), 'A substituted FIFO blocked the model reader.'
    assert len(failures) == 1 and isinstance(failures[0], lid.DetectorUnavailable)
    assert opened and all(stream.closed for stream in opened)
