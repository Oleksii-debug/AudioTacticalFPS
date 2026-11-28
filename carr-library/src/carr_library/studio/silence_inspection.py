"""Read-only long-silence inspection of an existing completed Studio master.

The saved receipt is checked before and after measurement under the same
existing render lock and shared resource lease. No state, node, completion
receipt, original or published output is written by this operation.
"""
from __future__ import annotations

from pathlib import Path
import time
from types import SimpleNamespace

from ..audio.cleanup import retained_context, retained_state_lock
from ..audio.models import AudioBudget, AudioCancelled, AudioError
from ..audio.recording import _file_identity, _verified_reader
from ..audio.silence import SilenceCriteria, runtime_for_inspection
from ..backup_producers import _studio_job
from ..durable_state import encode, local_path, read_json, read_json_snapshot, regular
from .project import Project, _id


def inspect_job(service, job_id, *, threshold_dbfs, minimum_seconds,
                cancelled=lambda: False, progress=None, source_guard=None, budget=None,
                runtime_binding=None):
    started = time.monotonic()
    criteria = SilenceCriteria(threshold_dbfs, minimum_seconds)
    budget = AudioBudget() if budget is None else budget
    if not isinstance(budget, AudioBudget):
        raise TypeError('Silence inspection requires an AudioBudget.')
    deadline = started + budget.timeout
    service._check_open()
    identity = _id(job_id)
    if source_guard is not None and not callable(source_guard):
        raise ValueError('The source guard must be callable.')
    if runtime_binding is not None and not callable(runtime_binding):
        raise ValueError('The selected runtime binding callback must be callable.')
    job_root = service.jobs_root / identity
    job_file = job_root / 'job.json'

    def early_check():
        service._check_open()
        if cancelled():
            raise AudioCancelled('Silence inspection cancelled.')
        return False

    early_check()
    with retained_state_lock(job_root, 'render'):
        early_check()
        saved_stamp = _file_identity(regular(job_file))
        state, saved_digest = read_json_snapshot(job_file)
        if _file_identity(regular(job_file)) != saved_stamp:
            raise ValueError('The saved production job changed before silence inspection.')
        _studio_job(state, identity)  # Pure typed validation; no output or engine access.
        if state.get('restore_requires_new_output', False):
            raise ValueError('This restored production job requires a newly reviewed output before silence inspection.')
        if state['status'] != 'complete':
            raise ValueError('Choose a completed production job for silence inspection.')
        project = Project.from_dict(state['project'])
        bound_source = any(line.source_ref.startswith('carr-book-audio:')
                           for scene in project.scenes for line in scene.lines)
        if bound_source and source_guard is None:
            raise ValueError('This source-bound Book job requires its original source guard for silence inspection; '
                             'the general Studio inspection entry cannot supply that authority.')
        source_provenance = None

        def check_source():
            nonlocal source_provenance
            early_check()
            if source_guard is not None:
                value = source_guard()
                if bound_source:
                    from .book_source import validate_project_provenance
                    validate_project_provenance(value, project)
                    current = encode(value)
                    if source_provenance is not None and current != source_provenance:
                        raise ValueError('The reviewed Book source provenance changed during silence inspection.')
                    source_provenance = current

        # This authorizes only the exact completed destination declared by the
        # current job. Restored/non-complete/source-bound refusals precede all
        # external path resolution and audio-runtime construction.
        check_source()
        destination = Path(state['destination'])
        matches = [item for item in state['artifacts']
                   if Path(item['path']) == destination / 'master.wav']
        if len(matches) != 1:
            raise ValueError('The completed job needs one exact master PCM receipt.')
        master = matches[0]

        def admission_cancelled():
            early_check()
            if time.monotonic() >= deadline:
                raise AudioError('Silence inspection exceeded its total time budget.')
            return False

        # Four raw bytes per input byte bounds every admitted integer PCM
        # width, including 8-bit and RF64. A decode exceeding the runtime's
        # own byte budget is refused before launch, so reserve that smaller
        # accepted maximum plus bounded container/directory overhead.
        raw_bound = min(budget.max_output_bytes, master['size'] * 4)
        reservation = raw_bound + 65536
        admission_cancelled()
        with service.audio_work(SimpleNamespace(budget=budget), cancelled=admission_cancelled,
                                disk_bytes=reservation, disk_path=service.cache) as lease:
            def check():
                admission_cancelled()
                lease.checkpoint()
                return False

            def unchanged_job():
                check()
                current, digest = read_json_snapshot(job_file)
                if (digest != saved_digest or current != state
                        or _file_identity(regular(job_file)) != saved_stamp):
                    raise ValueError('The saved production job changed during silence inspection.')
                check()

            def phase(message):
                check()
                if progress is not None:
                    progress(message)
                check()

            unchanged_job()
            check_source()
            phase('Validating the selected PCM tools under the shared reservation.')
            if runtime_binding is not None:
                # Headless callers enter their existing retained selected-runtime
                # context here, after admission, and own its ordered final close.
                runtime_binding(cancelled=check)
                check()
            runtime = runtime_for_inspection(service.cache, state_root=service.root.parent,
                input_roots=(service.cache, *service.input_roots, destination),
                budget=budget, cancelled=check, runtime_factory=service.runtime_factory)
            # A custom runtime may be more restrictive, never more permissive.
            # Keep the original operation start; runtime construction cannot
            # create a new deadline after waiting for resources.
            deadline = min(deadline, started + runtime.budget.timeout)
            check()
            phase('Verifying the completed production artifact receipts.')
            service._verify_artifacts(state, cancelled=check)
            root = local_path(destination, exists=True)
            path = local_path(master['path'], root=root, exists=True)
            with retained_context(_verified_reader(path)):
                if bound_source and encode(read_json(root / 'book-source.json')) != source_provenance:
                    raise ValueError('The published Book source differs from its current reviewed plan.')
                result = runtime.inspect_silence(path,
                    threshold_dbfs=criteria.threshold_dbfs,
                    minimum_seconds=criteria.minimum_seconds,
                    cancelled=check, progress=phase, deadline=deadline)
                if (result['source']['sha256'] != master['sha256']
                        or result['source']['size'] != master['size']):
                    raise ValueError('The measured PCM differs from the completed master receipt.')
                phase('Rechecking the completed artifacts and saved job after measurement.')
                service._verify_artifacts(state, cancelled=check)
                check_source()
                if bound_source and encode(read_json(root / 'book-source.json')) != source_provenance:
                    raise ValueError('The published Book source changed during silence inspection.')
                unchanged_job()
                check()
                result.update(job_id=identity, project_fingerprint=state['fingerprint'],
                              operation_sha256=state.get('operation_sha256'))
    admission_cancelled()
    return result
