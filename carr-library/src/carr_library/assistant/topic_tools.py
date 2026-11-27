"""Scoped topic tools over the current research store and export publisher.

The pure checkpoint checks never open saved paths. Live execution uses the
existing application transaction, verified evidence renderer and atomic writer.
A reviewed job can create a topic once, or recover only its own published file.
"""
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from .models import canonical, text

TOOLS = frozenset(('topic.list', 'topic.create', 'topic.export'))
FORMATS = ('markdown', 'text', 'ris', 'bibtex', 'csv')
MAX_TOPICS = 10000
MAX_RESULT_BYTES = 512 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
_STAGE = re.compile(r'\.research-export-[a-z0-9_]{8}\Z')


def _identity(value):
    if (type(value) is not list or len(value) != 2
            or any(type(item) is not int or item < 0 for item in value) or value[1] == 0):
        raise ValueError('Invalid owned topic-export file identity.')
    return value


def _file_id(info):
    return _identity([info.st_dev, info.st_ino])


def _topic(value):
    text(value, 'topic identity', maximum=256)
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError('Invalid topic identity.')
    if value.startswith('@') and re.fullmatch(r'@(?:0|[1-9][0-9]*)', value) is None:
        raise ValueError('Use @0, @1, etc. for a preceding topic.create step.')
    return value


def _name(value):
    text(value, 'topic name', maximum=512)
    if any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ValueError('Invalid topic name.')
    return value


def _output_name(value):
    from ..research.export_file import _filename
    text(value, 'topic export name', maximum=1024)
    if '\\' in value or value.startswith('/') or ':' in value:
        raise ValueError('A topic export needs a relative local filename.')
    for part in value.split('/'):
        _filename(part)
    return value


def validate_arguments(step):
    args = step.arguments
    required, optional = {
        'topic.list': (set(), set()),
        'topic.create': ({'name'}, {'parent'}),
        'topic.export': ({'topic', 'name'}, {'format'}),
    }[step.tool]
    if set(args) - required - optional or not required <= set(args):
        raise ValueError('Invalid arguments for ' + step.tool + '.')
    if step.tool == 'topic.create':
        _name(args['name'])
        if args.get('parent') is not None:
            _topic(args['parent'])
    if step.tool == 'topic.export':
        _topic(args['topic'])
        _output_name(args['name'])
        if args.get('format', 'markdown') not in FORMATS:
            raise ValueError('Choose markdown, text, ris, bibtex or csv for a topic export.')


def _tool(step):
    return step.tool if hasattr(step, 'tool') else step['tool']


def validate_reference(step, steps, index):
    """A reference is a prior plan index, never a topic result supplied by data."""
    key = 'parent' if step.tool == 'topic.create' else 'topic'
    value = step.arguments.get(key) if step.tool in ('topic.create', 'topic.export') else None
    if value is not None and value.startswith('@'):
        prior = int(value[1:])
        if prior >= index or _tool(steps[prior]) != 'topic.create':
            raise ValueError('A topic reference must name a preceding topic.create step.')


def topic_identity(job_id, index):
    return uuid5(NAMESPACE_URL, 'carr-assistant:' + job_id + ':' + str(index) + ':topic').hex


def resolved_topic(value, job_id):
    return topic_identity(job_id, int(value[1:])) if value is not None and value.startswith('@') else value


def _saved_output(scope, name):
    from .validation import _path, _output_name as check_name
    check_name(name)
    if scope['output_root'] is None:
        raise ValueError('A saved topic export has no selected output directory.')
    return _path(scope['output_root']) / name


def _bounded_result(value, scope):
    if len(canonical(value).encode('utf-8')) > MAX_RESULT_BYTES:
        raise ValueError('The topic result exceeds its bounded checkpoint budget.')


