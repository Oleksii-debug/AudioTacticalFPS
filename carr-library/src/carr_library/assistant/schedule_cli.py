"""Explicit foreground schedules for the current approved assistant journal."""
from argparse import ArgumentParser
from dataclasses import asdict
import json
from math import isfinite
from pathlib import Path
import signal
import sys
from threading import Event
from time import monotonic

from .schedule_model import MAX_LATENESS_SECONDS
from .scheduling import ScheduleManager, ScheduleSession


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument('--data-dir', type=Path, required=True)
    commands = value.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    create = commands.add_parser('create')
    create.add_argument('--job', required=True)
    create.add_argument('--plan-sha256', required=True)
    create.add_argument('--at', required=True, help='Complete future UTC timestamp, for example 2026-12-01T12:00:00Z.')
    create.add_argument('--lateness-seconds', type=int, default=3600,
                        help='Approved start window, from 1 to 86400 seconds.')
    create.add_argument('--previous-schedule-sha256',
                        help='Exact prior schedule pin, required only when replacing a never-started slot.')
    for name in ('disable', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--job', required=True)
        command.add_argument('--schedule-sha256', required=True)
        if name == 'run':
            command.add_argument('--run', action='store_true',
                help='Explicitly arm this one reviewed slot for this foreground invocation.')
            command.add_argument('--wait-seconds', type=float, default=0,
                help='Bounded foreground wait, 0 to 86400 seconds. No background service is installed.')
    return value


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.command == 'run':
        if not arguments.run:
            raise SystemExit('Use --run to arm this exact reviewed schedule explicitly.')
        if (not isfinite(arguments.wait_seconds)
                or not 0 <= arguments.wait_seconds <= MAX_LATENESS_SECONDS):
            raise SystemExit('Choose a finite foreground wait from 0 to 86400 seconds.')
    cancelled, session = Event(), None
    previous = None
    try:
        try:
            previous = signal.signal(signal.SIGINT, lambda *_: cancelled.set())
        except ValueError:
            pass
        from ..application import open_existing_library
        from .service import AssistantService
        from .production import LocalProduction
        app = open_existing_library(arguments.data_dir, cancelled=cancelled.is_set)
        service = AssistantService(app, production=LocalProduction(app.root, cancelled=cancelled.is_set))
        manager = ScheduleManager(service)
        if arguments.command == 'list':
            # Exact plans are included for explicit review; listing never arms.
            result = [{'schedule': asdict(view), 'plan': plan} for view, plan in manager.review(cancelled=cancelled.is_set)]
            code = 0
        elif arguments.command == 'create':
            result = asdict(manager.create(arguments.job, arguments.at,
                expected_plan_sha256=arguments.plan_sha256,
                expected_schedule_sha256=arguments.previous_schedule_sha256,
                lateness_seconds=arguments.lateness_seconds))
            code = 0
        elif arguments.command == 'disable':
            result = asdict(manager.disable(arguments.job,
                expected_schedule_sha256=arguments.schedule_sha256))
            code = 0
        else:
            session = ScheduleSession(manager)
            session.arm({arguments.job: arguments.schedule_sha256})
            deadline = monotonic() + arguments.wait_seconds
            report = None
            while not cancelled.is_set():
                # tick performs current root/plan/window checks inside the same
                # native job lock before its one durable dispatch attempt.
                report = session.tick(cancelled=cancelled.is_set,
                    progress=lambda done, total, phase: print(f'{done}/{total}: {phase}',
                                                              file=sys.stderr, flush=True))
                if report is not None:
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                cancelled.wait(min(.1, remaining))
            if cancelled.is_set() and report is None:
                raise InterruptedError('The foreground schedule was disarmed before dispatch.')
            if report is None:
                result = {'status': 'not_due', 'id': arguments.job,
                          'message': 'No job ran. This foreground invocation is now disarmed.'}
                code = 3
            else:
                result = asdict(report)
                code = (0 if report.status == 'complete' else
                        130 if report.status in ('paused', 'cancelled') else 1)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (KeyboardInterrupt, InterruptedError):
        print('Local schedule stopped. Saved plans remain available for explicit review or Resume.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as error:
        print('Local schedule: ' + str(error), file=sys.stderr)
        return 130 if cancelled.is_set() else 1
    finally:
        if session is not None:
            session.close()
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
