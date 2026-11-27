"""One required native runtime shard; assertions live in the common case module."""
import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location('verified_runtime_cases', Path(__file__).with_name('_verified_runtime_cases.py'))
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)
runtime, setup = _cases.runtime, _cases.setup

test_partial_run_preserves_accepted_source_and_resumes_only_unfinished = _cases.test_partial_run_preserves_accepted_source_and_resumes_only_unfinished
test_cancel_uses_worker_cancel_flag_then_releases_resource_lease = _cases.test_cancel_uses_worker_cancel_flag_then_releases_resource_lease
test_restored_historical_cancel_flags_are_scrubbed = _cases.test_restored_historical_cancel_flags_are_scrubbed
