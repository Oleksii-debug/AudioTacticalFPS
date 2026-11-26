"""Apply documented telemetry opt-outs before native runtime initialization.

ONNX Runtime 1.32.0's installed Privacy.md requires ORT_DISABLE_TELEMETRY
before initialization. A later API call alone cannot prevent initial events.
Optional inference dependencies are never imported by package initialization.
"""
import importlib
import importlib.metadata
import os
import sys


OFFLINE_ENV = {
    'ORT_DISABLE_TELEMETRY': '1',
    'HF_HUB_DISABLE_TELEMETRY': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
    'DO_NOT_TRACK': '1',
}
_preloaded_without_optout = False


def configure_offline_process():
    """Enforce the application's offline policy, including child processes."""
    global _preloaded_without_optout
    if 'onnxruntime' in sys.modules and os.environ.get('ORT_DISABLE_TELEMETRY') != '1':
        _preloaded_without_optout = True
    os.environ.update(OFFLINE_ENV)


def disable_native_telemetry():
    """Load the pinned engine only after its initialization opt-out is set."""
    configure_offline_process()
    if _preloaded_without_optout:
        raise RuntimeError('Restart the application: the model runtime was loaded before its offline policy.')
    if importlib.metadata.version('onnxruntime') != '1.32.0':
        raise ValueError('Offline model execution requires the pinned ONNX Runtime 1.32.0.')
    runtime = importlib.import_module('onnxruntime')
    # Cover the platform-specific provider exposed by the public API as well.
    runtime.disable_telemetry_events()
    return runtime
