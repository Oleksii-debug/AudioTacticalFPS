"""Explicit named-workflow settings commands; loading and rebinding never run jobs."""
import argparse
import json
from pathlib import Path

from ..research.export_file import capture_export_destination, export_completion, write_export
from .models import Scope
from .templates import MAX_TEMPLATE_BYTES, combine_instructions, draft_from_plan, read_json


def _draft_input(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--draft', type=Path, help='Typed JSON draft or plan; old scope is discarded')
    source.add_argument('--from-job', help='Copy only the typed steps of this saved job')


def run_templates(argv, service, *, cancelled=lambda: False):
    parser = argparse.ArgumentParser(prog='research-library-assistant --templates',
        description='Named inert workflows. No command executes an assistant plan.')
    actions = parser.add_subparsers(dest='command', required=True)
    actions.add_parser('list', help='List names and identities without loading permissions')
    load = actions.add_parser('load', help='Print an unapproved draft in the chosen identity order')
    load.add_argument('identities', nargs='+')
    save = actions.add_parser('save', help='Save steps under a new local identity')
    save.add_argument('name')
    _draft_input(save)
    update = actions.add_parser('update', help='Replace the reviewed stored draft using a revision check')
    update.add_argument('identity')
    update.add_argument('--name')
    _draft_input(update)
    rename = actions.add_parser('rename', help='Change only the selected workflow name')
    rename.add_argument('identity')
    rename.add_argument('name')
    remove = actions.add_parser('remove', help='Remove one named record; jobs and outputs remain')
    remove.add_argument('identity')
    importing = actions.add_parser('import', help='Validate a selected JSON record and give it a new identity')
    importing.add_argument('path', type=Path)
    importing.add_argument('--name')
    exporting = actions.add_parser('export', help='Write the exact reviewed inert record')
    exporting.add_argument('identity')
    exporting.add_argument('path', type=Path)
    exporting.add_argument('--replace-existing', action='store_true',
        help='Explicitly approve replacement of the exact currently selected destination')
    rebind = actions.add_parser('rebind', help='Preview stored steps with newly selected scope; never submit')
    rebind.add_argument('identities', nargs='+')
    rebind.add_argument('--workspace')
    rebind.add_argument('--input-root', action='append', type=Path, default=[])
    rebind.add_argument('--output-root', type=Path)
    rebind.add_argument('--allow-create', action='store_true')
    rebind.add_argument('--allow-full-backup', action='store_true')
    for name in ('bytes', 'files', 'steps'):
        rebind.add_argument('--max-' + name, type=int)
    rebind.add_argument('--max-seconds', type=float)
    args = parser.parse_args(argv)
    if cancelled():
        raise InterruptedError('Workflow settings stopped before the selected operation.')
    store = service.workflow_templates()

    def draft():
        if args.draft is not None:
            return draft_from_plan(read_json(args.draft, maximum=MAX_TEMPLATE_BYTES))
        return draft_from_plan(json.loads(service.review_instruction(args.from_job)))

    if args.command == 'list':
        return [{'id': row.id, 'name': row.name, 'revision': row.revision,
                 'steps': len(row.steps)} for row in store.list(cancelled=cancelled)]
    if args.command in ('load', 'rebind'):
        selected = []
        for identity in args.identities:
            if cancelled():
                raise InterruptedError('Workflow draft loading stopped.')
            selected.append(store.get(identity))
        instruction = combine_instructions(tuple(selected))
        if args.command == 'load':
            return json.loads(instruction)
        limits = {name: getattr(args, name) for name in
                  ('max_bytes', 'max_files', 'max_steps', 'max_seconds')
                  if getattr(args, name) is not None}
        scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
            allow_create=args.allow_create, allow_full_backup=args.allow_full_backup, **limits)
        if cancelled():
            raise InterruptedError('Workflow rebind stopped before preview.')
        plan = service.preview(instruction, scope)
        if cancelled():
            raise InterruptedError('Workflow rebind stopped after preview; nothing was submitted.')
        return plan.to_dict()
    if args.command == 'save':
        return store.save(args.name, draft(), cancelled=cancelled).to_dict()
    if args.command == 'import':
        return store.import_file(args.path, name=args.name, cancelled=cancelled).to_dict()
    previous = store.get(args.identity)
    if args.command == 'update':
        return store.save(previous.name if args.name is None else args.name, draft(),
                          previous=previous, cancelled=cancelled).to_dict()
    if args.command == 'rename':
        return store.save(args.name, previous.draft, previous=previous,
                          cancelled=cancelled).to_dict()
    if args.command == 'remove':
        store.remove(previous, cancelled=cancelled)
        return {'status': 'complete', 'removed_template': previous.id, 'name': previous.name}
    approval = capture_export_destination(args.path, private_root=service.app.root,
                                          cancelled=cancelled)
    if not approval.require_absent and not args.replace_existing:
        raise FileExistsError('The template export already exists. Choose a new filename or '
                              'explicitly use --replace-existing for this exact destination.')
    payload = store.export_json(previous)
    receipt = write_export(args.path, payload, private_root=service.app.root,
                           approval=approval, cancelled=cancelled)
    return {'status': 'complete', 'template_id': previous.id, 'path': str(args.path),
            'message': export_completion('Workflow template exported without scope or approval.', receipt)}
