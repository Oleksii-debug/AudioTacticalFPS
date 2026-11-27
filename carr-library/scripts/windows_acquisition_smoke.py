"""Actual native DOI/ISBN dialog event and focus gate, using offline fixtures."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This required native gate must run on Windows.')
    root = Path(__file__).resolve().parents[1]
    if not args.installed:
        sys.path.insert(0, str(root / 'src'))
    import carr_library
    module = Path(carr_library.__file__).resolve()
    if args.installed and module.is_relative_to(root / 'src'):
        raise RuntimeError('Installed acquisition probe loaded source-checkout code.')

    def offline(*_args, **_kwargs):
        raise AssertionError('Network access is disabled in the native acquisition probe.')
    socket.socket.connect = socket.socket.connect_ex = offline
    socket.create_connection = socket.getaddrinfo = offline
    path = root / 'tests/ui/test_acquisition_dialog.py'
    spec = importlib.util.spec_from_file_location('_acquisition_dialog_probe_helpers', path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    for identifier in ('doi', 'isbn'):
        with TemporaryDirectory(prefix='carr-native-acquisition-') as directory:
            helpers.native_dialog_async_lookup_focus_and_close(Path(directory), identifier)
    print(json.dumps(dict(result='PASS', package=str(module), installed=args.installed,
        identifiers=['doi', 'isbn'], native_event_dispatch='PASS', asynchronous_completion='PASS',
        preview_focus='PASS', default_network_denial='PASS', window_cleanup='PASS',
        real_provider_requests='NOT_RUN', nvda='NOT_RUN'), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
