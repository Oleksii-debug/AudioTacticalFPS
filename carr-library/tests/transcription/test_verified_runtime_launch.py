"""One required native runtime shard; assertions live in the common case module."""
import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location('verified_runtime_cases', Path(__file__).with_name('_verified_runtime_cases.py'))
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)
runtime, setup = _cases.runtime, _cases.setup

test_worker_launch_skips_pth_and_uses_fresh_bytecode_namespace = _cases.test_worker_launch_skips_pth_and_uses_fresh_bytecode_namespace
test_sourceless_bytecode_is_rejected_before_launch = _cases.test_sourceless_bytecode_is_rejected_before_launch
test_real_process_uses_new_protocol_and_verifies_completion_file_union = _cases.test_real_process_uses_new_protocol_and_verifies_completion_file_union