def validate_checkpoint(step, scope, job_id, index, workspace, checkpoint):
    """Pure exact result/intent binding; safe for passive complete backups."""
    args, intent, result = step.arguments, checkpoint['intent'], checkpoint['result']
    expected_topic = resolved_topic(args.get('topic', args.get('parent')), job_id)
    prepared = intent.get('topic_publication')
    if step.tool != 'topic.export':
        if intent:
            raise ValueError('This topic tool cannot have an export intent.')
    elif 'topic_publication' in intent:
        if (type(prepared) is not dict or set(prepared) != {
                'schema', 'workspace', 'topic', 'format', 'name', 'size', 'sha256',
                'parent_identity', 'staging'}
                or prepared['schema'] != 'carr-topic-export/1'
                or not workspace or prepared['workspace'] != workspace
                or prepared['topic'] != expected_topic
                or prepared['format'] != args.get('format', 'markdown')
                or prepared['name'] != args['name']):
            raise ValueError('The topic export intent differs from its reviewed step.')
        if (type(prepared['size']) is not int or not 0 <= prepared['size'] <= min(MAX_EXPORT_BYTES, scope['max_bytes'])
                or not isinstance(prepared['sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', prepared['sha256']) is None):
            raise ValueError('Invalid topic export content manifest.')
        _identity(prepared['parent_identity'])
        stage = prepared['staging']
        if stage is not None:
            if (type(stage) is not dict or set(stage) != {'name', 'identity'}
                    or not isinstance(stage['name'], str) or _STAGE.fullmatch(stage['name']) is None):
                raise ValueError('Invalid owned topic export staging record.')
            _identity(stage['identity'])
            if stage['name'] == Path(args['name']).name:
                raise ValueError('The output cannot be its own staging file.')
    if result is None:
        return
    if type(result) is not dict or result.get('workspace') != workspace or not workspace:
        raise ValueError('A topic result must remain in its reviewed workspace.')
    _bounded_result(result, scope)
    if step.tool == 'topic.list':
        if set(result) != {'workspace', 'topics', 'artifacts'} or result['artifacts'] != []:
            raise ValueError('Invalid saved topic-list result.')
        rows = result['topics']
        if type(rows) is not list or len(rows) > min(MAX_TOPICS, scope['max_files']):
            raise ValueError('Invalid topic-list result count.')
        seen = set()
        for row in rows:
            if type(row) is not dict or set(row) != {'id', 'parent', 'name', 'position', 'archived'}:
                raise ValueError('Invalid saved topic row.')
            identity = _topic(row['id'])
            if identity.startswith('@') or identity in seen:
                raise ValueError('Repeated or indirect identity in saved topic rows.')
            seen.add(identity)
            if row['parent'] is not None and _topic(row['parent']).startswith('@'):
                raise ValueError('A saved topic parent must be an actual identity.')
            _name(row['name'])
            if type(row['position']) is not int or not 0 <= row['position'] <= 2**31 - 1 or row['archived'] is not False:
                raise ValueError('Invalid saved visible topic fields.')
    elif step.tool == 'topic.create':
        expected = {'workspace': workspace, 'topic_id': topic_identity(job_id, index),
                    'name': args['name'], 'parent': expected_topic, 'artifacts': []}
        if result != expected:
            raise ValueError('A created-topic result differs from its deterministic approved identity.')
    else:
        from .validation import _path
        expected_path = _saved_output(scope, args['name'])
        if (prepared is None or prepared['staging'] is None or set(result) != {
                'workspace', 'topic_id', 'format', 'output', 'size', 'sha256',
                'recovered', 'cleanup_warning', 'artifacts'}
                or result['topic_id'] != expected_topic
                or result['format'] != prepared['format']
                or _path(result['output']) != expected_path
                or result['size'] != prepared['size'] or type(result['size']) is not int
                or result['sha256'] != prepared['sha256']
                or type(result['recovered']) is not bool):
            raise ValueError('A topic-export result differs from its owned publication intent.')
        warning = result['cleanup_warning']
        if warning is not None:
            text(warning, 'export cleanup notice', maximum=1000)
        if result['artifacts'] != [{'path': result['output'], 'size': prepared['size'],
                                   'sha256': prepared['sha256']}]:
            raise ValueError('The topic export artifact differs from its content receipt.')


def validate_live(app, step, scope, workspace, steps, index):
    validate_reference(step, steps, index)
    value = step.arguments.get('parent' if step.tool == 'topic.create' else 'topic')
    if value is not None and not value.startswith('@'):
        if workspace == '<new>':
            raise ValueError('A new library has no existing topics. Reference an earlier topic.create step.')
        from ..research.topic_tree import TopicTree
        with app.read(workspace) as db:
            TopicTree(db, workspace)._visible(value)
    if step.tool == 'topic.export':
        from ..research.export_file import capture_export_destination
        capture_export_destination(scope.output(step.arguments['name']), private_root=app.root,
                                   require_absent=True)


def validate_plan_outputs(plan):
    """Reject aliases involving these new exports before any earlier mutation."""
    if not any(step.tool == 'topic.export' for step in plan.steps):
        return
    seen = set()
    for step in plan.steps:
        if step.tool not in ('topic.export', 'library.extract', 'library.backup', 'document.convert', 'audio.effects'):
            continue
        names = [step.arguments['name']]
        if step.tool in ('document.convert', 'audio.effects'):
            names.append(step.arguments['name'] + '.provenance.json')
        for name in names:
            key = str(plan.scope.output(name)).casefold()
            if key in seen:
                raise ValueError('Every planned output needs a distinct filename.')
            seen.add(key)


def validate_sources(db, workspace, tool, result):
    """Check catalog membership without reopening or activating external exports."""
    identities = [row['id'] for row in result['topics']] if tool == 'topic.list' else [result['topic_id']]
    for identity in identities:
        if not db.execute('SELECT 1 FROM research_topic WHERE workspace=? AND id=?',
                          (workspace, identity)).fetchone():
            raise ValueError('A saved assistant topic belongs to a different or unavailable workspace.')


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Topic operation stopped before publication.')


def _present(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _parent(target, private_root, expected):
    from ..audio.jobs import checked_directory
    parent = checked_directory(target.parent)
    if parent.is_relative_to(Path(private_root).resolve()):
        raise PermissionError('Topic exports must remain outside the private library.')
    actual = _file_id(parent.stat())
    if actual != expected:
        raise PermissionError('The approved topic export folder changed. Review a new plan.')
    return parent


def _content(path, prepared, identity, links, cancelled):
    from ..audio.jobs import file_identity, regular_file, verified_reader
    before = regular_file(path)
    if (_file_id(before) != identity or before.st_nlink != links
            or before.st_size != prepared['size']):
        raise ValueError('The owned topic export was replaced, linked or changed.')
    digest, size = sha256(), 0
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        while True:
            _check_cancelled(cancelled)
            block = stream.read(min(1024 * 1024, prepared['size'] - size + 1))
            if not block:
                break
            size += len(block)
            if size > prepared['size']:
                raise ValueError('The owned topic export grew beyond its manifest.')
            digest.update(block)
    after = regular_file(path)
    if (size != prepared['size'] or digest.hexdigest() != prepared['sha256']
            or after.st_nlink != links or file_identity(after) != file_identity(before)):
        raise ValueError('The owned topic export no longer matches its exact content.')
    return after


def _published(target, prepared, private_root, cancelled, *, cleanup):
    if prepared is None or prepared['staging'] is None:
        raise FileExistsError('This existing output has no owned publication checkpoint; it was not changed.')
    parent = _parent(target, private_root, prepared['parent_identity'])
    stage = parent / prepared['staging']['name']
    staged = _present(stage)
    links = 1 if staged is None else 2
    identity = prepared['staging']['identity']
    if staged is not None:
        _content(stage, prepared, identity, links, cancelled)
    _content(target, prepared, identity, links, cancelled)
    _parent(target, private_root, prepared['parent_identity'])
    warning = None
    if staged is not None and cleanup:
        # Only the recorded inode with exactly these two proven links is ours.
        _content(stage, prepared, identity, 2, cancelled)
        try:
            stage.unlink()
        except OSError:
            warning = ('The topic export is saved. Its owned temporary-file cleanup is still pending; '
                       'do not repeat the export.')
        else:
            from ..durable_state import sync_directory
            try:
                sync_directory(parent)
            except OSError:
                warning = ('The topic export is saved. Directory synchronization after temporary-file cleanup '
                           'could not be confirmed; do not repeat the export.')
    return warning


def _export_result(target, prepared, recovered, warning):
    return {'workspace': prepared['workspace'], 'topic_id': prepared['topic'],
            'format': prepared['format'], 'output': str(target),
            'size': prepared['size'], 'sha256': prepared['sha256'],
            'recovered': recovered, 'cleanup_warning': warning, 'artifacts': [str(target)]}


def _export(app, step, scope, record, checkpoint, persist, cancelled):
    from ..research.export_file import capture_export_destination, write_export
    from ..durable_state import sync_directory
    args = step.arguments
    topic = resolved_topic(args['topic'], record['id'])
    target = scope.output(args['name'])
    intent = checkpoint['intent']
    prepared = intent.get('topic_publication')
    if _present(target) is not None:
        warning = _published(target, prepared, app.root, cancelled, cleanup=True)
        return _export_result(target, prepared, True, warning)
    approval = capture_export_destination(target, private_root=app.root, cancelled=cancelled,
                                          require_absent=True)
    if prepared is not None:
        _parent(target, app.root, prepared['parent_identity'])
    _check_cancelled(cancelled)
    content = app.export_topic(record['workspace'], topic,
        format=args.get('format', 'markdown'), cancelled=cancelled)
    payload = content.encode('utf-8')
    size, digest = len(payload), sha256(payload).hexdigest()
    if size > min(MAX_EXPORT_BYTES, scope.max_bytes):
        raise ValueError('The topic export exceeds its selected byte budget or the 64 MiB export limit.')
    if prepared is None:
        prepared = {'schema': 'carr-topic-export/1', 'workspace': record['workspace'],
                    'topic': topic, 'format': args.get('format', 'markdown'), 'name': args['name'],
                    'size': size, 'sha256': digest,
                    'parent_identity': list(approval.parent_identity), 'staging': None}
        intent['topic_publication'] = prepared
        persist()
    elif (prepared['size'], prepared['sha256']) != (size, digest):
        raise ValueError('The verified evidence changed after export preparation. Review a new plan.')
    if prepared['staging'] is not None:
        previous = target.parent / prepared['staging']['name']
        if _present(previous) is not None:
            _parent(target, app.root, prepared['parent_identity'])
            _content(previous, prepared, prepared['staging']['identity'], 1, cancelled)
            _check_cancelled(cancelled)
            previous.unlink()
            sync_directory(target.parent)
        prepared['staging'] = None
        persist()

    def stage_ready(path, destination):
        if (destination.target != target or destination.state is not None
                or list(destination.parent_identity) != prepared['parent_identity']):
            raise PermissionError('The topic export destination changed before publication.')
        info = path.lstat()
        _content(path, prepared, _file_id(info), 1, cancelled)
        _parent(target, app.root, prepared['parent_identity'])
        prepared['staging'] = {'name': path.name, 'identity': _file_id(info)}
        sync_directory(target.parent)
        persist()  # A killed process leaves ownership durable before rename/link.

    try:
        receipt = write_export(target, content, private_root=app.root, cancelled=cancelled,
                               require_absent=True, approval=approval, prepared=stage_ready)
    except TimeoutError as exc:
        if cancelled():
            raise InterruptedError('Topic export stopped before publication.') from exc
        raise
    # Publication already completed. A late Stop must not turn success into a retry.
    warning = _published(target, prepared, app.root, lambda: False, cleanup=False)
    try:
        sync_directory(target.parent)
    except OSError:
        warning = 'The topic export is saved. Directory synchronization could not be confirmed; do not repeat the export.'
    notices = [item for item in (receipt.cleanup_warning, warning) if item]
    return _export_result(target, prepared, False, ' '.join(notices) or None)


def execute(app, step, scope, record, checkpoint, persist, cancelled, index):
    args, workspace = step.arguments, record['workspace']
    reference = args.get('parent' if step.tool == 'topic.create' else 'topic')
    if reference is not None and reference.startswith('@'):
        earlier = record['steps'][int(reference[1:])]
        if earlier['status'] != 'complete':
            raise ValueError('The referenced topic creation is not complete.')
    _check_cancelled(cancelled)
    if step.tool == 'topic.list':
        from ..research.topic_tree import TopicTree
        maximum = min(MAX_TOPICS, scope.max_files)
        try:
            with app._search_read(workspace, min(60.0, scope.max_seconds), cancelled) as (db, budget):
                rows = TopicTree(db, workspace).list_topics(limit=maximum + 1)
                budget.check()
        except TimeoutError as exc:
            if cancelled():
                raise InterruptedError('Topic listing stopped.') from exc
            raise
        if len(rows) > maximum:
            raise ValueError('Topic listing exceeds the selected file/result count budget.')
        result = {'workspace': workspace, 'topics': [asdict(row) for row in rows], 'artifacts': []}
        _bounded_result(result, scope.to_dict())
        return result
    if step.tool == 'topic.create':
        identity = topic_identity(record['id'], index)
        parent = resolved_topic(args.get('parent'), record['id'])
        with app.topic_transaction(workspace) as tree:
            found = tree.db.execute('SELECT name,parent,archived,position FROM research_topic '
                'WHERE workspace=? AND id=?', (workspace, identity)).fetchone()
            if found is None:
                tree.create(identity, args['name'], parent=parent)
            elif tuple(found) != (args['name'], parent, 0, 0):
                raise ValueError('The topic created by this job changed. Review a new plan.')
            tree._visible(identity)
            _check_cancelled(cancelled)
        return {'workspace': workspace, 'topic_id': identity, 'name': args['name'],
                'parent': parent, 'artifacts': []}
    try:
        return _export(app, step, scope, record, checkpoint, persist, cancelled)
    except TimeoutError as exc:
        if cancelled():
            raise InterruptedError('Topic export stopped before publication.') from exc
        raise


def verify_completed(app, step, scope, record, checkpoint, cancelled, index):
    result = checkpoint['result']
    if step.tool == 'topic.create':
        from ..research.topic_tree import TopicTree
        with app.read(record['workspace']) as db:
            tree = TopicTree(db, record['workspace'])
            tree._visible(result['topic_id'])
            row = db.execute('SELECT name,parent,archived,position FROM research_topic '
                'WHERE workspace=? AND id=?', (record['workspace'], result['topic_id'])).fetchone()
            if tuple(row) != (step.arguments['name'], result['parent'], 0, 0):
                raise ValueError('A completed topic changed. It was not recreated or replaced.')
    elif step.tool == 'topic.export':
        target = scope.output(step.arguments['name'])
        if _present(target) is None:
            raise ValueError('The completed topic export is missing. It was not recreated.')
        _published(target, checkpoint['intent']['topic_publication'], app.root, cancelled, cleanup=True)
