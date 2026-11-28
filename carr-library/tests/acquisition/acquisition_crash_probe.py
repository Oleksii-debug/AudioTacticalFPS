"""Standalone abrupt-exit fixture bound to the exact parent-selected package."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    if len(sys.argv) != 6 or sys.argv[1] not in ('before_marker', 'after_marker'):
        raise SystemExit('Use the explicit acquisition completion fixture arguments.')
    boundary, directory, package_file, service_file, service_hash = sys.argv[1:]
    directory, package_file, service_file = (Path(value).resolve(strict=True)
                                            for value in (directory, package_file, service_file))
    # Like the existing resource spawn fixture, bind only the package already
    # selected by the parent. No checkout/src is added to the import path.
    spec = importlib.util.spec_from_file_location('carr_library', package_file,
        submodule_search_locations=[str(package_file.parent)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['carr_library'] = package
    spec.loader.exec_module(package)
    import carr_library
    from carr_library.acquisition import service
    from carr_library.operations import ResourceBroker
    assert Path(carr_library.__file__).resolve() == package_file
    assert Path(service.__file__).resolve() == service_file
    assert sha256(service_file.read_bytes()).hexdigest() == service_hash

    helpers = runpy.run_path(str(Path(__file__).with_name('test_acquisition_workflow.py')))
    app, workspace, transfers = helpers['sample'].__wrapped__(directory)
    transfers.pdf = helpers['pdf_bytes']()
    (directory / 'expected.pdf').write_bytes(transfers.pdf)
    preview = helpers['lookup'](app, workspace, oa=True)
    target = directory / 'completed'
    marker = target / '.acquisition-incomplete'
    actual_unlink = Path.unlink

    def witness():
        snapshot = ResourceBroker.for_app(app.root).snapshot()
        assert not snapshot['leases'], 'Heavy admission must be released before the completion point.'
        assert not app.browse(workspace), 'Import must remain after the publication boundary.'
        value = dict(boundary=boundary, package=str(package_file), service=str(service_file),
                     service_sha256=service_hash, library=str(app.root), workspace=workspace,
                     sha256=sha256(transfers.pdf).hexdigest(), bytes=len(transfers.pdf),
                     downloads=len(transfers.downloads), active_leases=snapshot['leases'])
        with (directory / 'witness.json').open('x', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())

    def unlink(path, *args, **kwargs):
        if path != marker:
            return actual_unlink(path, *args, **kwargs)
        if boundary == 'before_marker':
            witness()
            os._exit(74)
        result = actual_unlink(path, *args, **kwargs)
        witness()
        os._exit(73)
        return result  # pragma: no cover - the fixture must never return normally.

    Path.unlink = unlink
    app.acquire_doi_source(workspace, preview, 0, target,
        online_allowed=True, rights_confirmed=True)
    raise AssertionError('The requested abrupt completion boundary was not reached.')


if __name__ == '__main__':
    main()
