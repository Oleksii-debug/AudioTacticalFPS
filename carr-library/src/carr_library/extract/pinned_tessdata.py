"""Stage exact Apache-2.0 tessdata_fast models for local Tesseract OCR."""
from hashlib import sha256
from pathlib import Path
import re

REVISION = '87416418657359cb625c412a48b6e1d6d41c29bd'
POLICY = 'tessdata-fast-' + REVISION
# Local binary bytes matched Git blob IDs in the upstream tree at REVISION.
# SHA256 pins are over those same local bytes, not an upstream signature.
MODELS = {
    'eng': (4113088, '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'),
    'ukr': (3825102, 'd59e53e2bded32f4445f124b4b00240fcac7e8044c003ab822ccb94f0b3db59b'),
    'slk': (4427661, 'fbcc400a9c74c6a13d922fcb1211b655d1b165387b675ed75cd2dbd756b974a5'),
}
OSD_MODEL = (10562727, '9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff')


def stage_pinned_models(report, languages, destination, remaining, *, osd=False):
    """Copy verified model bytes into a fresh private per-call directory.

    No network or model fallback. The caller owns destination cleanup and the
    shared deadline; only the returned argv may be used for pinned inference.
    OSD uses a separate legacy model and engine mode, never LSTM language mode.
    """
    selected = languages.split('+')
    table = {'osd': OSD_MODEL} if osd else MODELS
    if not selected or len(set(selected)) != len(selected) or any(lang not in table for lang in selected):
        raise ValueError('unsupported pinned OCR language')
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.MULTILINE)
    if not match:
        raise ValueError('cannot identify local Tesseract model directory')
    source = Path(match[1])
    destination = Path(destination)
    destination.mkdir(mode=0o700, exist_ok=False)
    for lang in selected:
        remaining()
        size, expected = table[lang]
        name = lang + '.traineddata'
        origin = source / name
        if not origin.is_file() or origin.stat().st_size != size:
            raise ValueError('pinned OCR model missing or size mismatch')
        digest = sha256()
        count = 0
        with origin.open('rb') as inp, (destination / name).open('xb') as out:
            while chunk := inp.read(1024 * 1024):
                remaining()
                count += len(chunk)
                if count > size:
                    raise ValueError('pinned OCR model size mismatch')
                digest.update(chunk)
                out.write(chunk)
        if count != size or digest.hexdigest() != expected:
            raise ValueError('pinned OCR model digest mismatch')
    remaining()
    return ['--tessdata-dir', str(destination), '--oem', '0' if osd else '1'], {
        'policy': POLICY, 'upstream_revision': REVISION,
        'model_sha256': {lang: table[lang][1] for lang in selected},
        'engine_mode': 'legacy_osd' if osd else 'LSTM', 'verification': 'staged_bytes_sha256',
    }
