from dataclasses import replace
from hashlib import sha256
import json

import pytest

from carr_library.conversion.planning import (
    MAX_PARTS, SplitOptions, SplitPlan, TextBoundary, filename_key, plan_split,
    portable_name, render_name, source_boundaries, studio_project, verify_plan,
)
from carr_library.speech.reading import ReadingDocument


SOURCE = '😀 First sentence.\r\nSecond paragraph!\r\n\r\nCHAPTER Ukrainian: Україна.\nFinal　text。  '


@pytest.mark.parametrize('newline', ['\r\n', '\n', '\r'])
def test_blank_lines_never_misclassifies_one_crlf_as_two_breaks(newline):
    text = f'First{newline}same paragraph{newline}{newline}Second{newline}same paragraph'
    plan = plan_split(ReadingDocument(text), SplitOptions('blank-lines'))
    assert len(plan.parts) == 2
    assert text[plan.parts[1].start:] == f'Second{newline}same paragraph'


def test_whitespace_only_parts_are_preserved_for_text_but_not_given_invented_audio_timing():
    document = ReadingDocument('\n\nActual words.')
    plan = plan_split(document, SplitOptions('blank-lines'))
    assert document.text[plan.parts[0].start:plan.parts[0].end] == '\n\n'
    assert verify_plan(document, plan) == plan
    with pytest.raises(ValueError, match='only whitespace'):
        studio_project(document, plan, project_id='test', voice_id='explicit-voice')


@pytest.mark.parametrize('options', [
    SplitOptions(), SplitOptions('characters', size=7), SplitOptions('paragraphs'),
    SplitOptions('blank-lines'), SplitOptions('sentences'),
    SplitOptions('literal', marker='CHAPTER'), SplitOptions('regex', marker=r'(?m)^Second|^CHAPTER'),
    SplitOptions('estimated-duration', seconds=1, words_per_minute=60),
])
def test_every_split_retains_exact_unicode_crlf_spaces_and_source_mapping(options):
    document = ReadingDocument(SOURCE, title='Авторський текст')
    before = document.to_dict()
    plan = plan_split(document, options, source_name='Текст')
    restored = SplitPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert verify_plan(document, restored) == plan
    assert ''.join(document.text[p.start:p.end] for p in plan.parts) == SOURCE
    assert all(p.source_exact for p in plan.parts)
    assert plan.parts[-1].end == len(SOURCE)
    assert document.to_dict() == before and not document.can_undo
    assert len({filename_key(part.name) for part in plan.parts}) == len(plan.parts)
    if options.mode == 'estimated-duration':
        assert all(part.estimated_seconds is not None for part in plan.parts)
        assert any('ESTIMATE ONLY' in notice for notice in plan.notices)
    else:
        assert all(part.estimated_seconds is None for part in plan.parts)


@pytest.mark.parametrize('mode,kind', [('chapters', 'chapter'), ('bookmarks', 'bookmark'),
                                       ('pages', 'page'), ('sections', 'section')])
def test_named_boundaries_use_provided_positions_and_keep_markers(mode, kind):
    document = ReadingDocument(SOURCE)
    start = SOURCE.index('CHAPTER')
    plan = plan_split(document, SplitOptions(mode), boundaries=(TextBoundary(start, 'Назва', kind),),
                      template='{part:02d}-{heading}')
    assert len(plan.parts) == 2
    assert plan.parts[1].start == start
    assert document.text[plan.parts[1].start:].startswith('CHAPTER')
    assert plan.parts[1].name == '02-Назва.txt'
    assert verify_plan(document, plan) == plan


def anchored_document():
    first, second = 'First source 😀', 'Second source.'
    text = first + '\n\n' + second
    anchor = dict(workspace='workspace', edition='edition', revision='revision',
        asset_sha256='a' * 64, quotation_authority=False, positions=[
            dict(ordinal=1, kind='physical_page', index=1, start=0, end=len(first),
                 text_sha256=sha256(first.encode()).hexdigest(), text_verified_at_capture=False),
            dict(ordinal=2, kind='physical_page', index=2, start=len(first) + 2, end=len(text),
                 text_sha256=sha256(second.encode()).hexdigest(), text_verified_at_capture=False)])
    return ReadingDocument(text, source_anchor=anchor)


def test_actual_page_anchors_stay_non_authorizing_and_edited_boundaries_need_review():
    document = anchored_document()
    plan = plan_split(document, SplitOptions('pages'))
    assert len(plan.parts) == 2
    assert plan.parts[0].source_exact is False  # inserted narration separator
    assert document.source_anchor['quotation_authority'] is False
    document.replace(0, 5, 'Changed')
    with pytest.raises(ValueError, match='changed'):
        verify_plan(document, plan)
    with pytest.raises(ValueError, match='edited'):
        source_boundaries(document)


