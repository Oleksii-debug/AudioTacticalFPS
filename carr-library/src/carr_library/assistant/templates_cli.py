"""One named template catalogue; explicit capture and fresh previews, never execution."""
import argparse
import json
from pathlib import Path
import sys

from ..durable_state import read_json_snapshot
from ..installation_lease import installed_entrypoint
from .models import Scope, SCHEMA as PLAN_SCHEMA
from .templates import (MAX_BYTES, KINDS, TemplateStore, WorkflowTemplate,
                        instantiate_plan, review_request, _safe_step)


def _read(path, fallback=None):
    return fallback if path is None else read_json_snapshot(path, max_bytes=MAX_BYTES)[0]


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    show = commands.add_parser('show'); show.add_argument('id')
    save = commands.add_parser('save', help='Save fixed typed steps and optional parameter declarations')
    save.add_argument('--name', required=True)
    save.add_argument('--steps', type=Path, required=True)
    save.add_argument('--parameters', type=Path)
    save.add_argument('--kind', choices=KINDS, default='workflow')
    save.add_argument('--summary', default='')
    save.add_argument('--replace'); save.add_argument('--expected-catalog-sha256')
    for operation, field in (('from-plan', 'plan'), ('from-project', 'project')):
        child = commands.add_parser(operation)
        child.add_argument('--name', required=True)
        child.add_argument('--' + field, type=Path, required=True)
    project = commands.choices['from-project']
    project.add_argument('--format', default='wav')
    project.add_argument('--tool', choices=('audio.produce', 'audio.render'), default='audio.produce')
    project.add_argument('--no-stems', action='store_true')
    delete = commands.add_parser('delete')
    delete.add_argument('id'); delete.add_argument('--expected-catalog-sha256', required=True)
    for operation in ('request', 'preview'):
        child = commands.add_parser(operation)
        child.add_argument('id')
        child.add_argument('--values', type=Path)
        child.add_argument('--items', type=Path)
        if operation == 'preview':
            child.add_argument('--workspace')
            child.add_argument('--input-root', type=Path, action='append', default=[])
            child.add_argument('--output-root', type=Path)
            child.add_argument('--allow-create', action='store_true')
            child.add_argument('--allow-full-backup', action='store_true')
            child.add_argument('--max-steps', type=int, default=128)
    args = parser.parse_args(argv)
    production = None
    try:
        from ..application import LibraryApp
        app = LibraryApp(args.data_dir)
        store = TemplateStore(app.root)
        if args.command == 'list':
            records, token = store.list()
            result = {'catalog_sha256': token, 'templates': [item.to_dict() for item in records]}
        elif args.command == 'show':
            record, token = store.get(args.id)
            result = {'catalog_sha256': token, 'template': record.to_dict(), 'command_id': record.command_id}
        elif args.command == 'delete':
            store.get(args.id, expected_token=args.expected_catalog_sha256)
            token = store.delete(args.id, expected_token=args.expected_catalog_sha256)
            result = {'deleted': args.id, 'catalog_sha256': token, 'executed': False}
        elif args.command in ('save', 'from-plan', 'from-project'):
            records, token = store.list()
            if args.command == 'save':
                if bool(args.replace) != bool(args.expected_catalog_sha256):
                    raise ValueError('Replacement requires the exact currently reviewed catalogue SHA256.')
                if args.replace:
                    store.get(args.replace, expected_token=args.expected_catalog_sha256)
                    token = args.expected_catalog_sha256
                record = WorkflowTemplate.define(args.name, _read(args.steps),
                    _read(args.parameters, {}), kind=args.kind, summary=args.summary, identity=args.replace)
            elif args.command == 'from-plan':
                value = _read(args.plan)
                if (type(value) is not dict or set(value) - {'schema', 'scope', 'steps', 'summary'}
                        or value.get('schema', PLAN_SCHEMA) != PLAN_SCHEMA):
                    raise ValueError('Choose an existing typed assistant plan.')
                # Saved scope is deliberately not reopened or inherited.
                record = WorkflowTemplate.define(args.name, value.get('steps'),
                    summary=value.get('summary', ''))
                record = WorkflowTemplate.define(record.name,
                    [_safe_step(step).to_dict() for step in record.steps],
                    summary=record.summary, identity=record.id)
            else:
                from ..studio.project import Project
                record = WorkflowTemplate.from_project(args.name, Project.from_dict(_read(args.project)),
                    format=args.format, stems=not args.no_stems, tool=args.tool)
            token = store.save(record, expected_token=token)
            result = {'catalog_sha256': token, 'template': record.to_dict(),
                      'command_id': record.command_id, 'executed': False}
        else:
            record, token = store.get(args.id)
            values, items = _read(args.values, {}), _read(args.items)
            if args.command == 'request':
                result = review_request(record, values, items=items)
            else:
                from .service import AssistantService
                from ..studio.render import ProductionService
                scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                    allow_create=args.allow_create, allow_full_backup=args.allow_full_backup,
                    max_steps=args.max_steps)
                plan = instantiate_plan(record, scope, values, items=items)
                production = ProductionService(app.root / 'state/studio', input_roots=scope.input_roots)
                service = AssistantService(app, production=production.assistant_callback)
                result = service.validate(plan).to_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('Template operation interrupted; no job was run.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print('Workflow templates: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if production is not None:
            production.close()


if __name__ == '__main__':
    raise SystemExit(main())
