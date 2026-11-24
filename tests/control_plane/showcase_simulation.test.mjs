import test from "node:test";
import assert from "node:assert/strict";
import {NODE_ORDER, RunModel, SCENARIOS} from "../../docs/showcase/simulation.mjs";

for (const scenario of Object.keys(SCENARIOS)) {
  test(`${scenario}: reaches accepted delivery through the real tour sequence`, () => {
    const model = new RunModel(scenario);
    assert.equal(model.snapshot().status, "ready");
    assert.equal(model.snapshot().cursor, 0);
    assert.ok(NODE_ORDER.every((node) => model.snapshot().nodes[node] === "idle"));

    while (!model.complete) model.step();

    const result = model.snapshot();
    assert.equal(result.status, "accepted");
    assert.equal(result.cursor, result.total);
    assert.ok(NODE_ORDER.every((node) => result.nodes[node] === "accepted"));
    assert.equal(result.currentEvent.kind, "delivery");
    assert.match(result.currentEvent.artifact, /"duplicate_rows": 1/);
    assert.match(result.currentEvent.artifact, /"example\.com": 3/);

    const unchanged = model.step();
    assert.deepEqual(unchanged, result);
  });
}

test("revision scenario preserves rejection and submits a second candidate round", () => {
  const model = new RunModel("revision");
  while (!model.complete) model.step();
  const events = model.snapshot().visibleEvents;
  const revise = events.find((event) => event.kind === "revise");
  const candidates = events.filter((event) => event.kind === "candidate" && event.node === "clean");

  assert.equal(candidates.length, 2);
  assert.match(candidates[0].artifact, /"duplicate_rows": 0/);
  assert.match(candidates[1].artifact, /"duplicate_rows": 1/);
  assert.match(revise.artifact, /expected 1, got 0/);
  assert.ok(events.indexOf(revise) < events.indexOf(candidates[1]));
});

test("timeline seek reconstructs node state instead of replaying side effects", () => {
  const model = new RunModel("revision");
  const reviseIndex = model.events.findIndex((event) => event.kind === "revise") + 1;
  model.seek(reviseIndex);

  assert.equal(model.snapshot().cursor, reviseIndex);
  assert.equal(model.snapshot().currentEvent.kind, "revise");
  assert.equal(model.snapshot().nodes.clean, "revise");
  assert.equal(model.snapshot().nodes["clean-review"], "revise");
  assert.equal(model.snapshot().nodes.profile, "idle");

  model.seek(0);
  assert.ok(NODE_ORDER.every((node) => model.snapshot().nodes[node] === "idle"));
  assert.throws(() => model.seek(-1), RangeError);
  assert.throws(() => model.seek(model.events.length + 1), RangeError);
});

test("reset and scenario switch do not mutate shared event definitions", () => {
  const first = new RunModel("happy");
  while (!first.complete) first.step();
  const second = new RunModel("happy");

  assert.equal(second.snapshot().cursor, 0);
  assert.deepEqual(second.events, SCENARIOS.happy.events);
  assert.throws(() => second.reset("missing"), /Unknown scenario/);
  second.reset("revision");
  assert.equal(second.snapshot().total, SCENARIOS.revision.events.length);
});
