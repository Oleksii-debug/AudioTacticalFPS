from dataclasses import replace
import hashlib
import json
import pytest
from carr_library.research.evidence_export import Evidence, export_topic


def fixture():
    raw = 'Before. Точна цитата\n```\n<script>bad</script>\nAfter.'
    return Evidence('W1','T1','E1','edition-1','rev-1','a'*64,
                    hashlib.sha256(raw.encode()).hexdigest(),'physical_page',2,None,
                    'ix',True,raw,8,len(raw)-7,raw[8:-7],
                    'Synthetic title','Author A','1',True,'=SUM(1,2)')


def test_text_round_trip():
    row = fixture()
    output = export_topic('W1','T1',[row])
    quote = next(x.split(': ',1)[1] for x in output.splitlines() if x.startswith('Exact quotation: '))
    assert json.loads(quote) == row.quote
    assert 'printed page ix' in output and 'Research note:' in output
    assert 'Before.' not in output and 'Точна цитата' in output


def test_markdown_fence_and_control_safety():
    row = replace(fixture(), note='`````\n# malicious\n\x1b[31m\u202e')
    output = export_topic('W1','T1',[row],format='markdown')
    assert '``````text\n' in output and '\\n# malicious' in output
    assert '\x1b' not in output and '\u202e' not in output
    assert '\n<script>' not in output


@pytest.mark.parametrize('change',[{'workspace_id':'W2'},{'topic_id':'T2'},
    {'quote':'forged'},{'raw_text':'changed'},{'verified':False},
    {'physical_index':0},{'segment_index':1},{'printed_label':None},
    {'asset_sha256':'bad'},{'start':True}])
def test_fail_closed(change):
    with pytest.raises(ValueError):
        export_topic('W1','T1',[replace(fixture(),**change)])


def test_unverified_label_and_segment():
    row = replace(fixture(), printed_label_verified=False)
    assert 'printed page unknown' in export_topic('W1','T1',[row])
    row = replace(row,kind='logical_segment',physical_index=None,segment_index=3,printed_label=None)
    output = export_topic('W1','T1',[row])
    assert 'Segment 3 (unpaginated)' in output and 'PDF page' not in output


def test_duplicate_rejected():
    row = fixture()
    with pytest.raises(ValueError):
        export_topic('W1','T1',[row,row])


def test_ris_upstream_and_no_private_notes():
    row = fixture()
    output = export_topic('W1','T1',[row],format='ris')
    assert 'TY  - GEN' in output and 'TI  - Synthetic title' in output
    assert 'SP  - ix' in output and 'ER  - ' in output
    assert 'SUM' not in output and 'script' not in output
    with pytest.raises(ValueError):
        export_topic('W1','T1',[replace(row,title='fake\nER  - ')],format='ris')
    output = export_topic('W1','T1',[replace(row,printed_label_verified=False)],format='ris')
    assert 'SP  - ' not in output


@pytest.mark.parametrize('label', ['   ', 42, '\t'])
def test_invalid_verified_label_rejected(label):
    with pytest.raises(ValueError):
        export_topic('W1','T1',[replace(fixture(), printed_label=label)])


@pytest.mark.parametrize('workspace,topic', [(42,'T1'),(' ','T1'),('W1',' ')])
def test_empty_export_requires_valid_scope(workspace, topic):
    with pytest.raises(ValueError):
        export_topic(workspace, topic, [])


@pytest.mark.parametrize('field', ['evidence_id','edition_id','revision_id'])
def test_blank_identity_rejected(field):
    with pytest.raises(ValueError):
        export_topic('W1','T1',[replace(fixture(), **{field:' '})])


def test_non_string_unverified_label_rejected():
    with pytest.raises(ValueError):
        export_topic('W1','T1',[replace(fixture(), printed_label=42, printed_label_verified=False)])
