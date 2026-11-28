from hashlib import sha256
import zipfile

import pytest

from carr_library.catalog.intake import IntakeError, snapshot


def test_exact_unicode_bytes_and_duplicate(tmp_path):
    data = 'Історія\nSame title\n'.encode()
    for name in ('one.txt', 'two.txt'):
        (tmp_path / name).write_bytes(data)
    a = snapshot(tmp_path, 'one.txt')
    b = snapshot(tmp_path, 'two.txt')
    assert a == b
    assert a.data == data
    assert a.manifest() == {'sha256': sha256(data).hexdigest(), 'mime': 'text/plain', 'size': len(data)}


@pytest.mark.parametrize('name,data', [
    ('x.txt', b'MZfake'), ('x.txt', b'\x7fELF'), ('x.txt', b'%PDF-1.7'),
    ('x.pdf', b'not PDF'), ('x.pdf', b'%PDF'), ('x.txt', b'\x00abc'),
    ('x.txt', b'\xff'), ('x.txt', b''), ('x.exe', b'plain'),
])
def test_mismatches(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)
    with pytest.raises(IntakeError):
        snapshot(tmp_path, name)


@pytest.mark.parametrize('relative', ['../outside.txt', '/a.txt', 'C:/a.txt', 'a\\b.txt', 'a//b.txt', 'NUL.txt', 'a.txt:stream'])
def test_unsafe_path(tmp_path, relative):
    with pytest.raises(IntakeError):
        snapshot(tmp_path, relative)


def test_zip_slip_is_not_extracted(tmp_path):
    p = tmp_path / 'archive.txt'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('../escape.txt', 'unsafe')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'archive.txt')
    assert not (tmp_path.parent / 'escape.txt').exists()


def test_limit(tmp_path):
    (tmp_path / 'x.txt').write_bytes(b'abcde')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'x.txt', max_bytes=4)


def test_symlink(tmp_path):
    (tmp_path / 'real.txt').write_text('hello')
    try:
        (tmp_path / 'link.txt').symlink_to(tmp_path / 'real.txt')
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'link.txt')


def test_pdf_and_rtf_screening(tmp_path):
    # These are header fixtures, not structurally validated documents.
    (tmp_path / 'a.pdf').write_bytes(b'%PDF-1.7\n')
    (tmp_path / 'b.rtf').write_bytes(b'{\\rtf1 hello}')
    assert snapshot(tmp_path, 'a.pdf').mime == 'application/pdf'
    assert snapshot(tmp_path, 'b.rtf').mime == 'application/rtf'


def test_small_default_snapshot_uses_bounded_real_python_memory_and_retains_exact_bytes(tmp_path):
    import tracemalloc
    from carr_library.catalog.intake import AssetSnapshot, source_snapshot
    from carr_library.ingest.retained_assets import RetainedAssets

    raw = ("Original source. Київ.\r\n" * 160).encode("utf-8")
    path = tmp_path / "small.txt"
    path.write_bytes(raw)
    # Warm classification imports outside measurement; the measured operation
    # still performs the real default-limit open, read, screening and hash.
    assert source_snapshot(tmp_path, path.name).data == raw
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline, _ = tracemalloc.get_traced_memory()
        measured = source_snapshot(tmp_path, path.name)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already_tracing:
            tracemalloc.stop()
    assert isinstance(measured, AssetSnapshot)
    assert measured.data == raw
    assert measured.manifest() == {
        "sha256": sha256(raw).hexdigest(), "mime": "text/plain", "size": len(raw)}
    # Generous headroom for a ~4 KiB original; a 64 MiB default-sized
    # BufferedReader allocation cannot pass this actual tracemalloc ceiling.
    assert 0 < peak - baseline < 4 * 1024 * 1024, {
        "peak_python_bytes_above_baseline": peak - baseline, "source_bytes": len(raw)}
    path.write_bytes(b"Later source path change.")
    assert measured.data == raw
    retained_root = tmp_path / "retained"
    retained_root.mkdir()
    assets = RetainedAssets(retained_root)
    retained = assets.retain_snapshot(measured)
    assert retained.read_bytes() == raw
    assert assets.resolve(measured.digest) == retained


