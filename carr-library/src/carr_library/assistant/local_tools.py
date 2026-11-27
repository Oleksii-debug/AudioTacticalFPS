"""Finite local assistant adapters over the application's existing services.

These tools do not grant quotation, runtime-installation, playback-device,
microphone or network authority. Plans remain plain data and the ordinary
AssistantService checkpoint is their only execution journal.
"""
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import math
from pathlib import Path

from .models import canonical, local_path, text


LOCAL_TOOLS = frozenset({
    'reader.page', 'evidence.search', 'document.convert', 'voice.list',
    'audio.plan', 'audio.effects', 'player.inspect',
})
INDEPENDENT_TOOLS = LOCAL_TOOLS - {'reader.page', 'evidence.search'}
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_SCRIPT_CHARACTERS = 200_000

_KEYS = {
    'reader.page': ({'edition', 'revision', 'page'}, {'topic'}),
    'evidence.search': ({'query'}, {'field', 'tag', 'limit', 'include_archived'}),
    'document.convert': ({'path', 'name'}, set()),
    'voice.list': (set(), {'limit'}),
    'audio.effects': ({'script', 'target', 'effects'}, {'voice', 'language', 'master_mix'}),
    'player.inspect': ({'path'}, set()),
}


def _integer(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid assistant ' + label + '.')
    return value


def validate_arguments(tool, arguments):
    """Pure shape validation; imported plans never open external files."""
    required, optional = _KEYS[tool]
    if type(arguments) is not dict or not required <= set(arguments) <= required | optional:
        raise ValueError('Invalid arguments for ' + tool + '.')
    for key, value in arguments.items():
        if key == 'page':
            _integer(value, 'reading position', 10_000)
        elif key == 'limit':
            _integer(value, 'result limit', 256 if tool == 'voice.list' else 100)
        elif key == 'include_archived':
            if type(value) is not bool:
                raise ValueError('Archive selection must be an explicit boolean.')
        elif key == 'target' and value is None:
            pass
        elif key == 'effects':
            from ..audio.models import Effect
            if type(value) is not list or len(value) > 16:
                raise ValueError('Choose at most sixteen typed effects.')
            for item in value:
                if type(item) is not dict or set(item) != {'kind', 'parameters', 'enabled'}:
                    raise ValueError('Effects need their exact existing kind/parameters/enabled schema.')
                Effect.from_dict(item)
        elif key == 'master_mix':
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value <= 1 or arguments.get('target') is not None):
                raise ValueError('Dry/wet mix belongs only to the selected master, from zero to one.')
        else:
            text(value, key, maximum=MAX_SCRIPT_CHARACTERS if key == 'script' else
                 500 if key == 'query' else 32768)
    if tool == 'evidence.search' and arguments.get('field', 'both') not in ('quote', 'note', 'both'):
        raise ValueError('Choose quotation text, notes or both for evidence search.')
    if tool == 'audio.effects':
        # Apply the same Role/master constraints used by the Studio. This is an
        # isolated script proposal; no current draft is touched.
        _edit_effects(arguments)


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def bounded_result(value, scope):
    plain = _plain(value)
    if len(canonical(plain).encode('utf-8')) > min(MAX_RESULT_BYTES, scope.max_bytes):
        raise ValueError('The assistant result exceeds its reviewed byte budget.')
    return plain


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Assistant local operation stopped.')


def _edit_effects(arguments):
    from ..audio.effect_editing import RoleEffectEditor
    from ..audio.models import Effect
    editor = RoleEffectEditor(arguments['script'], voice=arguments.get('voice', 'sapi5:default'),
                              language=arguments.get('language', 'en'))
    editor.set_chain(arguments['target'], tuple(Effect.from_dict(item) for item in arguments['effects']))
    if 'master_mix' in arguments:
        editor.set_master_mix(arguments['master_mix'])
    return editor.script()


def _conversion(scope):
    from ..conversion.models import ConversionBudget, MAX_REPORT_BYTES, MAX_SOURCE_BYTES, MAX_TEXT_BYTES
    from ..conversion.service import ConversionService
    if scope.output_root is None or scope.max_files < 2 or scope.max_bytes <= MAX_REPORT_BYTES:
        raise ValueError('Conversion needs an output directory, two files, and more than 16 MiB for its bounded report.')
    if scope.max_seconds < .1:
        raise ValueError('Document conversion needs an approved time budget of at least 0.1 second.')
    budget = ConversionBudget(
        max_source_bytes=min(MAX_SOURCE_BYTES, scope.max_bytes),
        max_text_bytes=min(MAX_TEXT_BYTES, scope.max_bytes - MAX_REPORT_BYTES),
        max_files=1, timeout=min(600.0, scope.max_seconds))
    # No executable/model path may arrive in plan JSON. This basic route uses
    # the current installed readers; explicitly selected Office/Pandoc/OCR
    # adapters remain in their existing reviewed conversion UI.
    return ConversionService(local_path(scope.output_root), input_roots=scope.input_roots, budget=budget)


