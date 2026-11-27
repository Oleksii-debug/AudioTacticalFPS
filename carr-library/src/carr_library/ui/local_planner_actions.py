"""Owned optional model-drafting work inside the existing assistant dialog."""
from ..assistant.local_planner import LocalPlanner


class LocalPlanningActions:
    def __init__(self, parent_actions, *, planner=None):
        self.parent = parent_actions
        self.planner = planner or LocalPlanner(parent_actions.library, assistant=parent_actions.service)
        if self.planner.app is not parent_actions.library:
            raise PermissionError('The local planner must belong to this assistant library.')
        self.review = None
        self.parent_generation = None
        self.generation = 0
        self.running_generation = None
        self.closed = self.busy = False

    def current(self, generation=None):
        return (not self.closed and self.parent.current(self.parent_generation)
                and (generation is None or generation == self.generation))

    def invalidate(self):
        self.review = None
        self.generation += 1
        self.parent.cancel_event.set()
        self.planner.clear()

    def prepare(self, goal, scope, profile_id):
        self.parent.ensure()
        if self.closed or self.busy or self.parent.busy:
            raise RuntimeError('Wait for the current assistant operation before reviewing another model request.')
        self.invalidate()
        review = self.planner.prepare(goal, scope, profile_id)
        self.parent.ensure()
        self.parent_generation = self.parent.generation
        self.review = review
        return review

    def start(self, executor, *, set_running=lambda value:None, progress=None):
        self.parent.ensure()
        if self.busy or self.parent.busy or self.review is None or not self.current():
            raise PermissionError('Prepare and inspect the exact local-model request before approving it.')
        selected = self.review
        self.review = None
        self.generation += 1
        generation = self.running_generation = self.generation
        self.busy = True
        self.parent.cancel_event.clear()
        def stopped():
            if not self.current(generation):
                self.parent.cancel_event.set()
            return self.parent.cancel_event.is_set()
        def work():
            if stopped():
                raise InterruptedError('The local-model request was withdrawn before execution.')
            if progress is not None:
                progress(generation, None, None, 'Requesting one explicitly approved local-model proposal')
            return self.planner.draft(selected, approved_review_sha256=selected.digest, cancelled=stopped)
        try:
            set_running(True)
            if not self.current(generation):
                raise InterruptedError('The local-model request changed before worker submission.')
            future = executor.submit(work)
        except Exception:
            self.finish(generation)
            set_running(False)
            raise
        return generation, future

    def finish(self, generation):
        if self.running_generation == generation:
            self.running_generation = None
            self.busy = False
        return self.current(generation) and not self.parent.cancel_event.is_set()

    def close(self):
        self.closed = True
        self.invalidate()
