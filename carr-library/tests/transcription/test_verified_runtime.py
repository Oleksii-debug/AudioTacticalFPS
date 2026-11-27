"""One required native runtime shard; assertions live in the common case module."""
import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location('verified_runtime_cases', Path(__file__).with_name('_verified_runtime_cases.py'))
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)
runtime, setup = _cases.runtime, _cases.setup

test_descriptor_opts_into_complete_inspection_without_execution = _cases.test_descriptor_opts_into_complete_inspection_without_execution
test_changed_identity_requires_new_review_before_any_process = _cases.test_changed_identity_requires_new_review_before_any_process
