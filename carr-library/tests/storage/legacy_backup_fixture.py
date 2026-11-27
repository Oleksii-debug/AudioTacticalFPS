"""Construct exact historical state envelopes from synthetic test libraries.

The current product writer remains typed format 4. This fixture deliberately
uses the original bounded state producer and independently reseals an archive
to exercise legacy readers; it never adds a permissive production fallback.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from carr_library.library_backup import create_backup
from carr_library.state_backup import snapshot_state


def create_legacy_state_backup(app, destination, *, version=5, include_generated=False):
    if version not in (3, 4, 5):
        raise ValueError('Choose an exact historical state fixture version.')
    destination = Path(destination)
    state = app.root / 'state'
    parked = app.root.parent / ('.legacy-fixture-' + uuid4().hex)
    existed = os.path.lexists(state)
    if existed:
        state.rename(parked)
    try:
        create_backup(app, destination, include_generated=include_generated)
    finally:
        if existed:
            parked.rename(state)
    manifest = json.loads((destination / 'manifest.json').read_bytes())
    sidecars = manifest.pop('sidecars')
    assert all(entry['filename'] == 'reading-state.json' for entry in sidecars)
    reading = sidecars[0] if sidecars else None
    manifest.pop('studio_media', None)
    (destination / 'state').mkdir()
    manifest['state'] = snapshot_state(state, destination / 'state',
                                      include_generated=include_generated)
    manifest['version'] = version
    if version == 4:
        assert manifest.get('pending') is not None, 'Historical v4 requires its pending component.'
    elif version == 5:
        manifest.setdefault('pending', None)
    else:
        if manifest.pop('pending', None) is not None:
            shutil.rmtree(destination / 'pending')
    if version == 5:
        assert reading is not None, 'Historical v5 requires a declared reading-state file.'
        manifest['reading_state'] = reading
    elif reading is not None:
        (destination / 'reading-state.json').unlink()
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    (destination / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()
