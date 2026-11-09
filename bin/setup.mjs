#!/usr/bin/env node
/** One-time installer, not a runtime service.
 *
 * Public: agentgenome-setup --client pi|codex|qoder|claude [--dry-run]
 * Existing shared Python installer owns all CLI configuration changes.
 * npm installs/caches may disappear: install a non-editable Python package,
 * and bind CLI entries to its persistent uv tool environment.
 */
import { spawn } from 'node:child_process';
import { constants as fsConstants, realpathSync } from 'node:fs';
import { access, mkdtemp, rm } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const UV_VERSION = '0.11.14';
export const UV_INSTALL_URL = `https://astral.sh/uv/${UV_VERSION}/install.sh`;
export const CLIENTS = ['pi', 'codex', 'qoder', 'claude'];

const EXIT_OK = 0;
const EXIT_ERROR = 1;
const EXIT_USAGE = 2;
const POSIX_PLATFORMS = new Set(['darwin', 'linux']);
const REQUIRED_NODE_MAJOR = 20;

const ACTIVATION = {
  pi: 'open a new Pi window and type /agentgenome',
  codex: 'open a new Codex session and type $agentgenome (or /skills)',
  qoder:
    'open a new qodercli session and type /agentgenome',
  claude:
    'open a new Claude Code session and type /agentgenome (do not use --bare)',
};

const USAGE = `Usage: agentgenome-setup --client KIND [options]

One-time setup for AgentGenome in a native CLI. Installs the Python runtime
into a persistent uv tool environment, then delegates the single native entry
to the shared installer. It does not start the service or configure models.
An existing uv is reused; when uv is absent, curl must be available to install
a pinned official uv. Supports macOS and Linux (POSIX) with Node.js 20+.

Options:
  --client KIND       pi | codex | qoder | claude (required)
  --config-root PATH  explicit isolated native config root
  --data-dir PATH     service data directory recorded in the entry
  --dry-run           show the plan without downloading, writing or installing
  -h, --help          show this help

Exit codes: 0 success, 1 setup failure, 2 invalid arguments; the shared
installer exit code (2 invalid, 3 conflict) is forwarded unchanged.`;

class UsageError extends Error {}

class SetupError extends Error {
  constructor(message, code = EXIT_ERROR) {
    super(message);
    this.code = code;
  }
}

function takeValue(argv, index, name) {
  const value = argv[index + 1];
  if (value === undefined || value === '' || value.startsWith('--')) {
    throw new UsageError(`--${name} requires a value`);
  }
  return value;
}

export function parseArgs(argv) {
  const options = {
    help: false,
    dryRun: false,
    client: null,
    configRoot: null,
    dataDir: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '-h' || argument === '--help') {
      options.help = true;
      continue;
    }
    if (argument === '--dry-run') {
      options.dryRun = true;
      continue;
    }
    const match = /^--(client|config-root|data-dir)(?:=(.*))?$/.exec(argument);
    if (match === null) {
      throw new UsageError(`unknown argument: ${argument}`);
    }
    const name = match[1];
    const inline = match[2];
    let value;
    if (inline === undefined) {
      value = takeValue(argv, index, name);
      index += 1;
    } else {
      if (inline === '') {
        throw new UsageError(`--${name} requires a value`);
      }
      value = inline;
    }
    if (name === 'client') {
      options.client = value;
    } else if (name === 'config-root') {
      options.configRoot = value;
    } else {
      options.dataDir = value;
    }
  }
  if (options.help) {
    return options;
  }
  if (options.client === null) {
    throw new UsageError('--client is required');
  }
  if (!CLIENTS.includes(options.client)) {
    throw new UsageError(
      `unsupported client: ${options.client}; expected one of ${CLIENTS.join(', ')}`,
    );
  }
  return options;
}

