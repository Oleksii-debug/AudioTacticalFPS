"""Optional, explicitly selected Liblouis 3.39.0 Windows C-API runtime.

No network, package installation, CLI argument passthrough or application-side
download is provided. Tables and binaries are not bundled in this module.
"""
from __future__ import annotations

from hashlib import sha1, sha256
import io
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

from ..audio.models import AudioBudget
from ..audio.process import run_process
from ..speech._io import check_cancelled, linked, local_file

VERSION = "3.39.0"
SOURCE_COMMIT = "2e775aca674cd2910e272eed7736acceb4c2e4a2"
ARCHIVE_URL = "https://github.com/liblouis/liblouis/releases/download/v3.39.0/liblouis-3.39.0-win64.zip"
ARCHIVE_SIZE = 5762099
ARCHIVE_SHA256 = "64d669ac30f1411e0023b1cecc81c7a7b5374678ee41302c95ac8c7c8fbc6591"
MAX_TEXT_CHARACTERS = 100000
MAX_LINES = 4096
MAX_BRF_BYTES = 2 * 1024 * 1024
MAX_STAGE_BYTES = 32 * 1024 * 1024
TABLES = {"en-ueb-g1": "en-ueb-g1.ctb", "uk": "uk.utb"}
_TABLE_PINS = {
    "en-ueb-g1.ctb": {
        "git_blob_sha1": "c95a102781b56e7f93084aeb1a8e99d8f6d5c75a",
        "size": 5368
    },
    "uk.utb": {
        "git_blob_sha1": "2cc6762e76ff28b1dd71567918a21e7c91688bf4",
        "size": 3509
    },
    "en-us-brf.dis": {
        "git_blob_sha1": "f349d50c8152585e86d7ad48fdc6bfe16356e228",
        "size": 2438
    },
    "en-ueb-chardefs.uti": {
        "git_blob_sha1": "b0e5c49d03c64574c29998ae8189832601844af4",
        "size": 27834
    },
    "en-ueb-math.ctb": {
        "git_blob_sha1": "320607c52875b8bf95159be4b2ebbca3bfbb0e14",
        "size": 2045
    },
    "braille-patterns.cti": {
        "git_blob_sha1": "1cb7df65602c1d1b91d29c147106d724f642d96a",
        "size": 26431
    },
    "ru-litbrl.ctb": {
        "git_blob_sha1": "29544dd21279b9c24b6054e866df44355325e90a",
        "size": 33096
    },
    "text_nabcc.dis": {
        "git_blob_sha1": "dde2890c0d3d01afb82da1d69816d78fc73849e1",
        "size": 21650
    },
    "spaces.uti": {
        "git_blob_sha1": "e72b674452c7937347e6941db6d612c594801a36",
        "size": 2108
    },
    "latinLetterDef6Dots.uti": {
        "git_blob_sha1": "9ffefc80ab168a5380ed476b1a23fc5daccba584",
        "size": 1993
    },
    "latinUppercaseComp6.uti": {
        "git_blob_sha1": "2736a9fed2fb999fdc69f50e49da232f1723acb3",
        "size": 1868
    },
    "ru-unicode.dis": {
        "git_blob_sha1": "f702feccbce1196d40f8ceaae742de7ab4952765",
        "size": 3025
    },
    "litdigits6Dots.uti": {
        "git_blob_sha1": "5c6a7cdc1325f969bbdc5d6e9e493f06c0925722",
        "size": 957
    },
    "loweredDigits6Dots.uti": {
        "git_blob_sha1": "d30d92a3e5d9321f10632207418414f9e127798f",
        "size": 926
    },
    "ru-math.uti": {
        "git_blob_sha1": "ce216261f6ea8ec50238628142c3a86ece4c97a5",
        "size": 15084
    },
    "IPA-unicode-range.uti": {
        "git_blob_sha1": "e4e26bfa163981765afef206af4a92fb11e5bd18",
        "size": 13188
    },
    "unicode.dis": {
        "git_blob_sha1": "405ef89deb12fff35dc91e4aeba79ec9fa092614",
        "size": 5974
    }
}
_NATIVE = "import ctypes\nimport hashlib\nimport json\nimport os\nfrom pathlib import Path\nimport stat\nimport sys\n\nMAX_TEXT = 100000\nMAX_LINES = 4096\nMAX_OUTPUT = 2 * 1024 * 1024\nTABLES = {\"en-ueb-g1\": \"en-ueb-g1.ctb\", \"uk\": \"uk.utb\"}\nTABLE_PINS = {\n    \"en-ueb-g1.ctb\": {\n        \"git_blob_sha1\": \"c95a102781b56e7f93084aeb1a8e99d8f6d5c75a\",\n        \"size\": 5368\n    },\n    \"uk.utb\": {\n        \"git_blob_sha1\": \"2cc6762e76ff28b1dd71567918a21e7c91688bf4\",\n        \"size\": 3509\n    },\n    \"en-us-brf.dis\": {\n        \"git_blob_sha1\": \"f349d50c8152585e86d7ad48fdc6bfe16356e228\",\n        \"size\": 2438\n    },\n    \"en-ueb-chardefs.uti\": {\n        \"git_blob_sha1\": \"b0e5c49d03c64574c29998ae8189832601844af4\",\n        \"size\": 27834\n    },\n    \"en-ueb-math.ctb\": {\n        \"git_blob_sha1\": \"320607c52875b8bf95159be4b2ebbca3bfbb0e14\",\n        \"size\": 2045\n    },\n    \"braille-patterns.cti\": {\n        \"git_blob_sha1\": \"1cb7df65602c1d1b91d29c147106d724f642d96a\",\n        \"size\": 26431\n    },\n    \"ru-litbrl.ctb\": {\n        \"git_blob_sha1\": \"29544dd21279b9c24b6054e866df44355325e90a\",\n        \"size\": 33096\n    },\n    \"text_nabcc.dis\": {\n        \"git_blob_sha1\": \"dde2890c0d3d01afb82da1d69816d78fc73849e1\",\n        \"size\": 21650\n    },\n    \"spaces.uti\": {\n        \"git_blob_sha1\": \"e72b674452c7937347e6941db6d612c594801a36\",\n        \"size\": 2108\n    },\n    \"latinLetterDef6Dots.uti\": {\n        \"git_blob_sha1\": \"9ffefc80ab168a5380ed476b1a23fc5daccba584\",\n        \"size\": 1993\n    },\n    \"latinUppercaseComp6.uti\": {\n        \"git_blob_sha1\": \"2736a9fed2fb999fdc69f50e49da232f1723acb3\",\n        \"size\": 1868\n    },\n    \"ru-unicode.dis\": {\n        \"git_blob_sha1\": \"f702feccbce1196d40f8ceaae742de7ab4952765\",\n        \"size\": 3025\n    },\n    \"litdigits6Dots.uti\": {\n        \"git_blob_sha1\": \"5c6a7cdc1325f969bbdc5d6e9e493f06c0925722\",\n        \"size\": 957\n    },\n    \"loweredDigits6Dots.uti\": {\n        \"git_blob_sha1\": \"d30d92a3e5d9321f10632207418414f9e127798f\",\n        \"size\": 926\n    },\n    \"ru-math.uti\": {\n        \"git_blob_sha1\": \"ce216261f6ea8ec50238628142c3a86ece4c97a5\",\n        \"size\": 15084\n    },\n    \"IPA-unicode-range.uti\": {\n        \"git_blob_sha1\": \"e4e26bfa163981765afef206af4a92fb11e5bd18\",\n        \"size\": 13188\n    },\n    \"unicode.dis\": {\n        \"git_blob_sha1\": \"405ef89deb12fff35dc91e4aeba79ec9fa092614\",\n        \"size\": 5974\n    }\n}\n\ndef unique(pairs):\n    out = {}\n    for key, value in pairs:\n        if key in out:\n            raise ValueError()\n        out[key] = value\n    return out\n\ndef checked_file(path, maximum):\n    info = path.lstat()\n    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1\n            or getattr(info, \"st_file_attributes\", 0) & 0x400\n            or not 0 < info.st_size <= maximum):\n        raise ValueError()\n    with path.open(\"rb\") as stream:\n        opened = os.fstat(stream.fileno())\n        if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):\n            raise ValueError()\n        raw = stream.read(maximum + 1)\n        final = os.fstat(stream.fileno())\n    if (len(raw) != info.st_size or len(raw) > maximum\n            or (final.st_size, final.st_mtime_ns, final.st_ctime_ns)\n               != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)):\n        raise ValueError()\n    return raw\n\ndef main():\n    if os.name != \"nt\" or ctypes.sizeof(ctypes.c_void_p) != 8 or len(sys.argv) != 2:\n        raise ValueError()\n    raw = checked_file(Path(\"request.json\"), 800000)\n    if hashlib.sha256(raw).hexdigest() != sys.argv[1]:\n        raise ValueError()\n    request = json.loads(raw, object_pairs_hook=unique)\n    if (type(request) is not dict or set(request) != {\"schema\", \"table\", \"text\", \"dll_sha256\"}\n            or request[\"schema\"] != \"carr-liblouis-request/1\"\n            or request[\"table\"] not in TABLES or type(request[\"text\"]) is not str):\n        raise ValueError()\n    value = request[\"text\"]\n    if (not 0 < len(value) <= MAX_TEXT or not value.strip()\n            or any((ord(c) < 32 and c != \"\\n\") or 127 <= ord(c) < 160 for c in value)):\n        raise ValueError()\n    lines = value.split(\"\\n\")\n    if len(lines) > MAX_LINES:\n        raise ValueError()\n    dll = Path(\"bin/liblouis.dll\").absolute()\n    if hashlib.sha256(checked_file(dll, 16 * 1024 * 1024)).hexdigest() != request[\"dll_sha256\"]:\n        raise ValueError()\n    for name, pin in TABLE_PINS.items():\n        data = checked_file(Path(\"tables\") / name, 65536)\n        blob = b\"blob \" + str(len(data)).encode(\"ascii\") + b\"\\0\" + data\n        if len(data) != pin[\"size\"] or hashlib.sha1(blob).hexdigest() != pin[\"git_blob_sha1\"]:\n            raise ValueError()\n    os.environ[\"LOUIS_TABLEPATH\"] = \"tables\"\n    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32:\n    # never search PATH/current directory for unselected dependent libraries.\n    library = ctypes.WinDLL(str(dll), winmode=0x900)\n    kernel32 = ctypes.WinDLL(\"kernel32\", use_last_error=True)\n    kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]\n    kernel32.GetModuleFileNameW.restype = ctypes.c_uint32\n    module_name = ctypes.create_unicode_buffer(32768)\n    length = kernel32.GetModuleFileNameW(library._handle, module_name, len(module_name))\n    if not 0 < length < len(module_name) or Path(module_name.value).resolve(strict=True) != dll.resolve(strict=True):\n        raise ValueError()\n    library.lou_version.argtypes = []\n    library.lou_version.restype = ctypes.c_char_p\n    library.lou_charSize.argtypes = []\n    library.lou_charSize.restype = ctypes.c_int\n    if library.lou_version() != b\"3.39.0\" or library.lou_charSize() != 4:\n        raise ValueError()\n    callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)\n    quiet = callback_type(lambda level, message: None)\n    library.lou_registerLogCallback.argtypes = [callback_type]\n    library.lou_registerLogCallback.restype = None\n    library.lou_registerLogCallback(quiet)\n    library.lou_setLogLevel.argtypes = [ctypes.c_int]\n    library.lou_setLogLevel.restype = None\n    library.lou_setLogLevel(60000)\n    wide = ctypes.c_uint32\n    ptr = ctypes.POINTER(wide)\n    integer = ctypes.c_int\n    library.lou_translateString.argtypes = [\n        ctypes.c_char_p, ptr, ctypes.POINTER(integer), ptr,\n        ctypes.POINTER(integer), ctypes.c_void_p, ctypes.c_void_p, integer]\n    library.lou_translateString.restype = integer\n    library.lou_dotsToChar.argtypes = [ctypes.c_char_p, ptr, ptr, integer, integer]\n    library.lou_dotsToChar.restype = integer\n    library.lou_free.argtypes = []\n    library.lou_free.restype = None\n    table = TABLES[request[\"table\"]].encode(\"ascii\")\n\n    def translate(line, mode):\n        incoming = (wide * len(line))(*(ord(c) for c in line))\n        capacity = min(MAX_OUTPUT, max(256, len(line) * 24 + 32))\n        outgoing = (wide * capacity)()\n        consumed, produced = integer(len(line)), integer(capacity)\n        ok = library.lou_translateString(table, incoming, ctypes.byref(consumed),\n            outgoing, ctypes.byref(produced), None, None, mode)\n        # Success may mean only a prefix fitted. Such output is never exported.\n        if ok != 1 or consumed.value != len(line) or not 0 <= produced.value <= capacity:\n            raise ValueError()\n        return tuple(outgoing[:produced.value])\n\n    pieces, total = [], 0\n    try:\n        for index, line in enumerate(lines):\n            if line:\n                # Upstream dotsIO|ucBrl emits Unicode cells. noUndefined removes\n                # unknown input, so any difference proves unsupported characters.\n                dots = translate(line, 4 | 64)\n                if dots != translate(line, 4 | 64 | 128):\n                    raise ValueError()\n                if not dots or any(not 0x2800 <= c <= 0x283f for c in dots):\n                    raise ValueError()\n                incoming = (wide * len(dots))(*dots)\n                outgoing = (wide * len(dots))()\n                if library.lou_dotsToChar(b\"en-us-brf.dis\", incoming, outgoing, len(dots), 0) != 1:\n                    raise ValueError()\n                if any(not 32 <= c <= 95 for c in outgoing):\n                    raise ValueError()\n                # Convert cell values, never the ctypes array memory representation.\n                part = bytes(int(c) for c in outgoing)\n            else:\n                part = b\"\"\n            total += len(part) + (2 if index else 0)\n            if total > MAX_OUTPUT:\n                raise ValueError()\n            pieces.append(part)\n        output = b\"\\r\\n\".join(pieces)\n        if not output:\n            raise ValueError()\n        with open(\"output.brf\", \"xb\") as stream:\n            stream.write(output)\n            stream.flush()\n            os.fsync(stream.fileno())\n        print(json.dumps({\"status\": \"PASS\", \"version\": \"3.39.0\", \"widechar_bytes\": 4,\n            \"table\": request[\"table\"], \"size\": len(output),\n            \"sha256\": hashlib.sha256(output).hexdigest()}, sort_keys=True))\n    finally:\n        library.lou_free()\n\ntry:\n    main()\nexcept BaseException:\n    # Native input, local paths and raw library diagnostics stay private.\n    print('{\"status\":\"FAIL\",\"reason\":\"native-translation-refused\"}')\n    sys.exit(2)\n"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated Liblouis protocol field.")
        result[key] = value
    return result


