"""Progress preferences use the existing manifest-bound full backup contract."""
from hashlib import sha256
import json

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.operation_progress import ProgressPreferences, load_preferences, save_preferences


def test_actual_preferences_save_backup_restore_and_reload_exactly(tmp_path):
    original = LibraryApp(tmp_path / 'Оригінальна бібліотека')
    original.create_workspace('Private preference preservation')
    preferences = ProgressPreferences(mode='beep', interval_seconds=17, volume=.23,
        quiet=True, background=True, mute_during_audio=False)
    save_preferences(original.root, preferences)
    raw = (original.root / 'state/ui/progress.json').read_bytes()
    backup = tmp_path / 'Резервна копія'
    manifest_hash = create_backup(original, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    entry, = [row for row in manifest['sidecars'] if row['filename'] == 'state/ui/progress.json']
    assert entry == {'filename': 'state/ui/progress.json', 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    restored = LibraryApp(restore_backup(backup, tmp_path / 'Відновлена бібліотека', manifest_hash))
    assert (restored.root / 'state/ui/progress.json').read_bytes() == raw
    assert load_preferences(restored.root) == preferences
    assert load_preferences(original.root) == preferences
    assert not (restored.root / 'state/ui/.progress.lock').exists()
