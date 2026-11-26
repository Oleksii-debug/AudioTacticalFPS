"""Optional PR32 OSD integration; baseline evidence remains authoritative as a candidate."""
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
import importlib.metadata as metadata
import re
import shutil
import subprocess
import tempfile

from .orientation import candidate_image, detect_orientation, parse_osd
from .pinned_tessdata import POLICY, stage_pinned_models


def _model_observations(exe, languages, remaining, run):
    """Record observed local traineddata hashes, not a model attestation."""
    observed = {}
    try:
        report = run([exe, '--list-langs']).stdout.decode('utf-8', 'replace')
        match = re.search(r'List of available languages in "([^"\r\n]+)"', report)
        if not match:
            return observed
        for lang in sorted(set(languages.split('+')) | {'osd'}):
            remaining()
            path = Path(match[1]) / (lang + '.traineddata')
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                continue
            digest = sha256()
            with path.open('rb') as stream:
                total = 0
                while chunk := stream.read(1024 * 1024):
                    remaining()
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ValueError('traineddata observation limit')
                    digest.update(chunk)
            observed[lang] = digest.hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return observed


def orientation_candidate(image, config, exe, remaining, run, recognize):
    """Never overwrite baseline OCR/boxes; use only remaining subprocess budget."""
    result = dict(status='not_run', algorithm='pr32-osd-v1-dpi', dpi=config.dpi,
                  min_confidence=15.0, source_size=image.size,
                  source_rgb_sha256=sha256(image.tobytes()).hexdigest(),
                  coordinate_frame='orientation_candidate_pixels_top_left',
                  traineddata_sha256_observed={})
    pinned = config.model_policy == POLICY
    try:
        if pinned:
            result['algorithm'] = 'pinned-native-osd-v1'
            if not 0 < image.width * image.height <= config.max_pixels:
                result['status'] = 'render_limit'
                return result
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    report = run([exe, '--list-langs']).stdout.decode('utf-8')
                    args, model = stage_pinned_models(report, 'osd', Path(tmp) / 'verified osd', remaining, osd=True)
                except (OSError, ValueError, subprocess.SubprocessError):
                    result['status'] = 'pinned_osd_unavailable_or_invalid'
                    return result
                result['osd_model'] = model
                inp = Path(tmp) / 'orientation.png'
                image.save(inp, dpi=(config.dpi, config.dpi))
                raw_osd = run([exe, str(inp), 'stdout', '-l', 'osd', '--psm', '0',
                               '--dpi', str(config.dpi), *args]).stdout.decode('utf-8')
                evidence = replace(parse_osd(raw_osd), engine='Tesseract OSD via native CLI')
        else:
            import pytesseract
            result['wrapper_version'] = metadata.version('pytesseract')
            wrapper_exe = shutil.which(pytesseract.pytesseract.tesseract_cmd)
            if not wrapper_exe or Path(wrapper_exe).resolve() != Path(exe).resolve():
                result['status'] = 'engine_mismatch_or_missing'
                return result
            evidence = detect_orientation(image, timeout=min(15, remaining()),
                                          max_pixels=config.max_pixels, dpi=config.dpi)
        result.update(asdict(evidence))
        if evidence.status != 'candidate':
            return result
        if not evidence.clockwise:
            result['status'] = 'no_rotation_candidate'
            return result
        rotated = candidate_image(image, evidence)
        result.update(transformed_size=rotated.size,
                      transformed_rgb_sha256=sha256(rotated.tobytes()).hexdigest())
        raw, tsv = recognize(rotated, 'oriented')
        result.update(status='ocr_candidate', raw_text=raw, raw_tsv=tsv)
        # Optional observational provenance must not discard successful OCR.
        if not pinned:
            result['traineddata_sha256_observed'] = _model_observations(exe, config.languages, remaining, run)
    except ImportError:
        result['status'] = 'wrapper_missing'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, UnicodeError):
        result['status'] = 'orientation_failed_or_timed_out'
    return result
