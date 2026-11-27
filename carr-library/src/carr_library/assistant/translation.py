"""Explicit text derivatives through the application's single provider gateway.

Only an in-memory grant from this service can send requests. Checkpoints contain
accepted text and conservative operation receipts, never a reusable approval or
key. A persisted sending marker blocks automatic retry after a crash or restore.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..providers import (BudgetLedger, JobApproval, ProfileStore, ProviderError,
    ProviderGateway, ProviderProfile, ProviderRequest, translation_result)
from ..reader.narration import (MAX_TEXT_BYTES, NarrationStore, decode_project,
    encode_project, new_project, revise)
from ..reader.reading_state import _location, _now, _timestamp, _unique
from .models import canonical, local_path, text
from .state import JobStore, atomic_json, read_json


DIRECTORY = 'state/translations'
MAX_BYTES = 64 * 1024 * 1024
MAX_LANGUAGE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_JOBS = 1000
CHUNK_CHARACTERS = 4500
_ID = re.compile(r'[0-9a-f]{32}')
_SHA = re.compile(r'[0-9a-f]{64}')


def _hash_text(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _hash(value):
    return _hash_text(canonical(value))


def _fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError('Unsupported translation checkpoint fields.')


def _checksum(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError('Invalid translation content digest.')


def _money(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError('Enter an explicit nonnegative estimate in millionths of the selected currency.')
    return value


def split_text(value, limit=CHUNK_CHARACTERS):
    text(value, 'selected translation text', maximum=2_000_000)
    if len(value.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('Select at most 8 MiB of text for one translation.')
    if type(limit) is not int or not 100 <= limit <= 5000:
        raise ValueError('Invalid translation chunk size.')
    pieces, start = [], 0
    while start < len(value):
        end = min(start + limit, len(value))
        if end < len(value):
            boundary = max(value.rfind('\n', start + limit // 2, end),
                           value.rfind(' ', start + limit // 2, end))
            if boundary >= 0:
                end = boundary + 1
        pieces.append(value[start:end])
        start = end
    return tuple(pieces)


def _request(plan, index):
    return ProviderRequest('translation', plan['chunks'][index],
        source_language=None if plan['source_language'] == 'auto' else plan['source_language'],
        target_language=plan['target_language'])


def _origin_project(origin):
    _fields(origin, ('project', 'project_sha256'))
    _checksum(origin['project_sha256'])
    project = decode_project((canonical(origin['project']) + '\n').encode('utf-8'))
    if sha256(encode_project(project)).hexdigest() != origin['project_sha256']:
        raise ValueError('The retained reading-copy snapshot changed.')
    return project


def validate_plan(value):
    _fields(value, ('workspace', 'topic', 'title', 'profile', 'profile_binding', 'source_text',
        'source_sha256', 'source_language', 'target_language', 'chunks', 'origin', 'source_location'))
    text(value['workspace'], 'translation library', maximum=128)
    if value['topic'] is not None:
        text(value['topic'], 'translation topic', maximum=128)
    text(value['title'], 'translation title', maximum=200)
    profile = ProviderProfile.from_dict(value['profile'])
    if (profile.provider != 'google-translate' or profile.credential_ref is not None
            or value['profile_binding'] != profile.credential_binding):
        raise ValueError('Invalid non-secret translation profile binding.')
    parts = split_text(value['source_text'])
    if (value['source_sha256'] != _hash_text(value['source_text'])
            or type(value['chunks']) is not list or tuple(value['chunks']) != parts):
        raise ValueError('The original translation text or exact chunk boundaries changed.')
    if value['source_language'] == value['target_language']:
        raise ValueError('Choose a target language different from the source.')
    # Validate language fields even when every chunk is whitespace and no
    # request will be made. Restored records must have the same strict schema.
    ProviderRequest('translation', 'Language validation',
        source_language=None if value['source_language'] == 'auto' else value['source_language'],
        target_language=value['target_language'])
    for index, chunk in enumerate(parts):
        if chunk.strip():
            _request(value, index)
    if value['source_location'] is not None:
        location = _location(value['source_location'])
        if location.workspace != value['workspace'] or location.text_sha256 != value['source_sha256']:
            raise ValueError('The exact source location does not match the selected translation text.')
    if value['origin'] is not None:
        project = _origin_project(value['origin'])
        if (project.current_text != value['source_text'] or value['source_location'] is not None
                or (project.source_location is not None and project.source_location.workspace != value['workspace'])):
            raise ValueError('The reading-copy source differs from the selected text or library.')
    return value


def _operation(value, record, index):
    _fields(value, ('index', 'operation_id', 'request_sha256', 'profile_sha256',
                    'approval_id', 'estimate_micros', 'created'))
    if (type(value['index']) is not int or value['index'] != index
            or value['operation_id'] != record['id'] + '_' + str(index)
            or not isinstance(value['approval_id'], str) or _ID.fullmatch(value['approval_id']) is None):
        raise ValueError('Invalid translation operation ownership.')
    _checksum(value['profile_sha256'])
    expected = _request(record['plan'], index).fingerprint(SimpleNamespace(fingerprint=value['profile_sha256']))
    if value['request_sha256'] != expected:
        raise ValueError('The translation operation does not match its exact chunk and languages.')
    _money(value['estimate_micros'])
    _timestamp(value['created'])


def validate_job(value, identity=None):
    _fields(value, ('schema', 'id', 'created', 'updated', 'plan', 'plan_sha256', 'status',
                    'receipts', 'pending', 'reading_project', 'quotation_authority'))
    if (value['schema'] != 'carr-translation-job/1' or not isinstance(value['id'], str)
            or _ID.fullmatch(value['id']) is None or (identity is not None and value['id'] != identity)
            or value['quotation_authority'] is not False):
        raise ValueError('Invalid translation job identity or derivative status.')
    _timestamp(value['created']); _timestamp(value['updated'])
    plan = validate_plan(value['plan'])
    if value['plan_sha256'] != _hash(plan):
        raise ValueError('The translation source plan changed.')
    if value['status'] not in ('prepared', 'running', 'paused', 'attention', 'complete'):
        raise ValueError('Invalid translation completion state.')
    rows = value['receipts']
    if type(rows) is not list or len(rows) > len(plan['chunks']):
        raise ValueError('Invalid accepted translation chunks.')
    size = 0
    for index, row in enumerate(rows):
        _fields(row, ('index', 'source_sha256', 'text', 'text_sha256', 'response_sha256',
                      'operation', 'detected_source', 'accepted_at'))
        chunk = plan['chunks'][index]
        if (type(row['index']) is not int or row['index'] != index or row['source_sha256'] != _hash_text(chunk)
                or not isinstance(row['text'], str) or '\x00' in row['text']
                or row['text_sha256'] != _hash_text(row['text'])):
            raise ValueError('An accepted translation chunk is damaged.')
        size += len(row['text'].encode('utf-8'))
        if size > MAX_TEXT_BYTES:
            raise ValueError('Translation exceeds the reading-copy text budget.')
        _timestamp(row['accepted_at'])
        if chunk.strip():
            _operation(row['operation'], value, index)
            _checksum(row['response_sha256'])
            # Reuse the wire validator for inert text and detected-language data.
            item = {'translatedText': row['text']}
            if row['detected_source'] is not None:
                item['detectedSourceLanguage'] = row['detected_source']
            translation_result(canonical({'data': {'translations': [item]}}).encode(), 'translation')
        elif (row['text'] != chunk or row['operation'] is not None
              or row['response_sha256'] is not None or row['detected_source'] is not None):
            raise ValueError('Whitespace-only chunks must be retained locally without a provider call.')
    if value['pending'] is not None:
        if len(rows) >= len(plan['chunks']) or not plan['chunks'][len(rows)].strip():
            raise ValueError('Invalid pending translation chunk.')
        _operation(value['pending'], value, len(rows))
        if value['status'] not in ('running', 'attention'):
            raise ValueError('A possibly transmitted chunk cannot be marked safe to resume.')
    if value['status'] == 'attention' and value['pending'] is None:
        raise ValueError('An uncertain translation needs its operation receipt.')
    if value['status'] == 'prepared' and (rows or value['pending'] is not None):
        raise ValueError('A prepared translation cannot contain transmitted chunks.')
    if value['status'] == 'complete' and (len(rows) != len(plan['chunks']) or value['pending'] is not None):
        raise ValueError('Incomplete translation cannot be published as complete.')
    if value['reading_project'] is not None:
        _fields(value['reading_project'], ('id', 'sha256'))
        if (value['status'] != 'complete' or value['reading_project']['id'] != _copy_id(value['id'])):
            raise ValueError('Invalid owned reading-copy result.')
        _checksum(value['reading_project']['sha256'])
    return value


def validate_languages(value, identity=None):
    _fields(value, ('schema', 'profile_id', 'profile_binding', 'fetched_at', 'display_language',
                    'languages', 'languages_sha256', 'qualification'))
    if (value['schema'] != 'carr-translation-languages/1'
            or not isinstance(value['profile_id'], str)
            or re.fullmatch('[a-zA-Z0-9_-]{1,100}', value['profile_id']) is None
            or (identity is not None and value['profile_id'] != identity)
            or value['qualification'] != 'Provider-declared; translation quality has not been independently verified.'):
        raise ValueError('Invalid translation-language inventory.')
    _checksum(value['profile_binding']); _timestamp(value['fetched_at'])
    ProviderRequest('translation_languages', '', target_language=value['display_language'])
    translation_result(canonical({'data': {'languages': value['languages']}}).encode(), 'translation_languages')
    if value['languages_sha256'] != _hash(value['languages']):
        raise ValueError('The saved language inventory changed.')
    return value


def validate_backup_file(name, raw):
    """Pure allowlist for the shared complete-backup registry; grants are absent."""
    prefix = DIRECTORY + '/'
    leaf = name[len(prefix):] if isinstance(name, str) and name.startswith(prefix) else name
    if not isinstance(leaf, str) or '/' in leaf or '\\' in leaf:
        raise ValueError('Invalid translation backup filename.')
    match = re.fullmatch(r'([0-9a-f]{32})\.json', leaf)
    language = re.fullmatch(r'languages-([a-zA-Z0-9_-]{1,100})\.json', leaf)
    maximum = MAX_LANGUAGE_BYTES if language else MAX_BYTES
    if (not match and not language) or not isinstance(raw, bytes) or not 0 < len(raw) <= maximum:
        raise ValueError('Invalid translation backup document.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite translation value.')))
    return validate_job(value, match[1]) if match else validate_languages(value, language[1])


def source_locations(record):
    """Historical source identities for common-backup source validation only."""
    if record.get('schema') != 'carr-translation-job/1':
        return ()
    plan = validate_job(record)['plan']
    location = plan['source_location']
    if plan['origin'] is not None:
        original = _origin_project(plan['origin']).source_location
        return () if original is None else (original,)
    return () if location is None else (_location(location),)


def _copy_id(identity):
    return uuid5(NAMESPACE_URL, 'carr:translation:' + identity).hex


@dataclass(frozen=True)
class TranslationReview:
    identity: str
    digest: str
    details: str

    @property
    def data(self):
        return json.loads(self.details)


@dataclass(frozen=True)
class _Grant:
    token: str
    review: TranslationReview
    approval: JobApproval | None


class TranslationService:
    def __init__(self, app, *, vault=None, transport=None):
        from ..search.federated import _data_identity
        self.app = self._app = app
        self.identity = _data_identity(app)
        self.root = local_path(app.root)
        self.store = JobStore(self.root / DIRECTORY)
        self.profiles = ProfileStore(self.root / 'api-connections.json', vault)
        self.transport = transport
        self._reviews, self._grants = {}, {}

    def _bound(self, workspace=None, topic=None, cancelled=lambda: False):
        from ..search.federated import _data_identity
        if self.app is not self._app or _data_identity(self.app) != self.identity:
            raise PermissionError('The translation data folder changed; reopen the operation.')
        local_path(self.store.directory)
        stopped = bool(cancelled())
        # Cancellation is caller code: it may also withdraw or replace the
        # current host. It cannot return False to retain the prior authority.
        if self.app is not self._app or _data_identity(self.app) != self.identity:
            raise PermissionError('The translation data folder changed; reopen the operation.')
        if stopped:
            raise InterruptedError('Translation stopped before the next request.')
        if workspace is not None:
            with self.app.read(workspace) as db:
                if topic is not None:
                    from ..research.topic_tree import TopicTree
                    TopicTree(db, workspace)._visible(topic)

    def _profile(self, identity):
        self._bound()
        profile = next((p for p in self.profiles.profiles() if p.id == identity), None)
        if profile is None or profile.provider != 'google-translate':
            raise ProviderError('Choose an existing Google Basic v2 NMT connection profile.')
        return profile

    def _gateway(self):
        self._bound()
        return ProviderGateway(BudgetLedger(self.root / 'api-operations.sqlite'),
                               vault=self.profiles.vault, transport=self.transport)

    def _load(self, identity):
        self._bound()
        value = validate_job(read_json(self.store.path(identity), maximum=MAX_BYTES), identity)
        self._bound(value['plan']['workspace'], value['plan']['topic'])
        return value

    def _write(self, value, *, expected=None):
        from ..ingest.writer_guard import exclusive_writer
        self._bound()
        with exclusive_writer(self.root, timeout=5):
            self._bound()
            path = self.store.path(value['id'])
            try:
                current = read_json(path, maximum=MAX_BYTES)
            except FileNotFoundError:
                current = None
            if (None if current is None else _hash(current)) != expected:
                raise ValueError('The translation checkpoint changed elsewhere; it was kept.')
            value['updated'] = _now()
            validate_job(value)
            if len(canonical(value).encode('utf-8')) + 1 > MAX_BYTES:
                raise ValueError('Translation checkpoint exceeds its storage budget.')
            atomic_json(path, value)
        return _hash(value)

    def languages(self, profile_id):
        profile = self._profile(profile_id)
        path = self.store.directory / ('languages-' + profile.id + '.json')
        if not path.exists():
            return ()
        value = validate_languages(read_json(path, maximum=MAX_LANGUAGE_BYTES), profile.id)
        if value['profile_binding'] != profile.credential_binding:
            raise ValueError('Refresh the language inventory for the selected profile and model.')
        return tuple(value['languages'])

    def refresh_languages(self, profile_id, *, approved_profile_fingerprint, display_language='en',
                          cancelled=lambda: False):
        profile = self._profile(profile_id)
        if approved_profile_fingerprint != profile.fingerprint:
            raise PermissionError('Explicitly approve the current profile before requesting its language inventory.')
        request = ProviderRequest('translation_languages', '', target_language=display_language)
        approved = JobApproval(uuid4().hex, profile.fingerprint, (request.fingerprint(profile),),
                               budget_micros=0, mode='APPROVED_REMOTE')
        self._bound(cancelled=cancelled)
        result = self._gateway().perform(profile, request, approval=approved, operation_id=uuid4().hex,
            estimated_micros=0, cancelled=lambda: self._cancelled(cancelled))
        rows = list(translation_result(result, 'translation_languages'))
        value = {'schema':'carr-translation-languages/1', 'profile_id':profile.id,
            'profile_binding':profile.credential_binding, 'fetched_at':_now(),
            'display_language':display_language, 'languages':rows, 'languages_sha256':_hash(rows),
            'qualification':'Provider-declared; translation quality has not been independently verified.'}
        validate_languages(value)
        if len(canonical(value).encode('utf-8')) + 1 > MAX_LANGUAGE_BYTES:
            raise ValueError('Translation-language inventory exceeds its storage budget.')
        self._bound()  # Retain an accepted result before observing late cancellation.
        from ..ingest.writer_guard import exclusive_writer
        with exclusive_writer(self.root, timeout=5):
            self._bound()
            atomic_json(self.store.directory / ('languages-' + profile.id + '.json'), value)
        return tuple(rows)

    def _cancelled(self, callback, plan=None):
        self._bound(None if plan is None else plan['workspace'], None if plan is None else plan['topic'])
        stopped = bool(callback())
        self._bound(None if plan is None else plan['workspace'], None if plan is None else plan['topic'])
        return stopped

    def prepare(self, workspace, profile_id, value, source, target, *, title='Machine translation',
                topic=None, source_location=None, origin=None, cancelled=lambda: False):
        self._bound(workspace, topic, cancelled)
        profile = self._profile(profile_id)
        codes = {row['language'] for row in self.languages(profile.id)}
        if target not in codes or (source != 'auto' and source not in codes):
            raise ValueError('Explicitly refresh and choose languages declared by this provider model.')
        public = asdict(profile); public['credential_ref'] = None
        public['capabilities'] = list(public['capabilities'])
        plan = dict(workspace=workspace, topic=topic, title=title, profile=public,
            profile_binding=profile.credential_binding, source_text=value, source_sha256=_hash_text(value),
            source_language=source, target_language=target, chunks=list(split_text(value)),
            origin=origin, source_location=asdict(source_location) if source_location is not None else None)
        validate_plan(plan)
        self._source(plan, cancelled)
        if len(self.store.identities()) >= MAX_JOBS:
            raise ValueError('Translation job history reached its bounded storage limit.')
        value = dict(schema='carr-translation-job/1', id=uuid4().hex, created=_now(), updated=_now(),
            plan=plan, plan_sha256=_hash(plan), status='prepared', receipts=[], pending=None,
            reading_project=None, quotation_authority=False)
        self._write(value)
        return value['id']

    def prepare_reading_copy(self, workspace, profile_id, project_id, expected_project_sha256,
                             source, target, *, title='Translated reading copy', topic=None, cancelled=lambda: False):
        self._bound(workspace, topic, cancelled)
        project, digest = NarrationStore(self.root).load(project_id)
        if digest != expected_project_sha256:
            raise ValueError('The saved reading copy changed; review it again before translation.')
        origin = {'project':json.loads(encode_project(project)), 'project_sha256':digest}
        return self.prepare(workspace, profile_id, project.current_text, source, target,
                            title=title, topic=topic, origin=origin, cancelled=cancelled)

    def _source(self, plan, cancelled=lambda: False):
        from ..reader.reading_desk import ReadingDesk
        self._bound(plan['workspace'], plan['topic'], cancelled)
        location = _location(plan['source_location']) if plan['source_location'] is not None else None
        original = plan['source_text']
        if plan['origin'] is not None:
            project = _origin_project(plan['origin'])
            location, original = project.source_location, project.original_text
        if location is not None:
            page = ReadingDesk(self.app, plan['workspace'], location.edition, topic=plan['topic'],
                cancelled=lambda: self._cancelled(cancelled, plan)).validate(location)
            if page.raw_text != original:
                raise ValueError('The retained source changed; no translation request was authorized.')

    def review(self, identity, *, budget_micros, per_call_micros, cancelled=lambda: False):
        value = self._load(identity)
        if value['pending'] is not None:
            raise ProviderError('A request may already have been charged. Its operation receipt must be reconciled; this job will not retry it.')
        plan = value['plan']; profile = self._profile(plan['profile']['id'])
        if profile.credential_binding != plan['profile_binding']:
            raise PermissionError('The saved provider/model binding changed. Prepare a new reviewed translation.')
        if profile.credential_ref is None:
            raise ProviderError('Reconnect the selected profile before approving an external translation.')
        self._source(plan, cancelled)
        _money(budget_micros); _money(per_call_micros)
        remaining = tuple(index for index in range(len(value['receipts']), len(plan['chunks']))
                          if plan['chunks'][index].strip())
        if per_call_micros * len(remaining) > budget_micros:
            raise PermissionError('The caller-entered per-call estimates exceed the approved remaining budget.')
        details = {'job_sha256':_hash(value), 'job_id':identity, 'profile_fingerprint':profile.fingerprint,
            'profile_name':profile.name, 'endpoint':profile.endpoint, 'model':profile.model,
            'workspace':plan['workspace'], 'title':plan['title'], 'source_text':plan['source_text'],
            'source_sha256':plan['source_sha256'], 'chunks_sha256':_hash(plan['chunks']),
            'chunk_lengths':[len(chunk) for chunk in plan['chunks']],
            'source_language':plan['source_language'], 'target_language':plan['target_language'],
            'completed_chunks':len(value['receipts']), 'total_chunks':len(plan['chunks']),
            'remaining_calls':len(remaining), 'request_hashes':[_request(plan, i).fingerprint(profile) for i in remaining],
            'budget_micros':budget_micros, 'per_call_micros':per_call_micros, 'currency':'USD',
            'estimate_basis':'Caller-entered reservation; actual provider billing is not measured.',
            'quotation_authority':False}
        encoded = canonical(details)
        review = TranslationReview(identity, _hash_text(encoded), encoded)
        self._reviews = {review.digest:review}  # One bounded, exact current review.
        self._grants.clear()
        return review

    def approve(self, review, *, approved_review_sha256, cancelled=lambda: False):
        if (type(review) is not TranslationReview or self._reviews.get(review.digest) is not review
                or approved_review_sha256 != review.digest):
            raise PermissionError('Review and approve this exact translation in the current session.')
        value = self._load(review.identity); data = review.data
        profile = self._profile(value['plan']['profile']['id'])
        if _hash(value) != data['job_sha256'] or profile.fingerprint != data['profile_fingerprint']:
            raise PermissionError('Translation text, progress, profile or budget changed; review again.')
        self._source(value['plan'], cancelled)
        approval = (JobApproval(uuid4().hex, profile.fingerprint, tuple(data['request_hashes']),
            budget_micros=data['budget_micros'], max_calls=data['remaining_calls'], mode='APPROVED_REMOTE')
            if data['remaining_calls'] else None)
        grant = _Grant(uuid4().hex, review, approval)
        self._grants = {grant.token:grant}
        return grant

    def run(self, identity, *, grant, cancelled=lambda: False, progress=lambda *args: None):
        if type(grant) is not _Grant or self._grants.pop(grant.token, None) is not grant or grant.review.identity != identity:
            raise PermissionError('A fresh exact-text approval is required after restart, restore or a completed run.')
        with self.store.lock(identity):
            value = self._load(identity); data = grant.review.data
            if _hash(value) != data['job_sha256'] or value['pending'] is not None:
                raise PermissionError('The saved translation changed; review it again.')
            plan = value['plan']; profile = self._profile(plan['profile']['id'])
            if profile.fingerprint != data['profile_fingerprint']:
                raise PermissionError('The approved profile changed before transmission.')
            lease = _hash(value)
            try:
                for index in range(len(value['receipts']), len(plan['chunks'])):
                    self._bound(plan['workspace'], plan['topic'], cancelled)
                    self._source(plan, cancelled)
                    fresh = self._profile(profile.id)
                    if fresh.fingerprint != profile.fingerprint:
                        raise PermissionError('The approved connection changed; no further request was sent.')
                    chunk, operation = plan['chunks'][index], None
                    response_hash, detected = None, None
                    if chunk.strip():
                        request = _request(plan, index)
                        operation = dict(index=index, operation_id=identity+'_'+str(index),
                            request_sha256=request.fingerprint(profile), profile_sha256=profile.fingerprint,
                            approval_id=grant.approval.job_id, estimate_micros=data['per_call_micros'], created=_now())
                        value['pending'], value['status'] = operation, 'running'
                        lease = self._write(value, expected=lease)
                        raw = self._gateway().perform(profile, request, approval=grant.approval,
                            operation_id=operation['operation_id'], estimated_micros=data['per_call_micros'],
                            cancelled=lambda: self._cancelled(cancelled, plan))
                        translated = translation_result(raw, 'translation')
                        output = translated['translatedText']; detected = translated.get('detectedSourceLanguage')
                        response_hash = sha256(raw).hexdigest()
                    else:
                        output = chunk
                    receipt = dict(index=index, source_sha256=_hash_text(chunk), text=output,
                        text_sha256=_hash_text(output), response_sha256=response_hash, operation=operation,
                        detected_source=detected, accepted_at=_now())
                    value['receipts'].append(receipt)
                    value['pending'], value['status'] = None, 'running'
                    # Persist accepted output before a late Cancel; it must never bill twice.
                    lease = self._write(value, expected=lease)
                    try:
                        progress(index+1, len(plan['chunks']), 'Accepted translation chunks')
                    except Exception:
                        pass  # Display failure cannot discard accepted provider output.
                value['status'] = 'complete'
                self._write(value, expected=lease)
            except BaseException as exc:
                # Reload the last durable boundary. A successful response whose
                # checkpoint failed still has a sending marker, hence no retry.
                saved = self._load(identity)
                saved_lease = _hash(saved)
                saved['status'] = 'attention' if saved['pending'] is not None else 'paused'
                try:
                    self._write(saved, expected=saved_lease)
                except Exception:
                    pass
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if saved['pending'] is not None:
                    raise ProviderError('Translation stopped with an uncertain operation. Accepted earlier chunks were kept; do not repeat this request automatically.') from None
                if isinstance(exc, InterruptedError):
                    return self.summary(identity)
                raise
            return self.summary(identity)

    def summary(self, identity):
        value = self._load(identity)
        return {'id':identity, 'status':value['status'], 'completed':len(value['receipts']),
            'total':len(value['plan']['chunks']), 'title':value['plan']['title'],
            'workspace':value['plan']['workspace'], 'quotation_authority':False}

    def details(self, identity):
        """Detached review data; opening it never approves or resumes a request."""
        return self._load(identity)

    def jobs(self, workspace, *, cancelled=lambda: False):
        self._bound(workspace, cancelled=cancelled)
        if len(self.store.identities()) > MAX_JOBS:
            raise ValueError('Translation history exceeds its storage budget.')
        rows = []
        for identity in self.store.identities():
            self._bound(workspace, cancelled=cancelled)
            value = validate_job(read_json(self.store.path(identity), maximum=MAX_BYTES), identity)
            if value['plan']['workspace'] == workspace:
                rows.append({'id':identity, 'title':value['plan']['title'], 'status':value['status'],
                    'completed':len(value['receipts']), 'total':len(value['plan']['chunks'])})
        return tuple(rows)

    def result(self, identity):
        value = self._load(identity)
        if value['status'] != 'complete':
            raise ValueError('Complete all translation chunks before publishing the derivative.')
        return value, ''.join(row['text'] for row in value['receipts'])

    def save_reading_copy(self, identity, *, cancelled=lambda: False):
        with self.store.lock(identity):
            value, translated = self.result(identity)
            self._source(value['plan'], cancelled)
            plan = value['plan']
            original, location = plan['source_text'], plan['source_location']
            if plan['origin'] is not None:
                source = _origin_project(plan['origin'])
                original = source.original_text
                location = asdict(source.source_location) if source.source_location is not None else None
            project = new_project(original, title=plan['title'],
                                  source_location=_location(location) if location is not None else None)
            project = revise(project, translated,
                label='Google NMT translation derivative; not quotation evidence; job '+identity)
            project = replace(project, id=_copy_id(identity), revisions=tuple(
                replace(row, created_at=value['created']) for row in project.revisions))
            expected = sha256(encode_project(project)).hexdigest()
            store = NarrationStore(self.root)
            try:
                existing, digest = store.load(project.id)
            except FileNotFoundError:
                digest = store.save(project)
            if digest != expected:
                raise ValueError('The owned reading copy changed; it was kept without overwriting.')
            lease = _hash(value)
            value['reading_project'] = {'id':project.id, 'sha256':digest}
            self._write(value, expected=lease)
            return project.id, digest

    def export(self, identity, destination, *, bilingual=False, cancelled=lambda: False):
        from ..research.export_file import write_export
        value, translated = self.result(identity)
        heading = ('Google Cloud Translation (NMT) machine translation.\n'
            'Reading derivative; not quotation evidence. Accuracy has not been independently reviewed.\n'
            + 'Job: '+identity+'; source language: '+value['plan']['source_language']
            + '; target language: '+value['plan']['target_language']+'\n\n')
        content = heading + translated
        if bilingual:
            content = (heading
                + '\n\n'.join('ORIGINAL\n'+chunk+'\nTRANSLATION\n'+row['text']
                    for chunk, row in zip(value['plan']['chunks'], value['receipts'])))
        self._bound(value['plan']['workspace'], value['plan']['topic'], cancelled)
        write_export(destination, content, private_root=self.root, require_absent=True,
                     cancelled=lambda: self._cancelled(cancelled, value['plan']))
        return {'path':str(Path(destination).absolute()), 'status':'complete', 'quotation_authority':False}
