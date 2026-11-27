"""Capture and restore only this per-user standard installer's wrapper state."""
from __future__ import annotations

import base64
import os
from pathlib import Path

from ._files import TrustError, atomic_write, directory, file_info, json_bytes, read_bytes


KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7421E0E1-9394-4A8D-8AF8-DAD3CF947D18}_is1'
LINKS = ('Resonance.lnk', 'Repair Resonance.lnk', 'Uninstall Resonance.lnk')
_OWNED_VALUES = frozenset(value.casefold() for value in (
    'DisplayIcon', 'DisplayName', 'DisplayVersion', 'EstimatedSize', 'InstallDate',
    'InstallLocation', 'NoModify', 'NoRepair', 'Publisher', 'QuietUninstallString',
    'UninstallString', 'URLInfoAbout', 'URLUpdateInfo', 'VersionMajor', 'VersionMinor',
    'ModifyPath', 'NoRemove', 'Language', 'HelpLink', 'HelpTelephone'))
MAX_SNAPSHOT = 1024 * 1024


def _binary(value):
    if not isinstance(value, str):
        raise TrustError('Invalid saved installation wrapper bytes.')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError):
        raise TrustError('Invalid saved installation wrapper bytes.') from None
    if len(raw) > 128 * 1024:
        raise TrustError('Saved installation wrapper bytes exceed their allowance.')
    return raw