def validate_admission(service, step, scope):
    tool, args = step.tool, step.arguments
    if tool in ('document.convert', 'player.inspect'):
        from ..audio.jobs import regular_file
        source = scope.input(args['path'])
        info = regular_file(source)
        if info.st_nlink != 1 or not 0 < info.st_size <= scope.max_bytes:
            raise ValueError('Choose an unlinked regular input within the approved byte budget.')
    if tool == 'document.convert':
        converter = _conversion(scope)
        converter._input(source)
        converter._output(scope.output(args['name']))
    if tool in ('voice.list', 'audio.plan') and service.production is None:
        raise RuntimeError('Configure the existing local voice/production service first.')
    if tool == 'audio.plan':
        service.production.plan(args, max_bytes=scope.max_bytes)


def execute(service, step, scope, *, workspace, cancelled):
    args, tool = step.arguments, step.tool
    _check(cancelled)
    if tool == 'reader.page':
        from ..reader.reading_desk import ReadingDesk
        desk = ReadingDesk(service.app, workspace, args['edition'], topic=args.get('topic'),
            revision=args['revision'], cancelled=cancelled, timeout=min(60.0, scope.max_seconds))
        page = desk.page(args['page'])
        result = dict(page=_plain(page), quotation_authority=False, notice=page.status)
    elif tool == 'evidence.search':
        result = service.app.search_notebook(workspace, cancelled=cancelled,
            timeout=min(60.0, scope.max_seconds), **args)
        # Return authenticated identities, not a second persisted quotation.
        # Opening/copying evidence still uses the ordinary selection service.
        result = dict(hits=[dict(topic=row.evidence.topic_id,
            evidence=row.evidence.evidence_id, checksum=row.evidence.checksum,
            source=_plain(row.evidence.anchor.identity),
            title=row.evidence.anchor.metadata.title, topic_path=list(row.topic_path),
            tags=list(row.tags), archived=row.archived, matched_fields=list(row.matched_fields))
            for row in result.hits], total_matches=result.total_matches,
            evidence_scanned=result.evidence_scanned, topics_scanned=result.topics_scanned,
            truncated=result.truncated, quotation_authority=False)
    elif tool == 'document.convert':
        from ..operations import ResourceBroker
        converter = _conversion(scope)
        target = scope.output(args['name'])
        conversion = converter.extract(scope.input(args['path']), target, cancelled=cancelled,
            broker=ResourceBroker.for_app(service.app.root, cancelled=cancelled))
        result = dict(artifacts=[str(conversion.path), str(conversion.report_path)],
                      reading_derivative={key: conversion.report[key] for key in (
                          'schema', 'source_sha256', 'source_format', 'source_size',
                          'text_sha256', 'text_size', 'verified_for_quotation', 'original_modified')},
                      quotation_authority=False)
    elif tool == 'voice.list':
        limit = args.get('limit', 100)
        rows, seen, truncated = [], set(), False
        for voice in service.production.voices(cancelled=cancelled):
            _check(cancelled)
            if len(rows) >= limit:
                truncated = True
                break
            identity = text(voice.id, 'voice identity', maximum=4096)
            if identity in seen:
                raise ValueError('The configured local registry repeated a voice identity.')
            seen.add(identity)
            languages = tuple(voice.languages)
            if len(languages) > 256:
                raise ValueError('The configured voice exceeds its language metadata budget.')
            for language in languages:
                text(language, 'voice language', maximum=128)
            rows.append(dict(id=identity, name=text(getattr(voice, 'name', identity),
                'voice name', maximum=4096), languages=list(languages)))
        result = dict(voices=rows, truncated=truncated,
                      notice='Availability is descriptive; each render revalidates its selected local runtime.')
    elif tool == 'audio.plan':
        plan = service.production.plan(args, max_bytes=scope.max_bytes, cancelled=cancelled)
        result = dict(production_plan=plan.to_dict(), rendered=False)
    elif tool == 'audio.effects':
        result = dict(script=_edit_effects(args), original_sha256=sha256(args['script'].encode('utf-8')).hexdigest(),
                      target=args['target'], draft_modified=False, rendered=False)
    elif tool == 'player.inspect':
        from ..audio.ffmpeg import wav_info
        from ..audio.jobs import regular_file, file_identity, verified_reader
        path = scope.input(args['path'])
        info = regular_file(path)
        if info.st_nlink != 1 or not 0 < info.st_size <= scope.max_bytes:
            raise ValueError('The selected PCM source exceeds its approved input budget.')
        digest = sha256()
        with verified_reader(path, expected_identity=file_identity(info)) as stream:
            audio = wav_info(path)
            while block := stream.read(1024 * 1024):
                _check(cancelled)
                digest.update(block)
        result = dict(path=str(path), sha256=digest.hexdigest(), size=info.st_size,
                      audio=_plain(audio), seconds=audio.seconds, playback_started=False)
    else:
        raise ValueError('Unknown finite local assistant tool.')
    _check(cancelled)
    return bounded_result(result, scope)
