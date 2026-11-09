import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import {
  access,
  chmod,
  mkdir,
  mkdtemp,
  rm,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  CLIENTS,
  UV_INSTALL_URL,
  UV_VERSION,
  main,
  parseArgs,
} from '../../bin/setup.mjs';

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

async function makeSandbox(t) {
  const root = await mkdtemp(join(tmpdir(), 'agentgenome-setup-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = join(root, 'home');
  const data = join(root, 'data');
  const source = join(root, 'npm-source');
  const uvDir = join(data, 'agentgenome', 'bootstrap', 'uv');
  const uvPath = join(uvDir, 'uv');
  const binDir = join(root, 'tool-bin');
  const consolePath = join(binDir, 'agentgenome');
  const curlPath = join(root, 'bin', 'curl');
  await mkdir(home, { recursive: true });
  await mkdir(source, { recursive: true });
  await writeFile(join(source, 'pyproject.toml'),
    '[project]\nname = "agentgenome"\n');
  await mkdir(binDir, { recursive: true });

  const calls = { run: [], downloaded: [], temp: [] };
  const output = [];
  const errors = [];
  const failures = { install: null, client: null, curl: null };
  const deps = {
    platform: 'linux',
    nodeVersion: '22.0.0',
    env: { PATH: '', HOME: home, XDG_DATA_HOME: data },
    home: () => home,
    packageRoot: source,
    stdout: (text) => output.push(text),
    stderr: (text) => errors.push(text),
    findExecutable: async (name) => (name === 'curl' ? curlPath : null),
    makeTempDir: async () => {
      const directory = await mkdtemp(join(root, 'bootstrap-'));
      calls.temp.push(directory);
      return directory;
    },
    access: (path, mode) => access(path, mode),    run: async (file, args, options = {}) => {
      calls.run.push({ file, args, options });
      if (file === curlPath) {
        if (failures.curl !== null) {
          return { code: failures.curl, stdout: '', stderr: 'curl: (22) 403\n' };
        }
        const destination = args[args.indexOf('--output') + 1];
        calls.downloaded.push({ url: args.at(-1), destination });
        await writeFile(destination, '#!/bin/sh\nexit 0\n');
        return { code: 0, stdout: '', stderr: '' };
      }
      if (file === 'sh') {
        await mkdir(uvDir, { recursive: true });
        await writeFile(uvPath, '#!/bin/sh\nexit 0\n');
        await chmod(uvPath, 0o755);
        return { code: 0, stdout: 'uv installed\n', stderr: '' };
      }
      if (file === uvPath && args[0] === 'tool' && args[1] === 'install') {
        if (failures.install !== null) {
          return { code: failures.install, stdout: '', stderr: 'install failed\n' };
        }
        await writeFile(consolePath, '# console\n');
        await chmod(consolePath, 0o755);
        return { code: 0, stdout: 'installed executable\n', stderr: '' };
      }
      if (file === uvPath && args[0] === 'tool' && args[1] === 'dir') {
        return { code: 0, stdout: `${binDir}\n`, stderr: '' };
      }
      if (file === consolePath) {
        if (failures.client !== null) {
          return { code: failures.client, stdout: '', stderr: 'conflict\n' };
        }
        return {
          code: 0,
          stdout: `${JSON.stringify({
            ok: true,
            native_path: join(home, 'config.toml'),
            activation_notes: ['reopen the host'],
          })}\n`,
          stderr: '',
        };
      }
      throw new Error(`unexpected command: ${file} ${args.join(' ')}`);
    },
  };
  return {
    root, home, data, source, uvDir, uvPath, binDir, consolePath,
    curlPath, calls, output, errors, failures, deps,
  };
}

function text(parts) {
  return parts.join('');
}

test('parseArgs accepts the documented flags and equals forms', () => {
  assert.deepEqual(parseArgs(['--help']), {
    help: true, dryRun: false, client: null, configRoot: null, dataDir: null,
  });
  const options = parseArgs([
    '--client=codex', '--dry-run', '--config-root', '/tmp/config',
    '--data-dir=/tmp/data',
  ]);
  assert.equal(options.client, 'codex');
  assert.equal(options.dryRun, true);
  assert.equal(options.configRoot, '/tmp/config');
  assert.equal(options.dataDir, '/tmp/data');
  for (const client of CLIENTS) {
    assert.equal(parseArgs(['--client', client]).client, client);
  }
});

test('parseArgs rejects invalid input before any installation step', () => {
  assert.throws(() => parseArgs([]), /--client is required/);
  assert.throws(() => parseArgs(['--client', 'vim']), /unsupported client/);
  assert.throws(() => parseArgs(['--client']), /requires a value/);
  assert.throws(() => parseArgs(['--client', '--dry-run']), /requires a value/);
  assert.throws(() => parseArgs(['--client=']), /requires a value/);
  assert.throws(() => parseArgs(['--bogus']), /unknown argument/);
  assert.throws(() => parseArgs(['--client', 'pi', 'extra']),
    /unknown argument: extra/);
});

test('--help is zero network, zero writes and zero child processes', async (t) => {
  const sandbox = await makeSandbox(t);
  const code = await main(['--help'], sandbox.deps);
  assert.equal(code, 0);
  assert.match(text(sandbox.output), /Usage: agentgenome-setup/);
  assert.equal(sandbox.calls.run.length, 0);
  assert.equal(sandbox.calls.downloaded.length, 0);
  assert.equal(existsSync(sandbox.uvDir), false);
});

test('--dry-run prints the plan without touching disk, network or processes', async (t) => {
  const sandbox = await makeSandbox(t);
  const code = await main(['--client', 'codex', '--dry-run'], sandbox.deps);
  assert.equal(code, 0);
  const plan = text(sandbox.output);
  assert.match(plan, /Dry run - no download, writes, or process lifecycle changes/);
  assert.ok(plan.includes(UV_INSTALL_URL));
  assert.ok(plan.includes(UV_VERSION));
  assert.ok(plan.includes(sandbox.uvDir));
  assert.match(plan, /curl is required for this bootstrap/);
  assert.match(plan, /uv tool install --reinstall --python 3\.11/);
  assert.match(plan, /agentgenome client install --client codex/);
  assert.match(plan, /\$agentgenome/);
  assert.equal(sandbox.calls.run.length, 0);
  assert.equal(sandbox.calls.downloaded.length, 0);
  assert.equal(existsSync(sandbox.uvDir), false);
  assert.equal(existsSync(sandbox.consolePath), false);
});

test('--dry-run reuses an existing uv without executing it', async (t) => {
  const sandbox = await makeSandbox(t);
  sandbox.deps.findExecutable = async () => '/opt/uv/bin/uv';
  const code = await main(['--client', 'pi', '--dry-run'], sandbox.deps);
  assert.equal(code, 0);
  assert.match(text(sandbox.output), /uv: reuse \/opt\/uv\/bin\/uv/);
  assert.equal(sandbox.calls.run.length, 0);
});

test('invalid flags and clients fail with exit 2 before any step', async (t) => {
  const sandbox = await makeSandbox(t);
  for (const argv of (
    [['--client', 'vim'], ['--bogus'], [], ['--client', 'pi', '--bogus']]
  )) {
    const code = await main(argv, sandbox.deps);
    assert.equal(code, 2, String(argv));
  }
  assert.equal(sandbox.calls.run.length, 0);
  assert.equal(sandbox.calls.downloaded.length, 0);
  assert.equal(existsSync(sandbox.uvDir), false);
});

test('unsupported platforms and old Node fail clearly with exit 1', async (t) => {
  const sandbox = await makeSandbox(t);
  const windows = await main(['--client', 'pi'],
    { ...sandbox.deps, platform: 'win32' });
  assert.equal(windows, 1);
  assert.match(text(sandbox.errors), /supports macOS and Linux \(POSIX\) only/);
  const oldNode = await main(['--client', 'pi'],
    { ...sandbox.deps, nodeVersion: '18.19.0' });
  assert.equal(oldNode, 1);
  assert.match(text(sandbox.errors), /Node\.js >= 20 is required/);
  assert.equal(sandbox.calls.run.length, 0);
});

test('missing uv is bootstrapped from the pinned script into XDG_DATA_HOME', async (t) => {
  const sandbox = await makeSandbox(t);
  const code = await main([
    '--client', 'pi', '--config-root', join(sandbox.root, 'config'),
    '--data-dir', join(sandbox.root, 'service-data'),
  ], sandbox.deps);
  assert.equal(code, 0);

  assert.equal(sandbox.calls.downloaded.length, 1);
  assert.equal(sandbox.calls.downloaded[0].url, UV_INSTALL_URL);
  assert.match(UV_INSTALL_URL, /^https:\/\/astral\.sh\/uv\/0\.11\.14\/install\.sh$/);

  const curl = sandbox.calls.run.find((call) => call.file === sandbox.curlPath);
  assert.ok(curl, 'bootstrap download must use the resolved curl executable');
  assert.ok(curl.args.includes('--max-time'));
  assert.ok(curl.args.includes('--proto'));
  assert.ok(curl.args.includes('=https'));
  assert.deepEqual(curl.args.slice(-3), [
    '--output', sandbox.calls.downloaded[0].destination, UV_INSTALL_URL,
  ]);
  assert.ok(!curl.args.includes('-c') && !curl.args.includes('-sS'),
    'curl must be invoked with an argument array, not a shell command string');

  const sh = sandbox.calls.run.find((call) => call.file === 'sh');
  assert.ok(sh, 'bootstrap installer must run through sh');
  assert.deepEqual(sh.args, [sandbox.calls.downloaded[0].destination]);
  assert.equal(sh.options.env.UV_UNMANAGED_INSTALL, sandbox.uvDir);
  assert.equal(sh.options.env.UV_NO_MODIFY_PATH, '1');
  assert.equal(sh.options.env.HOME, sandbox.home);
  assert.equal(sh.options.env.PATH, '');
  assert.equal(existsSync(join(sandbox.home, '.bashrc')), false);
  assert.equal(existsSync(join(sandbox.home, '.profile')), false);

  const install = sandbox.calls.run.find(
    (call) => call.file === sandbox.uvPath && call.args[1] === 'install');
  assert.deepEqual(install.args, [
    'tool', 'install', '--reinstall', '--python', '3.11', sandbox.source,
  ]);
  assert.ok(!install.args.includes('--force'));
  assert.ok(!install.args.includes('--editable') && !install.args.includes('-e'));

  assert.equal(existsSync(sandbox.calls.temp[0]), false,
    'downloaded installer temp directory must be removed');

  const entry = sandbox.calls.run.find((call) => call.file === sandbox.consolePath);
  assert.deepEqual(entry.args, [
    'client', 'install', '--client', 'pi',
    '--config-root', join(sandbox.root, 'config'),
    '--data-dir', join(sandbox.root, 'service-data'),
  ]);
  assert.ok(!entry.args.includes('--python'),
    'Node must not infer an interpreter; Python defaults to sys.executable');
  assert.ok(!entry.args.includes(sandbox.source),
    'entries must not bind to the npm source copy');

  const report = text(sandbox.output);
  assert.match(report, /AgentGenome setup complete/);
  assert.match(report, /Native entry: /);
  assert.match(report, /open a new Pi window and type \/agentgenome/);
  assert.match(report, /service was not started/i);
});

test('a repeat install reuses the managed uv and survives deleting the npm source', async (t) => {
  const sandbox = await makeSandbox(t);
  assert.equal(await main(['--client', 'codex'], sandbox.deps), 0);
  assert.equal(sandbox.calls.downloaded.length, 1);
  assert.equal(existsSync(sandbox.uvPath), true);
  assert.equal(existsSync(sandbox.consolePath), true);

  await rm(sandbox.source, { recursive: true, force: true });
  assert.equal(existsSync(sandbox.source), false);

  const shells = sandbox.calls.run.filter((call) => call.file === 'sh').length;
  assert.equal(await main(['--client', 'codex'], sandbox.deps), 0);
  assert.equal(sandbox.calls.downloaded.length, 1,
    'the second install must not download uv again');
  assert.equal(
    sandbox.calls.run.filter((call) => call.file === 'sh').length, shells,
    'the second install must not run the bootstrap installer again');

  const entries = sandbox.calls.run.filter(
    (call) => call.file === sandbox.consolePath);
  assert.equal(entries.length, 2);
  for (const call of entries) {
    assert.ok(!call.args.includes('--python'));
    assert.ok(!call.args.includes(sandbox.source));
  }
});

test('--dry-run reuses a managed uv left by an earlier bootstrap', async (t) => {
  const sandbox = await makeSandbox(t);
  assert.equal(await main(['--client', 'pi', '--dry-run'], sandbox.deps), 0);
  assert.ok(!text(sandbox.output).includes(`reuse ${sandbox.uvPath}`));

  assert.equal(await main(['--client', 'pi'], sandbox.deps), 0);
  const before = sandbox.calls.run.length;
  assert.equal(await main(['--client', 'pi', '--dry-run'], sandbox.deps), 0);
  assert.match(text(sandbox.output), new RegExp(`uv: reuse ${sandbox.uvPath}`));
  assert.equal(sandbox.calls.run.length, before,
    'dry-run must not execute the managed uv');
});

test('a failed curl download keeps its exit code and cleans the temp dir', async (t) => {
  const sandbox = await makeSandbox(t);
  sandbox.failures.curl = 22;
  const code = await main(['--client', 'pi'], sandbox.deps);
  assert.equal(code, 22);
  assert.match(text(sandbox.errors), /cannot download .*exit 22/s);
  assert.match(text(sandbox.errors), /curl: \(22\) 403/);
  assert.doesNotMatch(text(sandbox.output), /setup complete/);
  assert.equal(existsSync(sandbox.calls.temp[0]), false);
  assert.equal(sandbox.calls.run.some((call) => call.file === 'sh'), false);
});

test('missing curl fails clearly before any download or install', async (t) => {
  const sandbox = await makeSandbox(t);
  sandbox.deps.findExecutable = async () => null;
  const code = await main(['--client', 'pi'], sandbox.deps);
  assert.equal(code, 1);
  assert.match(text(sandbox.errors), /curl is required to bootstrap/);
  assert.equal(sandbox.calls.downloaded.length, 0);
  assert.equal(sandbox.calls.run.length, 0);
  assert.equal(sandbox.calls.temp.length, 0);
  assert.equal(existsSync(sandbox.uvDir), false);
});

test('uv tool install failure preserves its exit code', async (t) => {
  const sandbox = await makeSandbox(t);
  sandbox.failures.install = 7;
  const code = await main(['--client', 'pi'], sandbox.deps);
  assert.equal(code, 7);
  assert.match(text(sandbox.errors), /uv tool install failed with exit code 7/);
  assert.doesNotMatch(text(sandbox.output), /setup complete/);
  assert.equal(
    sandbox.calls.run.some((call) => call.file === sandbox.consolePath), false);
});

test('shared installer exit codes are forwarded, including conflict 3', async (t) => {
  const sandbox = await makeSandbox(t);
  sandbox.failures.client = 3;
  const code = await main(['--client', 'claude'], sandbox.deps);
  assert.equal(code, 3);
  assert.match(text(sandbox.errors), /no success was recorded/);
  assert.doesNotMatch(text(sandbox.output), /setup complete/);
});

test('Qoder output names its native skill trigger', async (t) => {
  const sandbox = await makeSandbox(t);
  const code = await main(['--client', 'qoder'], sandbox.deps);
  assert.equal(code, 0);
  const report = text(sandbox.output);
  assert.match(report, /qodercli session and type \/agentgenome/);
  assert.ok(!/\$agentgenome/.test(report));
});

test('npm pack ships only the allowlisted runtime and exclusions hold', () => {
  const packed = execFileSync(
    'npm', ['pack', '--dry-run', '--json', '--ignore-scripts'],
    {
      cwd: PACKAGE_ROOT,
      encoding: 'utf8',
      env: { ...process.env, npm_config_loglevel: 'error' },
    },
  );
  const [manifest] = JSON.parse(packed);
  const paths = manifest.files.map((file) => file.path);
  for (const required of [
    'package.json',
    'bin/setup.mjs',
    'pyproject.toml',
    'LICENSE',
    'README.md',
    'node_runtime/__init__.py',
    'node_runtime/control_plane/cli.py',
    'node_runtime/control_plane/client_install.py',
    'node_runtime/control_plane/client_activation.md',
    'node_runtime/control_plane/backends/pi_command.mjs',
  ]) {
    assert.ok(paths.includes(required), `npm pack is missing ${required}`);
  }
  for (const path of paths) {
    assert.ok(!path.startsWith('tests/'), `tests leaked: ${path}`);
    assert.ok(!path.startsWith('.refactor/'), `.refactor leaked: ${path}`);
    assert.ok(!path.startsWith('docs/'), `docs leaked: ${path}`);
    assert.ok(!path.startsWith('.venv/'), `.venv leaked: ${path}`);
    assert.ok(!path.includes('__pycache__'), `bytecode leaked: ${path}`);
    assert.ok(!path.endsWith('.pyc'), `bytecode leaked: ${path}`);
    assert.notEqual(path, 'uv.lock');
    assert.doesNotMatch(path, /(^|\/)(\.env|credentials?|secrets?)(\.|$)/i);
  }
});
