"""Shared real-library/UsageLedger fixtures; only the HTTPS transport is synthetic."""
from pathlib import Path
import runpy
from time import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Authorization, Gateway
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_batch import TranslationBatchService
from carr_library.assistant.translation_source import capture_source

# Resolve only test helpers by their own file. Product imports continue to use
# the selected source or installed interpreter without sys.path injection.
_shared = runpy.run_path(str(Path(__file__).with_name('test_translation_v3.py')))
transport = _shared['transport']
profile, Vault = _shared['profile'], _shared['Vault']
GLOSSARY, TOKEN = _shared['GLOSSARY'], _shared['TOKEN']


@pytest.fixture
def source_library(tmp_path, transport):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit translation source')
    topic = app.create_topic(workspace, 'Selected topic')
    original = 'Original first paragraph. ' * 240 + '\fSecond source segment.\r\nУкраїна.'
    imported = app.add_text(workspace, 'Exact source title', original)
    p, vault = profile(), Vault()
    ProfileStore(app.root / 'state' / 'providers').save(p)
    service = TranslationService(app.root / 'state' / 'translation', library=app,
        gateway=Gateway(app.root / 'state' / 'translation' / 'provider',
                        vault=vault, mode='APPROVED_REMOTE'))
    approval = Authorization('source_discovery', 'source_discovery', ((p.id, p.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, original=original,
        imported=imported, p=p, vault=vault, service=service)


def select(e, **kwargs):
    return capture_source(e.app, e.workspace, e.imported['edition'], topic=e.topic, **kwargs)


def item(e, selected, **changes):
    return {'profile': e.p.to_dict(), 'text': selected['text'], 'title': selected['title'],
        'source': 'en', 'target': 'uk', 'budget': '1',
        **{key: selected[key] for key in ('source_anchor', 'source_metadata', 'source_topic')},
        **changes}


@pytest.fixture
def batch_env(source_library):
    e = source_library
    e.second = e.app.add_text(e.workspace, 'Second independently selected source',
                              'Other original text. Україна.\n')
    e.selected = [select(e), capture_source(e.app, e.workspace, e.second['edition'], topic=e.topic)]
    e.items = [item(e, e.selected[0]), item(e, e.selected[1], target='fr', glossary=GLOSSARY)]
    e.batches = TranslationBatchService(e.service)
    return e
