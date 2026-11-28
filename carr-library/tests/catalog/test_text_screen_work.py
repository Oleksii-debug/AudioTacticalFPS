"""Fresh immutable text screening after the actual retained-corpus profile.

These are admission/equivalence and real file-boundary regressions. They do not
claim an uninstrumented speedup or qualify the two-million-segment profile.
"""
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.catalog import intake


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_text_screen_preserves_every_ascii_code_point(prefix):
    for codepoint in range(128):
        data = prefix + b"Ordinary source " + bytes([codepoint]) + b" tail"
        if codepoint < 32 and codepoint not in (9, 10, 12, 13):
            with pytest.raises(intake.IntakeError, match="^binary control character in text$"):
                intake.classify(data, ".txt")
        else:
            assert intake.classify(data, ".txt") == "text/plain"


@pytest.mark.parametrize("first,last", [
    (0x80, 0x800), (0x800, 0xD800), (0xE000, 0x10000), (0x10000, 0x110000),
])
def test_all_non_ascii_unicode_scalars_remain_valid_text(first, last):
    # C1 controls, combining characters, noncharacters and supplementary planes
    # are all permitted by the existing C0-only screening contract.
    text = "Ordinary source " + "".join(map(chr, range(first, last)))
    data = text.encode("utf-8")
    assert data.decode("utf-8-sig") == text
    assert intake.classify(data, ".txt") == "text/plain"


@pytest.mark.parametrize("invalid", [
    b"\x80", b"\xc0\xaf", b"\xe0\x80\x80", b"\xed\xa0\x80",
    b"\xf0\x80\x80\x80", b"\xf4\x90\x80\x80", b"\xe2\x82", b"\xf8\x88\x80\x80\x80",
])
def test_invalid_utf8_precedes_the_control_character_refusal(invalid):
    # A control byte is also present: decoding must still be the first refusal.
    data = b"\xef\xbb\xbfOrdinary source \x00" + invalid + b" tail"
    with pytest.raises(intake.IntakeError, match="^unsupported binary or text encoding$") as error:
        intake.classify(data, ".txt")
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_real_small_snapshot_retains_exact_utf8_and_whitespace_bytes(tmp_path, prefix):
    data = prefix + "Київ\tOriginal\r\nPage\fNext\u0085\u2028\ufeff".encode("utf-8")
    path = tmp_path / "source.txt"
    path.write_bytes(data)
    result = intake.source_snapshot(tmp_path, path.name)
    assert isinstance(result, intake.AssetSnapshot)
    assert result.data == data
    assert result.mime == "text/plain"
    assert result.size == len(data)
    assert result.digest == sha256(data).hexdigest()
    assert path.read_bytes() == data


@pytest.mark.parametrize("phase", ["entry", "after_read"])
@pytest.mark.parametrize("signal", ["return", "interrupted", "timeout"])
def test_real_snapshot_cancellation_never_admits_or_leaks_a_reader(tmp_path, monkeypatch, phase, signal):
    data = "Original source Ї.\r\n".encode("utf-8")
    path = tmp_path / "source.txt"
    path.write_bytes(data)
    opened = []
    classified = []
    calls = 0
    real_open = Path.open
    real_classify = intake.classify
    marker = (InterruptedError("observed original cancellation") if signal == "interrupted"
              else TimeoutError("observed original deadline"))

    def observed_open(selected, mode="r", *args, **kwargs):
        stream = real_open(selected, mode, *args, **kwargs)
        if selected == path and mode == "rb":
            opened.append(stream)
        return stream

    def observed_classify(*args, **kwargs):
        classified.append(args)
        return real_classify(*args, **kwargs)

    def cancelled():
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "entry" else 2):
            if signal == "return":
                return True
            raise marker
        return False

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", observed_open)
        patch.setattr(intake, "classify", observed_classify)
        with pytest.raises(InterruptedError if signal != "timeout" else TimeoutError) as error:
            intake.source_snapshot(tmp_path, path.name, cancelled=cancelled)
    if signal != "return":
        assert error.value is marker
    else:
        assert str(error.value) == "Source intake cancelled."
    assert calls == (1 if phase == "entry" else 2)
    assert len(opened) == (0 if phase == "entry" else 1)
    assert all(stream.closed for stream in opened)
    assert classified == []
    assert path.read_bytes() == data
    assert intake.source_snapshot(tmp_path, path.name).digest == sha256(data).hexdigest()


@pytest.mark.parametrize("same_bytes", [True, False])
def test_actual_replacement_before_open_is_refused_and_owned_reader_closes(tmp_path, monkeypatch, same_bytes):
    original = b"Original source body."
    replacement = original if same_bytes else b"Changed source body!"
    path = tmp_path / "source.txt"
    other = tmp_path / "replacement.txt"
    path.write_bytes(original)
    other.write_bytes(replacement)
    assert path.stat().st_ino != other.stat().st_ino
    real_open = Path.open
    opened = []

    def replace_before_open(selected, mode="r", *args, **kwargs):
        if selected == path and mode == "rb":
            other.replace(path)
        stream = real_open(selected, mode, *args, **kwargs)
        if selected == path and mode == "rb":
            opened.append(stream)
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", replace_before_open)
        with pytest.raises(intake.IntakeError, match="^file replaced$"):
            intake.source_snapshot(tmp_path, path.name)
    assert len(opened) == 1 and opened[0].closed
    assert not other.exists()
    assert path.read_bytes() == replacement
    fresh = intake.source_snapshot(tmp_path, path.name)
    assert fresh.data == replacement and fresh.digest == sha256(replacement).hexdigest()


@pytest.mark.parametrize("corrupt", [True, False])
def test_each_real_snapshot_screens_and_hashes_current_file_bytes(tmp_path, corrupt):
    path = tmp_path / "source.txt"
    original = b"Ordinary source: Alpha."
    changed = b"Ordinary source: \x00lpha." if corrupt else b"Ordinary source: Omega."
    assert len(original) == len(changed)
    path.write_bytes(original)
    first = intake.source_snapshot(tmp_path, path.name)
    path.write_bytes(changed)
    if corrupt:
        with pytest.raises(intake.IntakeError, match="^binary control character in text$"):
            intake.source_snapshot(tmp_path, path.name)
    else:
        second = intake.source_snapshot(tmp_path, path.name)
        assert second.data == changed
        assert second.digest == sha256(changed).hexdigest() != first.digest
    assert first.data == original and first.digest == sha256(original).hexdigest()
    assert path.read_bytes() == changed
    path.write_bytes(original)
    assert intake.source_snapshot(tmp_path, path.name) == first
