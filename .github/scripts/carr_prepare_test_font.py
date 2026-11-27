"""Prepare the exact public DejaVu font used by existing synthetic OCR fixtures.

This is a CI prerequisite, never an application runtime or user asset.
The release digest is published at https://dejavu-fonts.github.io/Download.html.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import urllib.request
import zipfile

URL = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-sans-ttf-2.37.zip"
ARCHIVE_SHA256 = "5c6e497a2f36552cb5ffb112c413a6af39c0f3c47653662b90b4fa6499822fd7"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with urllib.request.urlopen(URL, timeout=45) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Public OCR fixture font archive failed its exact integrity check.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 64 or sum(entry.file_size for entry in entries) > 4 * 1024 * 1024:
            raise ValueError("Public OCR font archive exceeds its bound.")
        selected = [entry for entry in entries
                    if not entry.is_dir() and PurePosixPath(entry.filename).name == "DejaVuSans.ttf"]
        if len(selected) != 1 or not 0 < selected[0].file_size <= 1024 * 1024:
            raise ValueError("Public OCR font archive has an unexpected font inventory.")
        data = archive.read(selected[0])
    destination = Path.cwd() / "DejaVuSans.ttf"
    with destination.open("xb") as stream:
        stream.write(data)
    from PIL import ImageFont
    font = ImageFont.truetype("DejaVuSans.ttf", 72)
    if Path(font.path).resolve() != destination.resolve():
        raise ValueError("Existing OCR fixtures did not resolve the exact prepared font.")
    evidence = dict(status="PASS", scope="public synthetic OCR test prerequisite only",
                    source=URL, archive_sha256=ARCHIVE_SHA256,
                    font_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
                    selected_member=selected[0].filename)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
