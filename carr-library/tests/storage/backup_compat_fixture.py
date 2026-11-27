"""Build explicit historical v2/v3/v4 fixtures from a checked current snapshot.

This deliberately removes the new pending component from a test fixture. It is
not a production backup option. Current-format roundtrips use create_backup
directly and separately prove queued-source and checkpoint preservation.
"""
from hashlib import sha256
import json
from pathlib import Path
import shutil

from carr_library.library_backup import create_backup, _reader_path


def historical_backup(folder):
    folder = Path(folder)
    path = folder / 'manifest.json'
    manifest = json.loads(path.read_bytes())
    if manifest['version'] == 5:
        del manifest['pending']
        shutil.rmtree(folder / 'pending')
        entries = manifest.pop('sidecars')
        # Historical managed audio/assistant archives used paths under sidecars;
        # historical reading/preferences archives used filenames at the root.
        managed = any(not _reader_path(entry['path']) for entry in entries)
        if managed:
            manifest.update(version=4, sidecars=entries)
        else:
            for source in sorted((folder / 'sidecars').rglob('*')):
                if source.is_file():
                    target = folder / source.relative_to(folder / 'sidecars')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
            shutil.rmtree(folder / 'sidecars')
            manifest['version'] = 3 if entries else 2
            if entries:
                manifest['sidecars'] = [
                    dict(filename=entry['path'], size_bytes=entry['size_bytes'], sha256=entry['sha256'])
                    for entry in entries]
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    return sha256(path.read_bytes()).hexdigest()


def create_historical_backup(app, destination, **kwargs):
    create_backup(app, destination, **kwargs)
    return historical_backup(destination)
