"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
import math
import struct
from .studio_models import PlanError


def _verify_report(path, report, digest):
    """A byte checksum must also agree with the actual rendered PCM timeline."""
    if type(report) is not dict or report.get('sha256') != digest:
        raise PlanError('Chunk report checksum does not match its verified bytes.')
    if 'size_bytes' in report:
        if type(report['size_bytes']) is not int or report['size_bytes'] != path.stat().st_size:
            raise PlanError('Chunk report size does not match its verified bytes.')
    media_fields = {'frames', 'sample_rate', 'channels', 'duration_seconds'}
    if media_fields.intersection(report):
        if not media_fields <= report.keys():
            raise PlanError('Rendered PCM metadata is incomplete.')
        from .pcm import _pcm_header
        try:
            rate, channels, frames, _, _ = _pcm_header(path)
        except (ValueError, OSError, struct.error) as exc:
            raise PlanError('Rendered PCM metadata could not be verified.') from exc
        actual = {'frames': frames, 'sample_rate': rate, 'channels': channels}
        if any(type(report[key]) is not int or report[key] != value for key, value in actual.items()):
            raise PlanError('Rendered PCM metadata differs from the actual recording.')
        duration = report['duration_seconds']
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not math.isclose(duration, frames / rate, rel_tol=1e-12, abs_tol=1e-9)):
            raise PlanError('Rendered PCM duration differs from the actual recording.')
    elif 'size_bytes' not in report:
        raise PlanError('A prepared input snapshot requires its verified byte size.')
    return report