function runProcess(file, args, { env, cwd } = {}) {
  return new Promise((keep, fail) => {
    const child = spawn(file, args, { env, cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', fail);
    child.on('close', (code, signal) => {
      keep({ code: code ?? EXIT_ERROR, signal, stdout, stderr });
    });
  });
}

async function findOnPath(name, env) {
  for (const directory of (env.PATH ?? '').split(delimiter)) {
    if (directory === '') {
      continue;
    }
    const candidate = join(directory, name);
    try {
      await access(candidate, fsConstants.X_OK);
      return candidate;
    } catch {
      // Keep scanning PATH.
    }
  }
  return null;
}

async function downloadInstaller(deps, curl, destination) {
  const result = await deps.run(curl, [
    '--fail', '--silent', '--show-error', '--location',
    '--max-time', '120', '--proto', '=https', '--proto-redir', '=https',
    '--output', destination, UV_INSTALL_URL,
  ], { env: deps.env });
  if (result.code !== EXIT_OK) {
    echo(deps, result);
    throw new SetupError(
      `cannot download ${UV_INSTALL_URL} with curl (exit ${result.code})`,
      result.code);
  }
}

function echo(deps, result) {
  if (result.stdout) {
    deps.stdout(result.stdout.endsWith('\n') ? result.stdout : `${result.stdout}\n`);
  }
  if (result.stderr) {
    deps.stderr(result.stderr.endsWith('\n') ? result.stderr : `${result.stderr}\n`);
  }
}

function dataHome(env, home) {
  const configured = env.XDG_DATA_HOME;
  return configured && isAbsolute(configured)
    ? configured
    : join(home, '.local', 'share');
}

function entryArguments(options) {
  const args = ['client', 'install', '--client', options.client];
  if (options.configRoot !== null) {
    args.push('--config-root', options.configRoot);
  }
  if (options.dataDir !== null) {
    args.push('--data-dir', options.dataDir);
  }
  return args;
}

async function isExecutable(path, deps) {
  try {
    await deps.access(path, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function printPlan(deps, options, existingUv, uvDir) {
  const steps = [
    `Runtime source: ${deps.packageRoot}`,
    existingUv === null
      ? `uv: no executable on PATH or at ${join(uvDir, 'uv')}; curl is required `
        + `for this bootstrap. Download pinned ${UV_INSTALL_URL} to a temp file `
        + 'with curl, then run it with sh, installing unmanaged into '
        + `${uvDir} (no shell profile edits)`
      : `uv: reuse ${existingUv}`,
    'Python: 3.11, resolved by uv (downloaded only if absent)',
    `Install: uv tool install --reinstall --python 3.11 ${deps.packageRoot}`,
    'Console: resolve `uv tool dir --bin`/agentgenome after installation',
    `Client entry: agentgenome ${entryArguments(options).join(' ')}`,
    `Activate: ${ACTIVATION[options.client]}`,
    'Service: not started by this command; start it separately.',
  ];
  deps.stdout(
    'Dry run - no download, writes, or process lifecycle changes.\n\n'
      + `${steps.map((step, index) => `${index + 1}. ${step}`).join('\n')}\n`,
  );
}

async function bootstrapUv(deps, uvDir) {
  const curl = await deps.findExecutable('curl', deps.env);
  if (curl === null) {
    throw new SetupError(
      'uv is not installed and curl is required to bootstrap it; '
        + 'install uv or curl first');
  }
  deps.stdout(
    `no usable uv found; downloading uv ${UV_VERSION} with ${curl} into ${uvDir}\n`);
  const temporary = await deps.makeTempDir();
  try {
    const script = join(temporary, 'uv-install.sh');
    await downloadInstaller(deps, curl, script);
    const result = await deps.run('sh', [script], {
      env: {
        ...deps.env,
        UV_UNMANAGED_INSTALL: uvDir,
        UV_NO_MODIFY_PATH: '1',
      },
    });
    echo(deps, result);
    if (result.code !== EXIT_OK) {
      throw new SetupError(
        `uv bootstrap installer failed with exit code ${result.code}`,
        result.code);
    }
  } finally {
    await deps.removeDir(temporary).catch(() => {});
  }
  const uv = join(uvDir, 'uv');
  try {
    await deps.access(uv, fsConstants.X_OK);
  } catch {
    throw new SetupError(`uv bootstrap did not produce an executable at ${uv}`);
  }
  return uv;
}

function printSuccess(deps, options, consolePath, result) {
  let payload = null;
  try {
    payload = JSON.parse(result.stdout.trim());
  } catch {
    payload = null;
  }
  const lines = ['', 'AgentGenome setup complete.'];
  if (payload !== null && typeof payload.native_path === 'string') {
    lines.push(`Native entry: ${payload.native_path}`);
  }
  lines.push(`Console: ${consolePath}`);
  lines.push(`Activate: ${ACTIVATION[options.client]}`);
  lines.push('The service was not started. Start it separately with '
    + '`agentgenome start`.');
  deps.stdout(`${lines.join('\n')}\n`);
}

const defaultDeps = {
  platform: process.platform,
  nodeVersion: process.versions.node,
  env: process.env,
  home: homedir,
  packageRoot: PACKAGE_ROOT,
  stdout: (text) => process.stdout.write(text),
  stderr: (text) => process.stderr.write(text),
  run: runProcess,
  findExecutable: findOnPath,
  makeTempDir: () => mkdtemp(join(tmpdir(), 'agentgenome-setup-')),
  removeDir: (path) => rm(path, { recursive: true, force: true }),
  access: (path, mode) => access(path, mode),
};

export async function main(argv = process.argv.slice(2), overrides = {}) {
  const deps = { ...defaultDeps, ...overrides };
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    if (error instanceof UsageError) {
      deps.stderr(`agentgenome-setup: ${error.message}\n\n${USAGE}\n`);
      return EXIT_USAGE;
    }
    throw error;
  }
  if (options.help) {
    deps.stdout(`${USAGE}\n`);
    return EXIT_OK;
  }
  const nodeMajor = Number(String(deps.nodeVersion).split('.')[0]);
  if (!Number.isFinite(nodeMajor) || nodeMajor < REQUIRED_NODE_MAJOR) {
    deps.stderr(
      `agentgenome-setup: Node.js >= ${REQUIRED_NODE_MAJOR} is required; `
        + `found ${deps.nodeVersion}\n`);
    return EXIT_ERROR;
  }
  if (!POSIX_PLATFORMS.has(deps.platform)) {
    deps.stderr(
      `agentgenome-setup: unsupported platform ${deps.platform}; this installer `
        + 'supports macOS and Linux (POSIX) only\n');
    return EXIT_ERROR;
  }

  const uvDir = join(dataHome(deps.env, deps.home()), 'agentgenome', 'bootstrap', 'uv');
  try {
    const managedUv = join(uvDir, 'uv');
    const onPath = await deps.findExecutable('uv', deps.env);
    const reusable = onPath
      ?? (await isExecutable(managedUv, deps) ? managedUv : null);
    if (options.dryRun) {
      printPlan(deps, options, reusable, uvDir);
      return EXIT_OK;
    }
    const uv = reusable ?? await bootstrapUv(deps, uvDir);

    const installed = await deps.run(
      uv, ['tool', 'install', '--reinstall', '--python', '3.11', deps.packageRoot],
      { env: deps.env });
    echo(deps, installed);
    if (installed.code !== EXIT_OK) {
      deps.stderr(`agentgenome-setup: uv tool install failed with exit code `
        + `${installed.code}\n`);
      return installed.code;
    }

    const binResult = await deps.run(uv, ['tool', 'dir', '--bin'],
      { env: deps.env });
    if (binResult.code !== EXIT_OK) {
      echo(deps, binResult);
      deps.stderr('agentgenome-setup: `uv tool dir --bin` failed with exit code '
        + `${binResult.code}\n`);
      return binResult.code;
    }
    const binDir = binResult.stdout.trim().split(/\r?\n/).filter(Boolean).pop();
    if (!binDir) {
      deps.stderr('agentgenome-setup: `uv tool dir --bin` returned no directory\n');
      return EXIT_ERROR;
    }
    const consolePath = join(binDir, 'agentgenome');
    if (!await isExecutable(consolePath, deps)) {
      deps.stderr(`agentgenome-setup: installed console is missing or not `
        + `executable: ${consolePath}\n`);
      return EXIT_ERROR;
    }

    const entry = await deps.run(consolePath, entryArguments(options),
      { env: deps.env });
    echo(deps, entry);
    if (entry.code !== EXIT_OK) {
      deps.stderr('agentgenome-setup: `agentgenome client install` failed with '
        + `exit code ${entry.code}; no success was recorded\n`);
      return entry.code;
    }
    printSuccess(deps, options, consolePath, entry);
    return EXIT_OK;
  } catch (error) {
    deps.stderr(`agentgenome-setup: ${error.message}\n`);
    return error instanceof SetupError ? error.code : EXIT_ERROR;
  }
}

const invokedPath = process.argv[1];
const invoked = invokedPath !== undefined
  && realpathSync(invokedPath) === fileURLToPath(import.meta.url);
if (invoked) {
  process.exitCode = await main();
}
