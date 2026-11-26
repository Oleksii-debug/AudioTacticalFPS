# Validate local model execution without network access

The application applies `ORT_DISABLE_TELEMETRY=1` before importing ONNX Runtime,
along with Hugging Face offline and telemetry controls. The supported runtime
is pinned to ONNX Runtime 1.32.0. Its public telemetry API is also disabled before
FastEmbed is imported. Construction stops if the control fails or if an unsafe
earlier native import is detected. Windows launchers apply the environment
before starting Python.

The startup setting matters: upstream ONNX Runtime distinguishes its
process-lifetime opt-out from API suppression of non-essential events. The API
alone can follow an initialization event. See the upstream
[privacy documentation](https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md)
and [Python telemetry API](https://onnxruntime.ai/docs/api/python/api_summary.html#telemetry-events).

## Reproduce the independent check

From `carr-library/`, set `CARR_TEST_MODEL_DIR` and `CARR07_MODEL_DIR` to the
reviewed local MiniLM directory, and `CARR07_MODEL_MANIFEST` to its sealed
manifest. These checks never download a model. Run them using the pinned
development environment:

```sh
PYTHONPATH=src:. python scripts/verify_offline.py \
  tests/e2e/test_semantic_runtime.py \
  tests/e2e/test_offline_startup.py \
  tests/packaging/test_offline_runtime_policy.py \
  tests/search/test_thematic_onnx_probe.py
```

The helper requires Linux `libseccomp`. Before loading pytest or inference
libraries, it denies socket creation, connection, binding, listening, accepting,
and sending syscalls. Anonymous local Unix socket pairs remain available for
process wakeups; they cannot create a network connection. Before test imports,
native C calls check that IPv4 and IPv6 sockets are denied and local IPC works.
The runner also blocks io_uring, whose networking operations can bypass those
ordinary socket syscalls, and refuses a process with inherited socket descriptors.
It aborts if the restriction cannot be installed and never
retries unrestricted. Newly created threads and child processes inherit the
filter. This restriction complements the product's startup controls; a Python
socket mock alone does not cover native-library networking.

This check establishes model operation with these kernel restrictions active.
It does not establish that a library never attempted networking, replace an OS
firewall, or prove native Windows or NVDA acceptance. Without the approved local
model, model tests report a skip; that is not evidence of completed inference.
