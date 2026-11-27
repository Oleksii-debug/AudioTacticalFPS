"""Passive backup adapters for reviewed Studio inputs, recordings and book plans.

Only declared data formats are accepted. Validation never discovers a device,
runs an audio engine or follows a project path outside the staged snapshot.
"""
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re

from .backup_producers import fields, text, number, digest, path
from .ingest.restore import RestoreError

HEX = r'[0-9a-f]{64}'
ID = r'[0-9a-f]{32}'
INPUT = re.compile(r'state/studio/inputs/(' + HEX + r')\.audio\Z')
RECORDING = re.compile(r'state/studio/recordings/(' + ID + r')\.(json|wav)\Z')
PRESET = re.compile(r'state/studio/effect-presets/(' + ID + r')\.json\Z')
BOOK = re.compile(r'state/studio/book-plans/(' + HEX + r')/(' + ID + r')\.json\Z')
BOOK_SOURCE = re.compile(r'state/studio/jobs/([A-Za-z0-9_-]{1,80})/book-source\.json\Z')
INPUT_RECEIPT = 'state/studio/backup-inputs.json'
INPUT_REVIEW = 'state/studio/legacy-input-review.json'
MAX_MEDIA_BYTES = 32 * 1024**3
MAX_RECORDING_BYTES = 2 * 1024**3


def limit(name):
    if INPUT.fullmatch(name):
        return MAX_MEDIA_BYTES
    if match := RECORDING.fullmatch(name):
        return MAX_RECORDING_BYTES if match[2] == 'wav' else 64 * 1024
    if PRESET.fullmatch(name):
        return 64 * 1024
    if BOOK.fullmatch(name) or BOOK_SOURCE.fullmatch(name) or name in (INPUT_RECEIPT, INPUT_REVIEW):
        return 32 * 1024 * 1024
    return None


def is_media(name):
    match = RECORDING.fullmatch(name)
    return INPUT.fullmatch(name) is not None or match is not None and match[2] == 'wav'


def validate_source_snapshot(db, anchor):
    from .reader.reading_state import ReadingLocation
    from .speech.reading import _validated_source_anchor

    fields(anchor, {'workspace', 'edition', 'revision', 'asset_sha256',
                    'positions', 'quotation_authority'})
    if type(anchor['positions']) is not list or not 1 <= len(anchor['positions']) <= 10000:
        raise ValueError('Invalid book source position inventory.')
    texts, characters = [], 0
    for position in anchor['positions']:
        fields(position, {'ordinal', 'kind', 'index', 'start', 'end',
                          'text_sha256', 'text_verified_at_capture'})
        location = ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
            position['kind'], position['index'], position['text_sha256'], anchor['asset_sha256'])
        predicate = ('p.physical=?' if location.kind == 'physical_page'
                     else 'p.physical IS NULL AND p.ordinal=?')
        row = db.execute('''SELECT p.raw FROM revision r
            JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?
              AND p.ordinal=? AND p.digest=? AND length(CAST(p.raw AS BLOB))<=8000000
              AND ''' + predicate,
            (location.workspace, location.edition, location.revision, location.asset_sha256,
             position['ordinal'], location.text_sha256, location.index)).fetchone()
        if row is None:
            raise ValueError('Book-audio backup source no longer matches its exact sealed revision.')
        characters += len(row[0]) + (2 if texts else 0)
        if characters > 2_000_000:
            raise ValueError('Book-audio source exceeds its bounded reading snapshot.')
        texts.append(row[0])
    value = '\n\n'.join(texts)
    _validated_source_anchor(anchor, value)
    return value