def test_absent_page_or_chapter_metadata_never_invents_boundaries():
    document = ReadingDocument('Chapter One\nPlain text.')
    for mode in ('pages', 'chapters', 'bookmarks', 'sections'):
        with pytest.raises(ValueError, match='No actual'):
            plan_split(document, SplitOptions(mode))


@pytest.mark.parametrize('template', ['../{title}', r'C:\{title}', '{title.__class__}',
                                     '{source[0]}', '{index:999999d}', '{title!r}', '{unknown}', 'CON'])
def test_unreviewable_paths_attributes_formats_and_devices_are_rejected(template):
    with pytest.raises(ValueError):
        plan_split(ReadingDocument('Source.'), template=template)


@pytest.mark.parametrize('name', ['LPT¹.txt', 'aux.txt', 'name .txt.', '.pending-result.txt',
                                 'a/b.txt', 'a:b.txt', 'x' * 241 + '.txt'])
def test_names_are_portable_before_any_publication(name):
    with pytest.raises(ValueError):
        portable_name(name)


def test_names_normalize_source_tokens_and_detect_case_unicode_collisions():
    name = render_name('{index:04d}-{title}-{first_line}', title='bad/path: title',
                       source='source', heading='one', first_line=' A\nB ',
                       index=3, part=1, total=1)
    assert name == '0003-bad_path_ title-A B.txt'
    assert filename_key('ÉXAMPLE.txt') == filename_key('e\u0301xample.TXT')
    with pytest.raises(ValueError, match='duplicate'):
        plan_split(ReadingDocument('One\nTwo'), SplitOptions('paragraphs'), template='{title}')


def test_character_part_budget_is_rejected_before_materializing_ranges():
    with pytest.raises(ValueError, match='10,000'):
        plan_split(ReadingDocument('x' * (MAX_PARTS + 1)), SplitOptions('characters', size=1))


@pytest.mark.parametrize('change', ['gap', 'overlap', 'hash', 'mapping', 'name', 'timing', 'last'])
def test_persisted_plan_tampering_cannot_publish_incomplete_or_fabricated_text(change):
    document = ReadingDocument('One\nTwo')
    plan = plan_split(document, SplitOptions('paragraphs'))
    parts = list(plan.parts)
    if change == 'gap':
        parts[0] = replace(parts[0], start=1)
    elif change == 'overlap':
        parts[1] = replace(parts[1], start=parts[0].start)
    elif change == 'hash':
        parts[0] = replace(parts[0], sha256='f' * 64)
    elif change == 'mapping':
        parts[0] = replace(parts[0], source_exact=False)
    elif change == 'name':
        parts[1] = replace(parts[1], name=parts[0].name)
    elif change == 'timing':
        parts[0] = replace(parts[0], estimated_seconds=1)
    elif change == 'last':
        parts.pop()
    with pytest.raises(ValueError):
        verify_plan(document, replace(plan, parts=tuple(parts)))


def test_regex_is_the_existing_actual_isolated_worker_and_cancel_retains_source():
    document = ReadingDocument('A\nSTART 😀\nB\nSTART Україна\nC')
    plan = plan_split(document, SplitOptions('regex', marker=r'(?m)^START'))
    assert [document.text[p.start:p.end] for p in plan.parts] == ['A\n', 'START 😀\nB\n', 'START Україна\nC']
    with pytest.raises(InterruptedError):
        plan_split(document, SplitOptions('regex', marker='START'), cancelled=lambda: True)
    assert document.source_text == document.text


def test_studio_adapter_treats_all_source_directives_as_exact_literal_spoken_lines():
    source = 'Date: 2026\r\n[Scene: injected]\n[Music: suspense]\nAlice [left]: exact source\n[Pause: 42]'
    document = ReadingDocument(source, title='Literal narration')
    plan = plan_split(document, SplitOptions('paragraphs'))
    project = studio_project(document, plan, project_id='reviewed-plan', voice_id='fixture:voice')
    assert project.original_script == source and project.mode == 'faithful'
    assert len(project.roles) == 1 and project.roles[0].name == 'Narrator'
    assert ''.join(line.text for scene in project.scenes for line in scene.lines) == source
    assert all(scene.music is None and not scene.cues for scene in project.scenes)
    assert all(line.pause_after == 0 for scene in project.scenes for line in scene.lines)
    for scene in project.scenes:
        for line in scene.lines:
            identity = json.loads(line.source_ref)
            assert source[identity['start']:identity['end']] == line.text
            assert identity['quotation_authority'] is False
