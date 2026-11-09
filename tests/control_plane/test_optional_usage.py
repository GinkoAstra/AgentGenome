"""Unknown CLI usage must not become a zero in API or trace projections."""
from node_runtime.control_plane.backends.base import Observation
from node_runtime.control_plane.storage import load
from node_runtime.control_plane.trace import TraceService
from helpers import operator, rid
from test_observation_activities import start_run, worker_of, controller, observe


def test_unknown_usage_masks_display_without_erasing_durable_totals(cluster):
    run_id = start_run(cluster, definition_id="optional-cli-usage")
    for sequence, calls in enumerate((5, None, 3), start=1):
        worker = worker_of(cluster, run_id)
        observation = Observation(
            observation_id=sequence, observed="idle", provider_calls=calls,
            tool_calls=2, activities_complete=True,
        )
        fact = cluster.controller._observation_fact(worker, observation)
        response = cluster.api.append_fact(controller(run_id, worker["epoch"]), fact,
                                           rid("usage"), worker["revision"])
        visible = cluster.api.get_resource(operator(), "worker", worker["worker_id"])
        expected = {"tool_calls": 2}
        if calls is not None:
            expected["provider_calls"] = 5
        assert visible["usage"] == expected
        assert response["usage"] == expected
        assert visible["usage_lower_bound"]["provider_calls"] == 5
        assert cluster.scheduler._usage_exhausted(
            {**visible, "budget": {"max_provider_calls": 5}}) == ["provider_calls"]
        snapshot = TraceService(cluster.api, service_id="test", backends={}).snapshot(
            operator(), run_id)
        assert snapshot["workers"][0]["usage"] == expected
        with cluster.api.store.read() as tx:
            row = tx.one("SELECT usage_json FROM workers WHERE worker_id = ?",
                         (worker["worker_id"],))
            assert load(row["usage_json"])["provider_calls"] == 5


def test_unobserved_worker_does_not_claim_known_zero_usage(cluster):
    run_id = start_run(cluster, definition_id="unobserved-cli-usage")
    worker = worker_of(cluster, run_id)
    assert worker["usage"] == {}
    fact = cluster.controller._observation_fact(worker, Observation(
        observation_id=1, observed="idle", provider_calls=None, tool_calls=None,
    ))
    assert fact["usage"] == {}
    # The complete zero counts from existing adapters remain explicit evidence.
    known = cluster.controller._observation_fact(worker, Observation(
        observation_id=2, observed="idle",
    ))
    assert known["usage"] == {"provider_calls": 0, "tool_calls": 0}


def test_new_epoch_execution_keeps_the_committed_usage_lower_bound(cluster):
    cluster.backend.agent = lambda spec, message, state: None
    run_id = start_run(cluster, definition_id="usage-across-epochs")
    worker = worker_of(cluster, run_id)
    cluster.controller.reconcile(run_id, worker["worker_id"])
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[], complete=True,
            usage={"provider_calls": 5, "tool_calls": 2})
    current = worker_of(cluster, run_id)
    cluster.scheduler._plan(
        run_id, [("worker", current["worker_id"], current["revision"])],
        [{"kind": "worker", "op": "update", "id": current["worker_id"],
          "fields": {"epoch": current["epoch"] + 1}}], "usage-epoch")
    replaced = worker_of(cluster, run_id)
    assert replaced["usage"] == {}
    spec = cluster.controller._spec(replaced, cluster.state(run_id)["run"])
    assert spec.usage == {"provider_calls": 5, "tool_calls": 2}
    assert cluster.scheduler._usage_exhausted(
        {**replaced, "budget": {"max_provider_calls": 5}}) == ["provider_calls"]