def validate_snapshot(value, root):
    if (type(value) is not dict or set(value) != {'schema', 'root', 'menu_root', 'registry_present', 'values', 'shortcuts'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['root'] != str(Path(root).absolute())
            or not isinstance(value['menu_root'], str) or not Path(value['menu_root']).is_absolute()
            or '..' in Path(value['menu_root']).parts
            or type(value['registry_present']) is not bool
            or type(value['values']) is not dict or not 0 <= len(value['values']) <= 128
            or (not value['registry_present'] and value['values'])
            or type(value['shortcuts']) is not dict or set(value['shortcuts']) != set(LINKS)
            or len(json_bytes(value)) > MAX_SNAPSHOT):
        raise TrustError('Unsupported managed installer wrapper snapshot.')
    seen = set()
    for name, item in value['values'].items():
        if (not isinstance(name, str) or not 0 < len(name) <= 256
                or any(ord(c) < 32 for c in name) or name.casefold() in seen
                or type(item) is not dict or set(item) != {'type', 'value'}
                or type(item['type']) is not int):
            raise TrustError('Invalid saved installer registration.')
        seen.add(name.casefold())
        kind, data = item['type'], item['value']
        if kind in (0, 3):
            _binary(data)
        elif kind in (1, 2):
            if not isinstance(data, str) or len(data) > 32768 or '\0' in data:
                raise TrustError('Invalid saved installer registration string.')
        elif kind in (4, 11):
            if type(data) is not int or not 0 <= data < 2 ** (32 if kind == 4 else 64):
                raise TrustError('Invalid saved installer registration number.')
        elif kind == 7:
            if (type(data) is not list or len(data) > 128
                    or any(not isinstance(s, str) or '\0' in s or len(s) > 32768 for s in data)):
                raise TrustError('Invalid saved installer registration string list.')
        else:
            raise TrustError('Unsupported saved installer registration value type.')
    for raw in value['shortcuts'].values():
        if raw is not None:
            _binary(raw)
    return value


class WindowsRegistration:
    """No shell, COM script or arbitrary registry path is accepted."""
    def __init__(self):
        if os.name != 'nt':
            raise TrustError('Managed installer registration requires native Windows.')
        import winreg
        self.winreg = winreg
        appdata = os.environ.get('APPDATA', '')
        if not appdata or not Path(appdata).is_absolute() or appdata.startswith(('\\\\', '//')):
            raise TrustError('A local per-user Start menu directory is required.')
        self.menu_root = Path(appdata) / 'Microsoft/Windows/Start Menu/Programs/Resonance'

    def _open(self, write=False):
        winreg = self.winreg
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0,
            winreg.KEY_READ | (winreg.KEY_WRITE if write else 0) | winreg.KEY_WOW64_64KEY)

    def _registration(self, *, allow_missing=False):
        winreg, values = self.winreg, {}
        try:
            context = self._open()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        with context as key:
            subkeys, count, stamp = winreg.QueryInfoKey(key)
            if subkeys or not 0 <= count <= 128:
                raise TrustError('Managed installation registration exceeds its fixed scope.')
            for index in range(count):
                name, value, kind = winreg.EnumValue(key, index)
                if kind in (winreg.REG_BINARY, winreg.REG_NONE):
                    value = base64.b64encode(value).decode('ascii')
                values[name] = {'type': kind, 'value': value}
            if winreg.QueryInfoKey(key) != (subkeys, count, stamp):
                raise TrustError('Managed installation registration changed during capture.')
        return values

    @staticmethod
    def _location(values, root):
        lookup = {name.casefold(): item for name, item in values.items()}
        item = lookup.get('installlocation')
        if (item is None or item['type'] != 1 or not isinstance(item['value'], str)
                or os.path.normcase(os.path.abspath(item['value'])) != os.path.normcase(str(Path(root).absolute()))):
            raise TrustError('Installer registration belongs to another program directory.')
        return lookup

    def snapshot(self, root, *, allow_partial=False):
        # A failed B snapshot is inert recovery evidence. It can retain an
        # absent or partial fixed registration without treating that content as
        # authority to overwrite a foreign current registration.
        values = self._registration(allow_missing=allow_partial)
        present = values is not None
        values = values or {}
        if not allow_partial:
            self._location(values, root)
        directory(self.menu_root.parent)
        if os.path.lexists(self.menu_root):
            directory(self.menu_root)
        elif not allow_partial:
            raise TrustError('The original standard installation has no Start-menu folder.')
        shortcuts = {}
        for name in LINKS:
            path = self.menu_root / name
            shortcuts[name] = (base64.b64encode(read_bytes(path, 128 * 1024)).decode('ascii')
                               if os.path.lexists(path) else None)
        return validate_snapshot({'schema': 1, 'root': str(Path(root).absolute()),
            'menu_root': str(self.menu_root), 'registry_present': present,
            'values': values, 'shortcuts': shortcuts}, root)

    def health(self, root, release):
        self.health_failure = None
        value = self.snapshot(root)
        lookup = self._location(value['values'], root)
        expected = {'displayname': 'Resonance', 'displayversion': release.version,
                    'modifypath': '"' + str(Path(root) / 'maintenance/ResonanceSetup.exe') + '"',
                    'uninstallstring': '"' + str(Path(root) / 'unins000.exe') + '"'}
        for name, text in expected.items():
            item = lookup.get(name)
            if item is None or item['type'] != 1 or os.path.normcase(item['value']) != os.path.normcase(text):
                # Report only the fixed field name, never registry values,
                # program paths or retained shortcut contents.
                self.health_failure = 'registration-' + name
                return False
        if not all(value['shortcuts'][name] is not None for name in LINKS):
            self.health_failure = 'registration-shortcuts'
            return False
        return True

    def restore(self, root, saved):
        saved = validate_snapshot(saved, root)
        if saved['menu_root'] != str(self.menu_root):
            raise TrustError('The saved Start menu belongs to another user profile.')
        if not saved['registry_present']:
            raise TrustError('Only a captured original installation can authorize registration recovery.')
        self._location(saved['values'], root)
        directory(self.menu_root.parent)
        if os.path.lexists(self.menu_root):
            directory(self.menu_root)
        current = self._registration(allow_missing=True)
        missing = current is None
        current = current or {}
        if current:
            # Missing InstallLocation on a nonempty key is not proof that the
            # key belongs to this root. Its partial bytes remain retained.
            self._location(current, root)
        before = {name.casefold(): (name, item) for name, item in saved['values'].items()}
        for name, item in current.items():
            key = name.casefold()
            ours = key in _OWNED_VALUES or key.startswith('inno setup:')
            if not ours and key in before and before[key][1] != item:
                raise TrustError('An unrelated registration value changed; preserve it for reviewed recovery.')
        context = (self.winreg.CreateKeyEx(self.winreg.HKEY_CURRENT_USER, KEY, 0,
                   self.winreg.KEY_READ | self.winreg.KEY_WRITE | self.winreg.KEY_WOW64_64KEY)
                   if missing else self._open(write=True))
        with context as key:
            if not current:
                # Publish the exact root binding first. If subsequent replay
                # fails, the next attempt can distinguish its own partial
                # recovery from a foreign nonempty unbound registration.
                name, item = before['installlocation']
                self.winreg.SetValueEx(key, name, 0, item['type'], item['value'])
                self.winreg.FlushKey(key)
            for name in current:
                folded = name.casefold()
                if folded not in before and (folded in _OWNED_VALUES or folded.startswith('inno setup:')):
                    self.winreg.DeleteValue(key, name)
            for name, item in saved['values'].items():
                data = _binary(item['value']) if item['type'] in (0, 3) else item['value']
                self.winreg.SetValueEx(key, name, 0, item['type'], data)
            self.winreg.FlushKey(key)
        directory(self.menu_root, create=True)
        for name, raw in saved['shortcuts'].items():
            path = self.menu_root / name
            if raw is None:
                if os.path.lexists(path):
                    file_info(path)
                    path.unlink()
            else:
                atomic_write(path, _binary(raw))
