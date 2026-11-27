import pytest
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('transcription_fixture_builder', Path(__file__).with_name('fixture_runtime.py'))
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)
make_runtime = _builder.make_runtime


@pytest.fixture
def synthetic_asr_runtime(tmp_path):
    return make_runtime(tmp_path / 'synthetic-runtime')
