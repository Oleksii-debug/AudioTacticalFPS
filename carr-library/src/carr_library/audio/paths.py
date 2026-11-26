"""Portable filename rules for audio projects moved between local machines."""
from __future__ import annotations


_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_DEVICES.update(f"{prefix}{suffix}" for prefix in ("COM", "LPT")
                        for suffix in (*"123456789", "\u00b9", "\u00b2", "\u00b3"))


def filename_component(name: str) -> str:
    """Reject device names, path syntax and aliases before filesystem access.

    Apply Windows rules on every platform so a valid portable project does
    not become a device, stream or differently named file when opened there.
    """
    if (not isinstance(name, str) or not name or name in (".", "..")
            or name.rstrip(" .") != name or any(ord(c) < 32 for c in name)
            or any(c in name for c in '<>:"/\\|?*')
            or name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES):
        raise ValueError("Use a local filename without Windows devices, alternate streams or ambiguous characters.")
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Audio paths must contain valid Unicode characters.") from exc
    return name
