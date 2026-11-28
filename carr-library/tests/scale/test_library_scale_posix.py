"""Actual POSIX preopen substitutions; selected only by Linux scale gates."""
from hashlib import sha256
import importlib.util
import multiprocessing
import os
from pathlib import Path
import sys

import pytest
import carr_library.durable_state as durable_state


def _substitution_child(script, script_sha, durable_path, durable_sha, directory, kind, reply):
    try:
        assert sha256(Path(script).read_bytes()).hexdigest() == script_sha
        assert Path(durable_state.__file__).resolve() == Path(durable_path)
        assert sha256(Path(durable_state.__file__).read_bytes()).hexdigest() == durable_sha
        name = "_carr_scale_posix_actual_child"
        specification = importlib.util.spec_from_file_location(name, script)
        module = importlib.util.module_from_spec(specification)
        sys.modules[name] = module
        specification.loader.exec_module(module)
        source = Path(directory) / "source.txt"
        source.write_bytes(b"Actual original bytes before descriptor substitution.")
        actual_open = module._open_json_file
        visited = []

        def substitute(path):
            assert Path(path) == source and not visited
            visited.append(True)
            source.unlink()
            if kind == "fifo":
                os.mkfifo(source)
            else:
                another = Path(directory) / "another.txt"
                another.write_bytes(b"Another actual file, never an admitted fixture.")
                source.symlink_to(another)
            return actual_open(path)

        module._open_json_file = substitute
        try:
            module.digest_file(source)
        except (OSError, ValueError):
            assert visited == [True]
        else:
            raise AssertionError("A substituted special object was admitted.")
        reply.send({"status": "PASS", "pid": os.getpid(), "kind": kind,
                    "script_sha256": script_sha, "durable_sha256": durable_sha})
    except BaseException as error:
        reply.send({"status": "FAIL", "type": type(error).__name__, "error": str(error)[:1024]})
    finally:
        reply.close()


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_actual_child_never_reads_a_substituted_special_object(tmp_path, kind):
    assert os.name == "posix", "This native boundary belongs only to the POSIX gate."
    script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_library_scale.py"
    script_sha = sha256(script.read_bytes()).hexdigest()
    durable_path = Path(durable_state.__file__).resolve()
    durable_sha = sha256(durable_path.read_bytes()).hexdigest()
    # Fork retains the actually selected source/installed module. The child
    # independently verifies both real files; no package path is substituted.
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_substitution_child, args=(
        str(script), script_sha, str(durable_path), durable_sha, str(tmp_path), kind, child))
    process.start()
    child.close()
    try:
        process.join(8)
        assert not process.is_alive(), "Non-following descriptor admission blocked on a real special object."
        assert process.exitcode == 0 and parent.poll()
        result = parent.recv()
        assert result == {"status": "PASS", "pid": process.pid, "kind": kind,
                          "script_sha256": script_sha, "durable_sha256": durable_sha}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        if process.is_alive():
            process.kill()
            process.join(3)
        parent.close()
        assert not process.is_alive(), "The actual authored child did not quiesce."
        process.close()
