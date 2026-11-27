"""Named workflow settings and explicit previews; execution uses existing CLIs."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from carr_library.installation_lease import installed_entrypoint
import sys

from .models import Plan, Scope
from .templates import (KINDS, MAX_TEMPLATE_BYTES, TemplateStore, definition_from_plan,
                        definition_from_project, instantiate_plan, review_request)
from ..durable_state import read_json


def _read(path, fallback):
    return fallback if path is None else read_json(path, maximum=MAX_TEMPLATE_BYTES)


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        'Save named typed workflow recipes. No command here runs a job; preview, then use the existing assistant/documents/Studio owner.'))
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    show = commands.add_parser('show'); show.add_argument('id')
    save = commands.add_parser('save')
    save.add_argument('--name', required=True); save.add_argument('--kind', choices=KINDS, required=True)
    save.add_argument('--definition', type=Path, required=True); save.add_argument('--parameters', type=Path)
    save.add_argument('--replace'); save.add_argument('--expected-sha256')
    for action, field in (('from-plan', 'plan'), ('from-project', 'project')):
        sub = commands.add_parser(action)
        sub.add_argument('--name', required=True); sub.add_argument('--' + field, type=Path, required=True)
    saved = commands.add_parser('from-job')
    saved.add_argument('--name', required=True); saved.add_argument('id')
    for action in ('request', 'preview', 'batch-preset'):
        sub = commands.add_parser(action)
        sub.add_argument('id'); sub.add_argument('--values', type=Path); sub.add_argument('--items', type=Path)
        if action == 'preview':
            sub.add_argument('--workspace'); sub.add_argument('--input-root', type=Path, action='append', default=[])
            sub.add_argument('--output-root', type=Path)
            sub.add_argument('--allow-create', action='store_true')
            sub.add_argument('--allow-full-backup', action='store_true')
    args = parser.parse_args(argv)
    production = None
    try:
        from ..application import LibraryApp
        from ..studio.cli import data_directory
        app = LibraryApp(data_directory(args.data_dir))
        store = TemplateStore(app.root)
        if args.command == 'list':
            result = [item.to_dict() for item in store.list()]
        elif args.command == 'show':
            result = store.get(args.id).to_dict()
        elif args.command == 'save':
            previous = None
            if bool(args.replace) != bool(args.expected_sha256):
                raise ValueError('Replacing a template requires its exact reviewed SHA256.')
            if args.replace:
                previous = store.get(args.replace)
                if previous.fingerprint != args.expected_sha256:
                    raise ValueError('Template changed after review.')
            result = store.save(args.name, args.kind, _read(args.definition, None),
                _read(args.parameters, {}), previous=previous).to_dict()
        elif args.command == 'from-plan':
            plan = Plan.from_dict(_read(args.plan, None))
            result = store.save(args.name, 'assistant', definition_from_plan(plan)).to_dict()
        elif args.command == 'from-job':
            from .service import AssistantService
            _record, plan = AssistantService(app)._load(args.id)
            result = store.save(args.name, 'assistant', definition_from_plan(plan)).to_dict()
        elif args.command == 'from-project':
            from ..studio.project import Project
            project = Project.from_dict(_read(args.project, None))
            result = store.save(args.name, 'project', definition_from_project(project)).to_dict()
        else:
            template = store.get(args.id)
            values, items = _read(args.values, {}), _read(args.items, None)
            request = review_request(template, values, items=items)
            if args.command == 'request':
                if template.kind == 'document-batch':
                    raise ValueError('Use batch-preset for settings opened in the existing documents review.')
                result = request
            elif args.command == 'batch-preset':
                if template.kind != 'document-batch':
                    raise ValueError('Choose a document-batch preset.')
                result = [template.resolve({**request['values'], **row})
                          for row in ([{}] if request['items'] is None else request['items'])]
            else:
                from ..studio.render import ProductionService
                from .service import AssistantService
                scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                              allow_create=args.allow_create, allow_full_backup=args.allow_full_backup)
                production = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
                service = AssistantService(app, production=production.assistant_callback)
                result = service.preview(json.dumps(request, ensure_ascii=False), scope).to_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if store.warning:
            print(store.warning, file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print('Workflow operation interrupted; no workflow execution was requested.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print('Workflow templates: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        from ..studio.cli import _close_production
        _close_production(production)


if __name__ == '__main__':
    raise SystemExit(main())
