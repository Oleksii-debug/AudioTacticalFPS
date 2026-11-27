"""Reviewed assistant tasks bound to one parent window and data-root lifetime."""
import threading


class AssistantActions:
    """Worker callbacks inspect Python state; native controls stay on the UI thread."""
    def __init__(self, frame, *, service=None, production=None):
        from ..assistant import AssistantService
        from ..search.federated import _data_identity
        self.frame = frame
        self.library = frame.library
        self.identity = _data_identity(self.library)
        self.host_scope = tuple(frame.active) if frame.active is not None else None
        self.service = service or AssistantService(self.library, production=production)
        self.cancel_event = threading.Event()
        self.closed = False
        self.busy = False
        self._running_generation = None
        self.generation = 0
        self.plan = None
        self.approved_digest = None
        self.job_id = None

    def current(self, generation=None):
        from ..search.federated import _data_identity
        try:
            return (not self.closed and not getattr(self.frame, '_closing', False)
                    and self.frame.library is self.library
                    and _data_identity(self.library) == self.identity
                    and (tuple(self.frame.active) if self.frame.active is not None else None) == self.host_scope
                    and (generation is None or generation == self.generation))
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def ensure(self):
        if not self.current():
            self.cancel_event.set()
            raise ValueError('The parent library changed or closed. Open the assistant again.')

    def invalidate(self):
        self.generation += 1
        self.cancel_event.set()
        self.plan = self.approved_digest = self.job_id = None

    def accept(self, plan, *, job_id=None):
        from ..assistant import Plan
        self.ensure()
        if self.busy:
            raise RuntimeError('Wait for the current assistant operation to finish.')
        snapshot = Plan.from_dict(plan.to_dict())
        self.generation += 1
        self.plan, self.approved_digest, self.job_id = snapshot, snapshot.digest, job_id
        return snapshot

    def preview(self, instruction, scope):
        self.ensure()
        if self.busy:
            raise RuntimeError('Wait for the current assistant operation to finish.')
        return self.accept(self.service.preview(instruction, scope))

    def review_job(self, identity):
        self.ensure()
        return self.accept(self.service.review(identity), job_id=identity)

    def prepare_run(self, *, progress=None):
        self.ensure()
        if self.busy or self.plan is None or self.approved_digest != self.plan.digest:
            raise PermissionError('Preview the exact task before choosing Run.')
        identity = self.job_id
        if identity is None:
            identity = self.service.submit(self.plan, approved_plan_sha256=self.approved_digest)
            self.job_id = identity
        expected = self.approved_digest
        self.busy = True
        self.cancel_event.clear()
        self.generation += 1
        generation = self.generation
        self._running_generation = generation

        def stopped():
            if not self.current(generation):
                self.cancel_event.set()
            return self.cancel_event.is_set()

        def operation():
            if stopped():
                raise InterruptedError('Assistant task cancelled before execution.')
            return self.service.resume(identity, approved_plan_sha256=expected,
                                       cancelled=stopped, progress=progress)
        return generation, operation

    def start(self, executor, *, progress=None, set_running=lambda value: None):
        generation, operation = self.prepare_run(progress=(
            lambda *args: progress(generation, *args)) if progress is not None else None)
        try:
            set_running(True)
            self.ensure()
            future = executor.submit(operation)
        except Exception:
            self.finish(generation)
            set_running(False)
            raise
        return generation, future

    def finish(self, generation):
        if generation == self._running_generation:
            self.busy = False
            self._running_generation = None
        return self.current(generation)

    def close(self):
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
