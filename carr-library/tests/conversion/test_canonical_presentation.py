"""Replay the exact reviewed producer-generated presentation in the current worker."""
from hashlib import sha1, sha256
import json
from pathlib import Path

from carr_library.conversion import ConversionService


def test_complete_canonical_presentation_uses_relationship_order_in_actual_worker(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'ordered_slides.pptx'
    raw = fixture.read_bytes()
    assert len(raw) == 29150
    assert sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest() == (
        '58b0d7cce78a50883ed4bcf1e73cbbcc40675d44')
    source = tmp_path / 'Київ-ordered-slides.pptx'
    source.write_bytes(raw)
    output = tmp_path / 'reading'
    output.mkdir()
    result = ConversionService(output, input_roots=(source,)).extract(source, 'slides.txt')
    text = result.path.read_text(encoding='utf-8')
    assert text.index('Earlier') < text.index('Later')
    assert len(result.report['segments']) == 2
    assert result.report == json.loads(result.report_path.read_text(encoding='utf-8'))
    assert result.report['source_sha256'] == sha256(raw).hexdigest()
    assert result.report['source_size'] == len(raw)
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert result.report['printed_pages_reconstructed'] is False
    for segment in result.report['segments']:
        assert segment['printed_label'] is None and segment['printed_label_verified'] is False
        start, end = segment['output_characters']
        assert sha256(text[start:end].encode('utf-8')).hexdigest() == segment['text_sha256']
    assert source.read_bytes() == raw and fixture.read_bytes() == raw
    assert sorted(path.name for path in output.iterdir()) == ['slides.txt', 'slides.txt.provenance.json']
