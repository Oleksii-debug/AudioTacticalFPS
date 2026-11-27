"""Synthetic contract helpers; none of these files claims a real engine result."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata

from carr_library.reader.preparation import (
    BABEL_VERSION, HUNSPELL_REVISION, HUNSPELL_VERSION, LANGUAGES, NUM2WORDS_VERSION,
    PreparationContext, PreparationPreview, PreparationRecord, WHEELS,
)


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def manifest(tmp_path, engine='reading-preparation', *, settings_change=None, **flags):
    root = Path(tmp_path) / engine
    root.mkdir()
    if engine == 'reading-preparation':
        files = {'packages/' + name: ('# synthetic identity only: ' + name + '\n').encode()
                 for name in ('num2words/__init__.py', 'babel/__init__.py',
                              'babel/dates.py', 'babel/numbers.py', 'babel/units.py')}
        settings = dict(packages='packages', num2words_version=NUM2WORDS_VERSION,
                        babel_version=BABEL_VERSION, languages=list(LANGUAGES), source_wheels=WHEELS)
    else:
        files = {'bin/identity.dll': b'synthetic identity; not a native DLL',
                 'dictionary/test.aff': b'SET UTF-8\n', 'dictionary/test.dic': b'1\nhello\n'}
        settings = dict(dll='bin/identity.dll', aff='dictionary/test.aff', dic='dictionary/test.dic',
                        language='en', library_version=HUNSPELL_VERSION, source_revision=HUNSPELL_REVISION,
                        dictionary_id='synthetic-contract-only', dictionary_revision='b' * 64,
                        dictionary_license='CC0-1.0')
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if settings_change:
        settings.update(settings_change)
    value = dict(schema_version=1, engine=engine, model_id='synthetic-contract-only',
                 revision='a' * 64, license='CC0-1.0', license_accepted=True, trusted_weights=True,
                 personal_noncommercial=False, settings=settings,
                 files={name: sha256(raw).hexdigest() for name, raw in files.items()})
    value.update(flags)
    path = root / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def preview(project, start, end, after, *, context=None):
    context = context or PreparationContext('en', 'number')
    text = project.current_text[:start] + after + project.current_text[end:]
    record = PreparationRecord('reading-preparation',
        'num2words 0.5.14; Babel 2.18.0; Unicode ' + unicodedata.unidata_version,
        'c' * 64, project.original_sha256, digest(project.current_text), digest(text),
        start, end, project.current_text[start:end], after, tuple(sorted(asdict(context).items())))
    return PreparationPreview(text, record)


def normal_response(text, manifest_path):
    root = Path(manifest_path).parent / 'packages'
    modules = {'num2words': 'num2words/__init__.py', 'babel': 'babel/__init__.py',
               'babel.dates': 'babel/dates.py', 'babel.numbers': 'babel/numbers.py', 'babel.units': 'babel/units.py'}
    origins = {name: {'path': str(root / path), 'sha256': sha256((root / path).read_bytes()).hexdigest()}
               for name, path in modules.items()}
    return json.dumps(dict(replacement=text, isolated=True, no_site=True, module_origins=origins,
        engine_version='num2words 0.5.14; Babel 2.18.0; Unicode ' + unicodedata.unidata_version)).encode('utf-8')

