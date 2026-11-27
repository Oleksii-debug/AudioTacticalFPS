"""Original client for an explicitly selected dordiai-voices installation.

The installed engine owns its voice policy, package validation and resource
inventory. This bridge invokes those APIs in that engine's Python process;
it does not vendor its implementation, voice names, models or recordings.
Resource presence means structural compatibility, not listening acceptance.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import sys


DISCOVERY_PROTOCOL = 'carr.dordiai-installed-voices.v2'
MAX_VOICES = 4096
MAX_LANGUAGES = 512
MAX_RESPONSE_BYTES = 1024 * 1024
REQUIRED_HELPERS = ('windows_runtime.py', 'voice_policy.py', 'language_catalog.py',
                    'voice_assets.py', 'wide_languages.py')
ROUTES = frozenset({'wide', 'native_profile', 'native_locale', 'ukrainian'})
LANGUAGE_FIELDS = frozenset({'code', 'name', 'resources_present', 'status',
                             'partial_variant', 'interactive_reading_ready', 'route'})


def _text(value, label, *, maximum=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f'Invalid installed {label}.')
    return value


def _profile(value):
    value = _text(value, 'voice identifier')
    if not re.fullmatch('[A-Za-z0-9_-]{1,255}', value):
        raise ValueError('Unsupported installed voice identifier.')
    return value


def _language(value):
    value = _text(value, 'language identifier', maximum=32)
    if not re.fullmatch('[a-z]{2,3}(?:-[a-z0-9]{2,8})*', value):
        raise ValueError('Unsupported installed language identifier.')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate installed inventory field.')
        result[key] = value
    return result


def _decode(raw):
    def reject_nonfinite(value):
        raise ValueError('Invalid installed inventory number.')
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_nonfinite)


def parse_inventory(raw):
    """Validate the bounded voices and exact installed language routes."""
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('Installed voice discovery exceeded its response budget.')
    document = _decode(raw.decode('utf-8'))
    if (not isinstance(document, dict) or set(document) != {'protocol', 'voices', 'languages'}
            or document['protocol'] != DISCOVERY_PROTOCOL
            or not isinstance(document['voices'], list)
            or len(document['voices']) > MAX_VOICES
            or not isinstance(document['languages'], list)
            or len(document['languages']) > MAX_LANGUAGES):
        raise ValueError('Unsupported installed voice discovery response.')
    statuses, available, codes = [], set(), set()
    for row in document['languages']:
        if not isinstance(row, dict) or set(row) != LANGUAGE_FIELDS:
            raise ValueError('Invalid installed language record.')
        code = _language(row['code'])
        route = row['route']
        if (code in codes or (route is not None and (not isinstance(route, str) or route not in ROUTES))
                or type(row['resources_present']) is not bool
                or type(row['partial_variant']) is not bool
                or (row['interactive_reading_ready'] is not None
                    and type(row['interactive_reading_ready']) is not bool)
                or (row['resources_present'] and route is None)):
            raise ValueError('Invalid installed language route or resource status.')
        _text(row['name'], 'language label')
        _text(row['status'], 'language status', maximum=128)
        codes.add(code)
        statuses.append(dict(row))
        if row['resources_present']:
            available.add(code)
    result, seen = [], set()
    for row in document['voices']:
        if not isinstance(row, dict) or set(row) != {'id', 'name', 'languages'}:
            raise ValueError('Invalid installed voice record.')
        identity = _profile(row['id'])
        name = _text(row['name'], 'voice label')
        languages = row['languages']
        if (not isinstance(languages, list) or not 1 <= len(languages) <= MAX_LANGUAGES
                or identity in seen):
            raise ValueError('Invalid installed voice languages or duplicate identity.')
        languages = tuple(_language(code) for code in languages)
        if len(set(languages)) != len(languages) or any(code not in available for code in languages):
            raise ValueError('Duplicate or unavailable installed voice language.')
        seen.add(identity)
        result.append((identity, name, languages))
    return tuple(result), tuple(statuses)


def parse_discovery(raw):
    """Retain the voice-tuple reader API while requiring the v2 route proof."""
    return parse_inventory(raw)[0]

def _offline_audit(event, args):
    # Complement the parent process's offline environment. Installed Python,
    # helpers and native dependencies are trusted; this is not a hostile-code
    # sandbox and does not claim to constrain arbitrary native library syscalls.
    if event in {'socket.__new__', 'socket.connect', 'socket.connect_ex', 'socket.bind',
                 'socket.getaddrinfo', 'socket.sendto', 'urllib.Request',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('Installed voice discovery is local and read-only.')


def installed_inventory(runtime, models, references):
    """Called only in the selected, manifest-verified interpreter subprocess."""
    runtime, models, references = (Path(value).resolve(strict=True)
                                   for value in (runtime, models, references))
    if (not runtime.is_dir() or models != runtime / 'models'
            or references != models / 'voices' or not references.is_dir()):
        raise ValueError('Unsupported installed runtime model layout.')
    sys.path.insert(0, str(runtime))
    def selected_module(name):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve(strict=True) != runtime / (name + '.py'):
            raise ValueError('Installed discovery helper resolved outside the reviewed runtime.')
        return module

    # The reviewed installed owner configures only its isolated interpreter's
    # DLL/import paths. No private runtime code is imported into the host.
    bootstrap = selected_module('windows_runtime')
    bootstrap.reset_inherited_dll_directory()
    bootstrap.configure_worker_import_paths()
    language_api = selected_module('language_catalog')
    worker = selected_module('nvda_worker')
    package_api = selected_module('voice_assets')
    policy = selected_module('voice_policy')
    wide_api = selected_module('wide_languages')
    if type(worker.OUTPUT_RATE) is not int or worker.OUTPUT_RATE != 24000:
        raise ValueError('Installed worker does not provide the reviewed 24 kHz PCM contract.')
    native_locales = worker.CLASSMATE_LOCALES
    if (not isinstance(native_locales, (tuple, list, set, frozenset))
            or len(native_locales) > MAX_LANGUAGES):
        raise ValueError('Invalid installed native-language routes.')
    native_locales = {_language(code) for code in native_locales}
    path = references / 'catalog.json'
    if not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError('Installed voice catalogue exceeds its byte budget.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('Installed voice catalogue exceeds its byte budget.')
    catalog = _decode(raw.decode('utf-8-sig'))
    if not isinstance(catalog, dict) or len(catalog) > MAX_VOICES:
        raise ValueError('Expected the installed flat voice catalogue.')
    for identity, label in catalog.items():
        _profile(identity)
        _text(label, 'voice label')
    catalog = policy.filter_voice_catalog(catalog)
    if not isinstance(catalog, dict):
        raise ValueError('Unsupported installed voice-policy API.')
    managed = package_api.discover_voices(references, engine_id='holos')
    if not isinstance(managed, dict) or len(managed) > MAX_VOICES:
        raise ValueError('Unsupported installed voice-package API.')
    for identity, document in managed.items():
        _profile(identity)
        if (not isinstance(document, dict) or document.get('id') != identity
                or not isinstance(document.get('engine'), dict)
                or document['engine'].get('id') != 'holos'
                or type(document['engine'].get('compatibility_version')) is not int
                or document['engine']['compatibility_version'] != 1):
            raise ValueError('Unsupported installed voice package.')
        catalog[identity] = _text(document.get('name'), 'voice label')
    catalog = policy.filter_voice_catalog(catalog)
    if not isinstance(catalog, dict) or len(catalog) > MAX_VOICES:
        raise ValueError('Installed voice catalogue exceeds its record budget.')
    statuses, languages, seen = [], [], set()
    for row in language_api.coverage(models, selected_only=False):
        if len(statuses) >= MAX_LANGUAGES or not isinstance(row, dict):
            raise ValueError('Unsupported installed language inventory.')
        code = _language(row.get('runtime_code'))
        if code in seen or type(row.get('resources_present')) is not bool:
            raise ValueError('Invalid installed language coverage.')
        seen.add(code)
        # The selected owner returns a registered code/record or None in
        # reviewed versions; its public predicate uses truth, not a bool type.
        registered = bool(wide_api.registered(code))
        route = ('wide' if registered else 'native_profile' if code in {'en', 'ru'}
                 else 'native_locale' if code in native_locales
                 else 'ukrainian' if code == 'uk' else None)
        present = row['resources_present'] and route is not None
        status = {'code': code, 'name': _text(row.get('settings_label_uk'), 'language label'),
                  'resources_present': present,
                  'status': _text(row.get('status'), 'language status', maximum=128),
                  'partial_variant': row.get('partial_variant', False),
                  'interactive_reading_ready': row.get('interactive_reading_ready'),
                  'route': route}
        if (type(status['partial_variant']) is not bool
                or (status['interactive_reading_ready'] is not None
                    and type(status['interactive_reading_ready']) is not bool)):
            raise ValueError('Invalid installed language readiness metadata.')
        statuses.append(status)
        if present:
            languages.append(code)
    allowed = {identity: [] for identity in catalog}
    for code in languages:
        builtin = language_api.compatible_voice_ids(models, code, tuple(catalog))
        if (not isinstance(builtin, (tuple, list, set, frozenset))
                or len(builtin) > MAX_VOICES or any(identity not in catalog for identity in builtin)):
            raise ValueError('Unsupported installed voice compatibility response.')
        required_role = {'uk': 'holos_profile', 'en': 'pocket_english_state',
                         'ru': 'pocket_russian_prompt'}.get(code, 'target_embedding')
        for identity in catalog:
            if identity not in managed:
                if identity in builtin:
                    allowed[identity].append(code)
                continue
            document = managed[identity]
            package_languages, files = document.get('languages'), document.get('files')
            if (not isinstance(package_languages, list) or len(package_languages) > MAX_LANGUAGES
                    or not isinstance(files, dict) or len(files) > 64
                    or any(not isinstance(entry, dict) for entry in files.values())):
                raise ValueError('Invalid installed voice-package resources.')
            package_languages = {_language(language_api.canonical_code(item))
                                 for item in package_languages}
            roles = {entry.get('role') for entry in files.values()}
            if code in package_languages and required_role in roles:
                allowed[identity].append(code)
    return {'protocol': DISCOVERY_PROTOCOL, 'languages': statuses, 'voices': [
        {'id': identity, 'name': catalog[identity], 'languages': codes}
        for identity, codes in allowed.items() if codes]}


def main():
    sys.addaudithook(_offline_audit)
    if len(sys.argv) != 4:
        raise ValueError('Installed discovery requires one reviewed runtime and its model directories.')
    document = installed_inventory(*sys.argv[1:])
    encoded = json.dumps(document, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError('Installed voice discovery exceeded its response budget.')
    parse_discovery(encoded)
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never expose private catalogue contents, paths or helper diagnostics.
        print('Installed voice discovery failed; review runtime resources and compatibility.', file=sys.stderr)
        raise SystemExit(1)
