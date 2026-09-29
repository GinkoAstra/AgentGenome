// Real installed Pi loop and worker, scripted model transport. No network calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { findPiInstallation, loadPi, runWorker, SUPPORTED_PI_VERSIONS } from '../../sop/pi_worker.mjs';
const pi = await loadPi();
const model = { id: 'protocol-fixture', name: 'Scripted protocol transport', api: 'probe', provider: 'probe',
  baseUrl: '', reasoning: false, input: ['text'], contextWindow: 100000, maxTokens: 4096,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
const request = { mode: 'agent', context: { task: 'orders.clean', inputs: { rule: null },
  information_query: 'context_only', contract: { checks: ['orders.clean'] } },
  config: { timeout_ms: 5000, max_turns: 3, max_tokens: 1024 } };
function transport(steps) {
  let index = 0;
  const calls = [];
  const streamFn = (_, context) => {
    // 0.87.1 carries system prompts/tool declarations in the transcript.
    // Inspect their public replay view without weakening any scope assertion.
    const declaredTools = context.tools ?? pi.ai.getCurrentTools(context.messages);
    const systemPrompt = context.systemPrompt ?? pi.ai.getCurrentSystemPrompt(context.messages);
    calls.push(structuredClone({ tools: declaredTools.map(tool => tool.name),
      messages: context.messages.filter(message => message.role !== 'system'),
      raw_messages: context.messages, systemPrompt }));
    const step = steps[index++];
    const content = typeof step === 'string' ? [{ type: 'text', text: step }]
      : (step || []).map((call, callIndex) => ({ type: 'toolCall', id: `${index}-${callIndex}`, name: call.name, arguments: call.args }));
    const message = { role: 'assistant', content, api: model.api, provider: model.provider, model: model.id,
      stopReason: typeof step === 'string' ? 'stop' : 'toolUse', timestamp: Date.now(),
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
    const stream = pi.ai.createAssistantMessageEventStream();
    stream.push({ type: 'done', reason: message.stopReason, message }); stream.end();
    return stream;
  };
  return { model, streamFn, calls };
}
const handoff = value => ({ name: 'handoff', args: { payload: JSON.stringify(value) } });
const needInfo = { kind: 'need_info', field: 'rule', reason: 'Business rule is absent' };

test('real Pi loop hands off D04 and only sees the restricted tools', async () => {
  const fixture = transport([[{ name: 'context', args: {} }], [handoff(needInfo)]]);
  const result = await runWorker(request, fixture);
  assert.deepEqual(result.result, needInfo);
  assert.equal(result.evidence.model_calls, 2);
  assert.equal(result.evidence.transport, 'scripted_protocol_test');
  for (const call of fixture.calls) {
    assert.deepEqual(call.tools, ['context', 'handoff']);
    assert.match(call.systemPrompt, /Failure does not enlarge permissions/);
    assert.match(call.systemPrompt, /Return exactly one D04 handoff/);
  }
});

test('unknown tools and second same-turn handoff cannot execute business effects', async () => {
  const fixture = transport([[{ name: 'shell', args: { command: 'touch forbidden' } }],
    [handoff(needInfo), handoff({ kind: 'cannot_continue', reason: 'overwrite' })]]);
  const result = await runWorker(request, fixture);
  assert.deepEqual(result.result, needInfo);
  assert.deepEqual(result.evidence.business_tools, []);
  assert.equal(result.evidence.model_calls, 2);
});

test('textual completion stops at budget and does not become a candidate', async () => {
  const fixture = transport(['Everything is complete']);
  await assert.rejects(runWorker({ ...request, config: { ...request.config, max_turns: 1 } }, fixture),
    error => error.code === 'backend_budget');
  assert.equal(fixture.calls.length, 1);
});

test('planner returns a proposal without accepting or executing it', async () => {
  const proposal = { schema: 'sop/1', id: 'protocol-proposal', task: 'orders.clean', steps: [] };
  const result = await runWorker({ ...request, mode: 'planner' }, transport([[handoff(proposal)]]));
  assert.deepEqual(result.result, proposal);
  assert.equal(result.evidence.status, 'handoff');
  assert.deepEqual(result.evidence.business_tools, []);
});


const sourceContext = {
  sources: { root_id: 'SKILL.md', files: [{ id: 'SKILL.md', relative_path: 'SKILL.md',
    sha256: 'a'.repeat(64), text: '# Order Skill\nAsk for the missing rule.',
    lines: [{ line: 1, text: '# Order Skill' }, { line: 2, text: 'Ask for the missing rule.' }] }] },
  clauses: { ambiguity: 'Retention rule is unknown and must be asked, not guessed.' },
};
const analysis = { schema: 'skill-analysis/1', task: 'orders.report', mode: 'pi',
  annotations: [
    { source_id: 'SKILL.md', start_line: 1, end_line: 1, kind: 'background', clauses: [], reason: 'Title' },
    { source_id: 'SKILL.md', start_line: 2, end_line: 2, kind: 'requirement', clauses: ['ambiguity'], reason: 'Rule gap' },
  ], rule: { status: 'unknown', value: null } };

test('author uses installed Pi loop with complete public sources and a proposal-only handoff', async () => {
  const fixture = transport([[{ name: 'context', args: {} }], [handoff(analysis)]]);
  const result = await runWorker({ ...request, mode: 'author', context: { ...sourceContext, scope: 'orders.report' } }, fixture);
  assert.deepEqual(result.result, analysis);
  assert.deepEqual(result.evidence.business_tools, []);
  assert.equal(result.evidence.transport, 'scripted_protocol_test');
  assert.equal(fixture.calls.length, 2);
  for (const call of fixture.calls) {
    assert.deepEqual(call.tools, ['context', 'handoff']);
    assert.match(call.systemPrompt, /EVERY nonempty line of EVERY supplied source exactly once/);
    assert.match(call.systemPrompt, /unsupported extra restriction/);
    assert.match(call.systemPrompt, /runtime with value:null/);
  }
  const provided = JSON.parse(fixture.calls[0].messages[0].content);
  assert.deepEqual(provided.sources, sourceContext.sources);
  const contextResult = fixture.calls[1].messages.find(message => message.role === 'toolResult');
  assert.deepEqual(JSON.parse(contextResult.content[0].text).sources, sourceContext.sources);
});

test('review is a separate Pi call with original sources and an exact review basis', async () => {
  const basis_hash = 'b'.repeat(64);
  const judgment = { schema: 'skill-review/1', basis_hash, verdict: 'reject', findings: [
    { code: 'missing_contract', source_id: 'SKILL.md', start_line: 2, end_line: 2, reason: 'Required business clauses absent' },
  ] };
  const fixture = transport([[handoff(judgment)]]);
  const result = await runWorker({ ...request, mode: 'review',
    context: { ...sourceContext, analysis, answers: {}, basis_hash, instruction: 'Check original spans independently.' } }, fixture);
  assert.deepEqual(result.result, judgment);
  assert.deepEqual(result.evidence.business_tools, []);
  assert.equal(fixture.calls.length, 1);
  assert.deepEqual(fixture.calls[0].tools, ['context', 'handoff']);
  assert.equal(fixture.calls[0].messages.length, 1); // no author conversation or self-review chain
  const supplied = JSON.parse(fixture.calls[0].messages[0].content);
  assert.deepEqual(supplied.sources, sourceContext.sources);
  assert.equal(supplied.basis_hash, basis_hash);
  assert.match(fixture.calls[0].systemPrompt, /INDEPENDENT semantic review/);
  assert.match(fixture.calls[0].systemPrompt, /requirements hidden as background\/examples/);
  assert.match(fixture.calls[0].systemPrompt, /Use reject for any unresolved concern or uncertainty/);
});

for (const mode of ['author', 'review']) {
  test(`${mode} rejects an incompatible handoff schema without accepting a D04 response`, async () => {
    const fixture = transport([[handoff(needInfo)]]);
    await assert.rejects(runWorker({ ...request, mode, context: sourceContext,
      config: { ...request.config, max_turns: 1 } }, fixture), error => error.code === 'backend_budget');
    assert.equal(fixture.calls.length, 1);
  });
}

const sourceHandle = { hash: '1'.repeat(64), bytes: 20, kind: 'input' };
const normalizedHandle = { hash: '2'.repeat(64), bytes: 20, kind: 'artifact' };
const cleanedHandle = { hash: '3'.repeat(64), bytes: 30, kind: 'artifact' };
const directContext = { task: 'orders.clean', inputs: { source_file: sourceHandle, rule: 'highest_revision' },
  contract: { capabilities: ['orders.normalize', 'orders.deduplicate'] },
  catalog: { 'orders.normalize': {}, 'orders.deduplicate': {} }, tool_receipts: [] };
const capability = (name, inputs) => ({ name: 'capability', args: { capability: name, inputs_json: JSON.stringify(inputs) } });

test('Pi can run two host-mediated capabilities and return their candidate without a child', async () => {
  const calls = [];
  const fixture = transport([
    [capability('orders.normalize', { source_file: sourceHandle })],
    [capability('orders.deduplicate', { normalized: normalizedHandle, rule: 'highest_revision' })],
    [handoff({ kind: 'candidate_result', outputs: { cleaned: cleanedHandle } })],
  ]);
  const result = await runWorker({ ...request, context: directContext }, fixture, async (name, inputs, raw) => {
    assert.deepEqual(JSON.parse(raw), inputs);
    calls.push({ name, inputs });
    return { operation: `op-${calls.length}`, outputs: name === 'orders.normalize'
      ? { normalized: normalizedHandle } : { cleaned: cleanedHandle }, reused: false };
  });
  assert.deepEqual(calls.map(call => call.name), ['orders.normalize', 'orders.deduplicate']);
  assert.deepEqual(result.result, { kind: 'candidate_result', outputs: { cleaned: cleanedHandle } });
  assert.deepEqual(result.evidence.business_tools, ['orders.normalize', 'orders.deduplicate']);
  for (const call of fixture.calls) assert.deepEqual(call.tools, ['context', 'handoff', 'capability']);
  assert.equal(result.evidence.model_calls, 3);
});

test('undeclared capability never reaches the trusted host', async () => {
  let executions = 0;
  const fixture = transport([[capability('shell', { command: 'forbidden' })], [handoff(needInfo)]]);
  await runWorker({ ...request, context: directContext }, fixture, async () => { executions++; });
  assert.equal(executions, 0);
});

for (const mode of ['author', 'review', 'planner']) {
  test(`${mode} cannot access capability even when a host callback was supplied`, async () => {
    let executions = 0;
    const value = mode === 'author' ? analysis : mode === 'review'
      ? { schema: 'skill-review/1', basis_hash: 'a'.repeat(64), verdict: 'reject', findings: ['test'] }
      : { schema: 'sop/1', id: 'test', task: 'orders.clean', steps: [] };
    const fixture = transport([[capability('orders.normalize', { source_file: sourceHandle })], [handoff(value)]]);
    const result = await runWorker({ ...request, mode, context: directContext }, fixture, async () => { executions++; });
    assert.equal(executions, 0);
    assert.deepEqual(result.result, value);
    assert.deepEqual(result.evidence.tools, ['context', 'handoff']);
  });
}

test('handoff revokes same-turn capability calls', async () => {
  let executions = 0;
  const fixture = transport([[handoff(needInfo), capability('orders.normalize', { source_file: sourceHandle })]]);
  await runWorker({ ...request, context: directContext }, fixture, async () => { executions++; });
  assert.equal(executions, 0);
});

test('host failure stops the remaining tool batch and preserves lack of candidate', async () => {
  let executions = 0;
  const fixture = transport([[capability('orders.normalize', { source_file: sourceHandle }),
    capability('orders.normalize', { source_file: sourceHandle }), handoff(needInfo)]]);
  await assert.rejects(runWorker({ ...request, context: directContext }, fixture, async () => {
    executions++; throw new Error('Host rejected execution');
  }), error => error.code === 'backend_protocol');
  assert.equal(executions, 1);
  assert.equal(fixture.calls.length, 1);
});

test('tool callback wait obeys the worker deadline', async () => {
  const fixture = transport([[capability('orders.normalize', { source_file: sourceHandle })]]);
  await assert.rejects(runWorker({ ...request, context: directContext, config: { ...request.config, timeout_ms: 25 } }, fixture,
    async () => new Promise(resolve => setTimeout(() => resolve({ operation: 'late', outputs: {}, reused: false }), 80))),
    error => error.code === 'backend_timeout');
});


function installationFixture(t, version, bundled) {
  const root = mkdtempSync(join(tmpdir(), 'sop-pi-discovery-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const codingRoot = join(root, 'lib/node_modules/@earendil-works/pi-coding-agent');
  const coreRoot = join(codingRoot, 'node_modules/@earendil-works/pi-agent-core');
  const aiRoot = join(codingRoot, 'node_modules/@earendil-works/pi-ai');
  for (const [directory, name] of [[codingRoot, 'pi-coding-agent'], [coreRoot, 'pi-agent-core'], [aiRoot, 'pi-ai']]) {
    mkdirSync(directory, { recursive: true });
    writeFileSync(join(directory, 'package.json'), JSON.stringify({ name: '@earendil-works/' + name, version }));
  }
  const cli = join(codingRoot, bundled ? 'dist/bundle/cli.js' : 'dist/cli.js');
  mkdirSync(join(codingRoot, bundled ? 'dist/bundle' : 'dist'), { recursive: true });
  writeFileSync(cli, '// discovery fixture; never executed');
  const bin = join(root, 'bin');
  mkdirSync(bin);
  symlinkSync(cli, join(bin, 'pi'));
  return { root, bin, codingRoot, coreRoot, aiRoot };
}

for (const [version, bundled] of [['0.80.6', false], ['0.87.1', true]]) {
  test(`discovers Pi ${version} CLI by package identity rather than fixed parent depth`, t => {
    const fixture = installationFixture(t, version, bundled);
    const found = findPiInstallation({ coreDir: '', searchPath: fixture.bin, startDirectory: fixture.root });
    assert.equal(found.coreRoot, fixture.coreRoot);
    assert.equal(found.codingRoot, fixture.codingRoot);
    assert.equal(found.aiRoot, fixture.aiRoot);
    assert.equal(found.version, version);
  });
}

test('unknown or mixed Pi versions fail closed before module execution', t => {
  const fixture = installationFixture(t, '0.87.1', true);
  const options = { coreDir: '', searchPath: fixture.bin, startDirectory: fixture.root };
  for (const [directory, name, version] of [
    [fixture.aiRoot, 'pi-ai', '0.80.6'],
    [fixture.coreRoot, 'pi-agent-core', '0.99.0'],
  ]) {
    writeFileSync(join(directory, 'package.json'), JSON.stringify({ name: '@earendil-works/' + name, version }));
    assert.throws(() => findPiInstallation(options), error => error.code === 'backend_version');
  }
});

test('explicit missing Pi installation does not fall back to another PATH installation', t => {
  const fixture = installationFixture(t, '0.87.1', true);
  assert.throws(() => findPiInstallation({ coreDir: join(fixture.root, 'missing'),
    searchPath: fixture.bin, startDirectory: fixture.root }), error => error.code === 'backend_unavailable');
});

test('supported installed loop preserves system authority and stops tool-only turns at budget', async () => {
  assert.ok(SUPPORTED_PI_VERSIONS.includes(pi.version));
  const fixture = transport([[{ name: 'context', args: {} }], [{ name: 'context', args: {} }], [handoff(needInfo)]]);
  await assert.rejects(runWorker({ ...request, config: { ...request.config, max_turns: 2 } }, fixture),
    error => error.code === 'backend_budget');
  assert.equal(fixture.calls.length, 2);
  for (const call of fixture.calls) {
    assert.match(call.systemPrompt, /One handoff ends this call/);
    assert.match(call.systemPrompt, /No native|no native/);
    assert.deepEqual(call.tools, ['context', 'handoff']);
  }
});

test('handoff ends a supported installed loop before any extra provider request', async () => {
  const fixture = transport([[handoff(needInfo)]]);
  const result = await runWorker(request, fixture);
  assert.deepEqual(result.result, needInfo);
  assert.equal(result.evidence.model_calls, 1);
  assert.equal(fixture.calls.length, 1);
});