class _NativeReadIndex:
    def __init__(self, value, *, error=False):
        self.value, self.error, self.calls = value, error, 0

    def __index__(self):
        self.calls += 1
        if self.error:
            raise TypeError("Actual __index__ refusal.")
        return self.value


class _NativeReadLimit:
    """A valid numeric budget whose addition exposes the existing read API."""
    def __init__(self, requested):
        self.requested = requested
        self.additions = 0

    def __gt__(self, other):
        return 64 > other

    def __le__(self, other):
        return 64 <= other

    def __lt__(self, other):
        return 64 < other

    def __add__(self, other):
        assert other == 1
        self.additions += 1
        return self.requested


def _native_read_argument(case):
    import sys
    return {
        "integer": lambda: 64,
        "index": lambda: _NativeReadIndex(64),
        "none": lambda: None,
        "read-all": lambda: -1,
        "float": lambda: 64.0,
        "bad-index": lambda: _NativeReadIndex(64.0),
        "too-positive": lambda: sys.maxsize + 1,
        "too-negative": lambda: -sys.maxsize - 2,
        "negative": lambda: -2,
        "index-refuses": lambda: _NativeReadIndex(0, error=True),
    }[case]()


@pytest.mark.parametrize("case", [
    "integer", "index", "none", "read-all", "float", "bad-index",
    "too-positive", "too-negative", "negative", "index-refuses",
])
def test_snapshot_preserves_the_actual_native_read_argument_contract(tmp_path, case):
    path = tmp_path / "native.txt"
    raw = b"Exact native read source."
    path.write_bytes(raw)
    native_argument, selected_argument = _native_read_argument(case), _native_read_argument(case)
    native_error = None
    try:
        with path.open("rb") as stream:
            expected = stream.read(native_argument)
    except (TypeError, ValueError, OverflowError) as exc:
        native_error = exc
    budget = _NativeReadLimit(selected_argument)
    if native_error is None:
        result = snapshot(tmp_path, path.name, max_bytes=budget)
        assert result.data == expected == raw
        assert result.manifest() == {
            "sha256": sha256(raw).hexdigest(), "mime": "text/plain", "size": len(raw)}
    else:
        with pytest.raises(type(native_error)) as caught:
            snapshot(tmp_path, path.name, max_bytes=budget)
        assert str(caught.value) == str(native_error)
    assert budget.additions == 1
    if isinstance(native_argument, _NativeReadIndex):
        assert native_argument.calls == selected_argument.calls == 1
    assert path.read_bytes() == raw