def validate(db, name, value):
    if match := PRESET.fullmatch(name):
        from .audio.effect_presets import EffectPreset
        preset = EffectPreset.from_dict(value)
        if preset.id != match[1]:
            raise ValueError('Effect preset filename differs from its identity.')
    elif match := RECORDING.fullmatch(name):
        if match[2] != 'json':
            raise ValueError('A recording container requires streamed verification.')
        from .audio.recording import validate_recording_receipt
        validate_recording_receipt(value, match[1])
    elif match := BOOK.fullmatch(name):
        from .studio.book_audio import BookPlan
        fields(value, {'plan', 'fingerprint'})
        plan = BookPlan.from_dict(value['plan'])
        anchor = plan.source['source_anchor']
        if (plan.id != match[2] or plan.fingerprint != value['fingerprint']
                or sha256(anchor['workspace'].encode('utf-8')).hexdigest() != match[1]):
            raise ValueError('Book plan differs from its saved scope or fingerprint.')
        validate_source_snapshot(db, anchor)
    elif BOOK_SOURCE.fullmatch(name):
        fields(value, {'schema', 'plan', 'plan_sha256', 'source_anchor', 'source_metadata',
                       'source_topic', 'warnings', 'source_sha256', 'part', 'notice'})
        if value['schema'] != 'carr-book-audio-source/1' or not re.fullmatch(ID, value['plan']):
            raise ValueError('Invalid book-source receipt identity.')
        for key in ('plan_sha256', 'source_sha256'):
            digest(value[key])
        fields(value['part'], {'index', 'start', 'end', 'text_sha256'})
        number(value['part']['index'], 1, 256, integer=True)
        number(value['part']['start'], 0, 2_000_000, integer=True)
        number(value['part']['end'], value['part']['start'] + 1, 2_000_000, integer=True)
        digest(value['part']['text_sha256'])
        if value['notice'] != 'A literal reading derivative; this anchor never authorizes a quotation.':
            raise ValueError('Book-source receipt authority changed.')
        validate_source_snapshot(db, value['source_anchor'])
    elif name == INPUT_REVIEW:
        from .studio.backup_inputs import validate_input_review
        validate_input_review(value)
    elif name == INPUT_RECEIPT:
        fields(value, {'schema', 'projects', 'inputs', 'external_outputs'})
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['projects']) is not list or len(value['projects']) > 100000
                or type(value['inputs']) is not dict or len(value['inputs']) > 100000
                or value['external_outputs'] != 'not-copied; choose a new output directory after restore'):
            raise ValueError('Invalid Studio input backup receipt.')
        # Full manifest, project and byte bindings are checked together below.
    else:
        raise ValueError('Unknown Studio backup sidecar.')


def validate_collection(db, stage, entries, *, cancelled):
    from .durable_state import read_json
    from .studio.backup_inputs import validate_backup_inputs
    from .studio.book_audio import BookPlan
    from .audio.recording import RecordingOptions, RecordingService

    by_name = {entry['filename']: entry for entry in entries}
    state_entries = [dict(entry, filename=entry['filename'][6:])
                     for entry in entries if entry['filename'].startswith('state/')]
    validate_backup_inputs(Path(stage) / 'state', state_entries, cancelled=cancelled)
    plans, scopes = {}, {}
    presets = 0
    for name, entry in by_name.items():
        if cancelled():
            raise InterruptedError('Backup verification cancelled.')
        if match := INPUT.fullmatch(name):
            if entry['sha256'] != match[1]:
                raise ValueError('Retained Studio input differs from its content-addressed filename.')
        elif match := RECORDING.fullmatch(name):
            if match[2] != 'json':
                if name[:-4] + '.json' not in by_name:
                    raise ValueError('An unaccepted recording needs explicit recovery before backup.')
                continue
            value = read_json(Path(stage) / name)
            audio_name = str(PurePosixPath(name).with_suffix('.wav'))
            audio = by_name.get(audio_name)
            if (audio is None or audio['sha256'] != value['audio']['sha256']
                    or audio['size_bytes'] != value['audio']['size_bytes']):
                raise ValueError('Recording receipt does not bind its retained bytes.')
            if RecordingService._audio(Path(stage) / audio_name,
                    RecordingOptions(**value['limits']), cancelled) != value['audio']:
                raise ValueError('Recording PCM differs from its saved receipt.')
        elif PRESET.fullmatch(name):
            presets += 1
            if presets > 256:
                raise ValueError('Effect preset snapshot exceeds its collection limit.')
        elif match := BOOK.fullmatch(name):
            plan = BookPlan.from_dict(read_json(Path(stage) / name)['plan'])
            if plan.id in plans:
                raise ValueError('Book plan identities collide across scopes.')
            plans[plan.id] = plan
            scopes[match[1]] = scopes.get(match[1], 0) + 1
            if scopes[match[1]] > 4096:
                raise ValueError('Book plan snapshot exceeds its scope limit.')
    for plan in plans.values():
        for index in range(1, len(plan.parts) + 1):
            if cancelled():
                raise InterruptedError('Backup verification cancelled.')
            name = 'state/studio/jobs/' + plan.job_id(index) + '/job.json'
            if name not in by_name:
                # Preparing a batch can stop after the immutable plan is saved.
                continue
            job = read_json(Path(stage) / name)
            if (job['fingerprint'] != plan.project(index).fingerprint
                    or job['format'] != plan.format or job['stems'] is not False):
                raise ValueError('A retained Book item differs from its immutable reviewed plan.')
    for name in by_name:
        if match := BOOK_SOURCE.fullmatch(name):
            value = read_json(Path(stage) / name)
            plan = plans.get(value['plan'])
            index = value['part']['index']
            if (plan is None or index > len(plan.parts) or match[1] != plan.job_id(index)
                    or value != plan.provenance(index)):
                raise ValueError('Book-source receipt differs from its immutable reviewed plan.')
