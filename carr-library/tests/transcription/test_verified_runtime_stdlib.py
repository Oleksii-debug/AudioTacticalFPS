"""One required native runtime shard; assertions live in the common case module."""
import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location('verified_runtime_cases', Path(__file__).with_name('_verified_runtime_cases.py'))
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)
runtime, setup = _cases.runtime, _cases.setup

test_changed_standard_library_source_revokes_profile_before_launch = _cases.test_changed_standard_library_source_revokes_profile_before_launch
