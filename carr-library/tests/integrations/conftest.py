"""Resolve shared synthetic fixtures without changing application import paths."""
import importlib.util
from pathlib import Path
import sys


def _fixture_module(name):
    path = Path(__file__).with_name(name + '.py').resolve(strict=True)
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve(strict=True) != path:
            raise RuntimeError('A synthetic fixture module name is already occupied.')
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


# These fixture modules import one another using their established names.
# The application was selected by the source/installed runner; never add src,
# replace product modules or alter sys.path to make an installed test pass.
for _name in ('test_dordiai_voices', 'test_dordiai_speech', 'test_dordiai_settings'):
    _fixture_module(_name)
