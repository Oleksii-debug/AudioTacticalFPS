"""Inspect the built PE without executing it or claiming code-signing acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


MAX_PE_BYTES = 2 * 1024 * 1024


def inspect_pe(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_PE_BYTES + 1)
    return inspect_pe_bytes(data, Path(path).name)


def inspect_pe_bytes(data, filename="ResearchLibrary.exe"):
    """Inspect the exact bounded snapshot that a packager will seal."""
    if not isinstance(data, bytes) or not 64 <= len(data) <= MAX_PE_BYTES or data[:2] != b"MZ":
        raise ValueError("Expected a small native Windows PE launcher")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Invalid PE signature")
    machine, sections, timestamp, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, pe + 4)
    if not 1 <= sections <= 96 or optional_size < 240:
        raise ValueError("Invalid PE section or optional-header bounds")
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    subsystem, dll_flags = struct.unpack_from("<HH", data, optional + 68)
    if machine != 0x8664 or magic != 0x20B or subsystem != 2:
        raise ValueError("Expected x64 PE32+ Windows GUI subsystem")
    if dll_flags & 0x160 != 0x160:
        raise ValueError("ASLR, high-entropy ASLR and DEP must be enabled")
    certificate_offset, certificate_size = struct.unpack_from("<II", data, optional + 112 + 4 * 8)
    if certificate_offset or certificate_size:
        raise ValueError("This unsigned development-build probe does not validate signing")
    rows = []
    for i in range(sections):
        offset = optional + optional_size + i * 40
        virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", data, offset + 8)
        rows.append((address, max(virtual_size, raw_size), raw_offset, raw_size))

    def physical(rva):
        for address, size, raw_offset, raw_size in rows:
            if address <= rva < address + size and rva - address < raw_size:
                return raw_offset + rva - address
        raise ValueError("PE data directory is outside its sections")

    debug_rva, debug_size = struct.unpack_from("<II", data, optional + 112 + 6 * 8)
    reproducible_marker = False
    if debug_rva:
        if debug_size > 28 * 64 or debug_size % 28:
            raise ValueError("Invalid debug directory size")
        debug = physical(debug_rva)
        reproducible_marker = any(struct.unpack_from("<I", data, debug + index + 12)[0] == 16
                                  for index in range(0, debug_size, 28))
    if timestamp != 0 and not reproducible_marker:
        raise ValueError("Expected a zero timestamp or IMAGE_DEBUG_TYPE_REPRO deterministic build marker")
    imports = []
    import_rva, _ = struct.unpack_from("<II", data, optional + 112 + 8)
    if import_rva:
        entry = physical(import_rva)
        for _ in range(64):
            record = struct.unpack_from("<IIIII", data, entry)
            if not any(record):
                break
            name = physical(record[3])
            imports.append(data[name:data.index(b"\0", name)].decode("ascii"))
            entry += 20
        else:
            raise ValueError("Too many PE imports")
    if not imports or not set(name.lower() for name in imports) <= {"kernel32.dll", "user32.dll", "shell32.dll"}:
        raise ValueError("The launcher has an unexpected external runtime dependency")
    if b'level="asInvoker"' not in data or b'uiAccess="false"' not in data or b"longPathAware" not in data:
        raise ValueError("The expected embedded least-privilege manifest is absent")
    resource_rva, resource_size = struct.unpack_from("<II", data, optional + 112 + 2 * 8)
    if not resource_rva or not resource_size:
        raise ValueError("The manifest must be embedded in the PE resources")
    return {"schema": 1, "file": filename, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "machine": "x86_64", "subsystem": "windows-gui",
            "aslr": True, "high_entropy_aslr": True, "dep": True, "timestamp": timestamp,
            "imports": imports, "reproducible_build_marker": reproducible_marker,
            "embedded_as_invoker": True, "certificate_table_present": False,
            "authenticode_signed": False, "windows_execution_verified": False, "nvda_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    print(json.dumps(inspect_pe(parser.parse_args().executable), indent=2))