def selected_text(value):
    """Preserve logical line boundaries; refuse unsupported layout controls."""
    if type(value) is not str or not 0 < len(value) <= MAX_TEXT_CHARACTERS or not value.strip():
        raise ValueError("Choose between 1 and 100,000 nonempty source characters for plain BRF.")
    value.encode("utf-8", "strict")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if (len(value.split("\n")) > MAX_LINES
            or any((ord(c) < 32 and c != "\n") or 127 <= ord(c) < 160 for c in value)):
        raise ValueError("Plain BRF accepts at most 4,096 lines and no tab, page or other control characters.")
    return value


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_regular(path, maximum, *, cancelled=lambda: False):
    """Read stable bounded bytes without following a replaced file."""
    check_cancelled(cancelled)
    path = local_file(path)
    before = path.lstat()
    if not 0 < before.st_size <= maximum:
        raise ValueError("The selected Liblouis input or output exceeds its byte budget.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if linked(opened) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _stamp(opened) != _stamp(before):
            raise ValueError("The selected Liblouis file changed before reading.")
        pieces, size = [], 0
        while True:
            check_cancelled(cancelled)
            piece = stream.read(min(65536, maximum + 1 - size))
            if not piece:
                break
            pieces.append(piece)
            size += len(piece)
            if size > maximum:
                raise ValueError("The selected Liblouis file grew beyond its byte budget.")
        after = os.fstat(stream.fileno())
    check_cancelled(cancelled)
    local_file(path)
    if size != before.st_size or _stamp(after) != _stamp(before) or _stamp(path.lstat()) != _stamp(before):
        raise ValueError("The selected Liblouis file changed during reading.")
    return b"".join(pieces)


def _archive(path, *, cancelled):
    raw = read_regular(path, ARCHIVE_SIZE, cancelled=cancelled)
    if len(raw) != ARCHIVE_SIZE or sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Select the exact reviewed official Liblouis 3.39.0 Windows x64 archive.")
    return raw


def _members(raw):
    """Return only pinned source tables and the library, never archive tools."""
    wanted = {"bin/liblouis.dll"}
    wanted.update("share/liblouis/tables/" + name for name in _TABLE_PINS)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 10000:
            raise ValueError("The selected Liblouis archive has too many members.")
        names, selected = set(), {}
        for item in infos:
            name = item.filename
            key = name.casefold()
            if key in names:
                raise ValueError("The selected Liblouis archive contains duplicate names.")
            names.add(key)
            if name not in wanted:
                continue
            mode = item.external_attr >> 16
            if (item.is_dir() or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or item.flag_bits & 1 or item.file_size <= 0):
                raise ValueError("A selected Liblouis archive member is not a regular unencrypted file.")
            limit = 16 * 1024 * 1024 if name == "bin/liblouis.dll" else 65536
            if item.file_size > limit:
                raise ValueError("A selected Liblouis archive member exceeds its byte budget.")
            with archive.open(item) as incoming:
                data = incoming.read(limit + 1)
            if len(data) != item.file_size:
                raise ValueError("A selected Liblouis archive member is truncated or oversized.")
            if name != "bin/liblouis.dll":
                pin = _TABLE_PINS[name.rsplit("/", 1)[1]]
                identity = sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
                if len(data) != pin["size"] or identity != pin["git_blob_sha1"]:
                    raise ValueError("The Liblouis table differs from its reviewed release-source closure.")
            selected[name] = data
        if set(selected) != wanted:
            raise ValueError("The official Liblouis archive is missing the reviewed library/table layout.")
    return selected


def inspect_archive(path, *, cancelled=lambda: False):
    """Verify the exact official archive and source closure without loading it."""
    selected = _members(_archive(path, cancelled=cancelled))
    check_cancelled(cancelled)
    return {
        "schema": "carr-liblouis-inspection/1", "version": VERSION,
        "source_commit": SOURCE_COMMIT, "archive_sha256": ARCHIVE_SHA256,
        "library_sha256": sha256(selected["bin/liblouis.dll"]).hexdigest(),
        "tables": {name: dict(pin) for name, pin in _TABLE_PINS.items()},
        "enabled_tables": sorted(TABLES),
        "native_execution": "NOT_RUN", "tactile_quality": "NOT_RUN",
        "layout": "plain six-dot Braille ASCII; no pagination or embosser layout",
    }


def render_selected(value, table, archive_path, stage, *, cancelled=lambda: False):
    """Use the existing process supervisor inside the caller's resource lease.

    The caller owns the fresh private stage and keeps it until that supervisor
    and any pending CleanupOwner are quiescent. The returned bytes are immutable.
    """
    value = selected_text(value)
    if table not in TABLES:
        raise ValueError("Choose the reviewed English UEB grade-1 or Ukrainian table.")
    if os.name != "nt" or sys.maxsize <= 2 ** 32:
        raise ValueError("This explicit Liblouis runtime supports Windows x64 only.")
    stage = Path(stage)
    check_cancelled(cancelled)
    local_file(stage / "request.json", must_exist=False)
    selected = _members(_archive(archive_path, cancelled=cancelled))
    if any(stage.iterdir()):
        raise ValueError("Liblouis requires an empty privately owned stage.")
    (stage / "bin").mkdir()
    (stage / "tables").mkdir()
    for name, data in selected.items():
        check_cancelled(cancelled)
        target = stage / ("bin/liblouis.dll" if name == "bin/liblouis.dll" else "tables/" + name.rsplit("/", 1)[1])
        with target.open("xb") as stream:
            stream.write(data)
    request = json.dumps({"schema": "carr-liblouis-request/1", "text": value, "table": table,
        "dll_sha256": sha256(selected["bin/liblouis.dll"]).hexdigest()},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with (stage / "request.json").open("xb") as stream:
        stream.write(request)
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LOUIS_TABLEPATH"] = "tables"
    result = run_process([str(Path(sys.executable).absolute()), "-I", "-B", "-S", "-c", _NATIVE,
        sha256(request).hexdigest()], cwd=stage, env=environment, cancelled=cancelled,
        budget=AudioBudget(timeout=60, max_output_bytes=MAX_STAGE_BYTES, max_log_bytes=8192,
            reserve_disk_bytes=16 * 1024 * 1024, max_inputs=1, threads=1,
            max_alloc_bytes=256 * 1024 * 1024, max_staging_files=32),
        watch=(stage,), owned_windows_job=os.name == "nt", allow_failure=True)
    check_cancelled(cancelled)
    if result.returncode != 0:
        raise ValueError("Liblouis refused this source, unsupported cells or the bounded native translation.")
    try:
        report = json.loads(result.stdout, object_pairs_hook=_unique)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("The bounded Liblouis runtime report is invalid.") from None
    if (type(report) is not dict or set(report) != {"status", "version", "widechar_bytes", "table", "size", "sha256"}
            or report["status"] != "PASS" or report["version"] != VERSION
            or type(report["widechar_bytes"]) is not int or report["widechar_bytes"] != 4
            or report["table"] != table):
        raise ValueError("The selected Liblouis C-API identity or result is invalid.")
    output = read_regular(stage / "output.brf", MAX_BRF_BYTES, cancelled=cancelled)
    if (type(report["size"]) is not int or len(output) != report["size"]
            or sha256(output).hexdigest() != report["sha256"]
            or any(c not in (10, 13) and not 32 <= c <= 95 for c in output)
            or b"\r" in output.replace(b"\r\n", b"") or b"\n" in output.replace(b"\r\n", b"")):
        raise ValueError("The native result is not exact bounded six-dot Braille ASCII.")
    return output, {**report, "archive_sha256": ARCHIVE_SHA256, "source_commit": SOURCE_COMMIT,
        "library_sha256": sha256(selected["bin/liblouis.dll"]).hexdigest(),
        "tactile_quality": "NOT_RUN", "layout": "unpaginated plain six-dot BRF"}