@pytest.mark.parametrize("case", ["zero", "negative", "over-limit", "nan", "float", "index-only"])
def test_snapshot_keeps_original_budget_validation_before_native_read(tmp_path, monkeypatch, case):
    from pathlib import Path
    from carr_library.catalog.intake import MAX_BYTES

    path = tmp_path / "budget.txt"
    path.write_bytes(b"x")
    limit = {
        "zero": 0, "negative": -1, "over-limit": MAX_BYTES + 1,
        "nan": float("nan"), "float": 8.0, "index-only": _NativeReadIndex(8),
    }[case]
    expected = TypeError if case in ("float", "index-only") else IntakeError
    reads = []
    genuine_open = Path.open

    def opened(selected, *args, **kwargs):
        if selected == path and (args[0] if args else kwargs.get("mode", "r")) == "rb":
            reads.append(True)
        return genuine_open(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(expected):
        snapshot(tmp_path, path.name, max_bytes=limit)
    assert reads == ([True] if case == "float" else [])
    if case == "index-only":
        assert limit.calls == 0
    assert path.read_bytes() == b"x"


@pytest.mark.parametrize("data,limit", [(b"Exact", 5), (b"x", True)], ids=["exact-limit", "legacy-bool-limit"])
def test_snapshot_exact_limit_and_existing_boolean_limit_stay_compatible(tmp_path, data, limit):
    path = tmp_path / "limit.txt"
    path.write_bytes(data)
    result = snapshot(tmp_path, path.name, max_bytes=limit)
    assert result.data == data
    assert result.manifest() == {
        "sha256": sha256(data).hexdigest(), "mime": "text/plain", "size": len(data)}


@pytest.mark.parametrize("mutation", ["growth", "truncation", "same-size"])
def test_snapshot_rejects_real_changes_after_opened_identity_capture(tmp_path, monkeypatch, mutation):
    import os
    from carr_library.catalog import intake

    path = tmp_path / "changed.txt"
    raw = b"Original exact source."
    path.write_bytes(raw)
    genuine_fstat = os.fstat
    observations = []
    changed = (raw + b"!" if mutation == "growth" else
               raw[:-1] if mutation == "truncation" else b"Changed!" + raw[8:])

    def observed_fstat(descriptor):
        info = genuine_fstat(descriptor)
        observations.append(info)
        if len(observations) == 1:
            with path.open("r+b") as writer:
                writer.write(changed)
                writer.truncate()
                writer.flush()
                os.fsync(writer.fileno())
            if mutation == "same-size":
                # Force observable metadata rather than assume Windows updates
                # a last-write stamp while another descriptor remains open.
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        # Growth/truncation exercise the genuine returned-byte sentinel even
        # when the metadata observation is stale. The reads/writes are real.
        return observations[0] if mutation != "same-size" else info

    monkeypatch.setattr(intake.os, "fstat", observed_fstat)
    with pytest.raises(IntakeError, match="changed during read"):
        snapshot(tmp_path, path.name)
    assert len(observations) == 2
    assert path.read_bytes() == changed
    assert observations[0].st_size == len(raw)
    if mutation == "same-size":
        assert observations[1].st_size == len(raw)
        assert observations[1].st_mtime_ns != observations[0].st_mtime_ns
    else:
        assert observations[1].st_size == len(changed)


def test_snapshot_rejects_same_bytes_replacement_before_native_open(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    path, replacement = tmp_path / "selected.txt", tmp_path / "replacement.txt"
    raw = b"Exact original bytes."
    path.write_bytes(raw)
    replacement.write_bytes(raw)
    genuine_open = Path.open
    replaced = []

    def opened(selected, *args, **kwargs):
        if selected == path and (args[0] if args else kwargs.get("mode", "r")) == "rb" and not replaced:
            os.replace(replacement, path)
            replaced.append(True)
        return genuine_open(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(IntakeError, match="file replaced"):
        snapshot(tmp_path, path.name)
    assert replaced == [True]
    assert path.read_bytes() == raw
    assert not replacement.exists()


@pytest.mark.parametrize("boundary", ["before-read", "after-read"])
def test_snapshot_cancellation_keeps_real_read_boundary_and_original_bytes(tmp_path, monkeypatch, boundary):
    from pathlib import Path

    path = tmp_path / "cancel.txt"
    raw = b"Unchanged original bytes."
    path.write_bytes(raw)
    genuine_open = Path.open
    reads, calls = [], []

    def opened(selected, *args, **kwargs):
        if selected == path and (args[0] if args else kwargs.get("mode", "r")) == "rb":
            reads.append(True)
        return genuine_open(selected, *args, **kwargs)

    def cancelled():
        calls.append(True)
        return len(calls) >= (1 if boundary == "before-read" else 2)

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(InterruptedError, match="Source intake cancelled"):
        snapshot(tmp_path, path.name, cancelled=cancelled)
    assert len(calls) == (1 if boundary == "before-read" else 2)
    assert reads == ([] if boundary == "before-read" else [True])
    assert path.read_bytes() == raw
