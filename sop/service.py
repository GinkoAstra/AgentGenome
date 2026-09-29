"""Application composition. The CLI does not own a second execution loop."""
from .authoring import Library, prepare_method, validate_definition
from .capabilities import Registry
from .common import SopError
from .runtime import Runtime
from .store import Store


class Resolver:
    def __init__(self, library, registry, planner):
        self.library, self.registry, self.planner = library, registry, planner
        self.runtime = None

    def __call__(self, task, inputs, run, definition_hash=None):
        if definition_hash:
            definition = self.library.get(definition_hash)
            if definition is None:
                raise SopError('definition_missing', 'explicit method reference is unavailable')
            return {'definition': definition, 'origin': 'candidate_reuse', 'selection': []}
        outer = self
        class BudgetedPlanner:
            def plan(self, context):
                if outer.planner is None:
                    raise SopError('backend_unavailable', 'no planner configured for new method')
                outer.runtime._budget(run, 'model', getattr(outer.planner, 'max_model_calls_per_request', 1))
                outer.runtime._save(run, 'planner_dispatched', {'task': task, 'context': context})
                try:
                    return outer.planner.plan(context)
                finally:
                    evidence = getattr(outer.planner, 'last_evidence', None)
                    if evidence:
                        run['usage']['model_usage'].append(evidence)
        return prepare_method(task, inputs, self.library, self.registry, BudgetedPlanner())


def application(data_dir, *, library=None, agent=None, planner=None, registry=None, hook=None):
    registry = registry or Registry()
    store = Store(data_dir)
    resolver = Resolver(library or Library([]), registry, planner)
    runtime = Runtime(store, registry, validate_definition, resolver, agent=agent, hook=hook)
    resolver.runtime = runtime
    return runtime
