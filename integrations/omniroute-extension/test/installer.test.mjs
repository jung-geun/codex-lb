import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { run, UPSTREAM_COMMIT } from '../lib/index.mjs';

const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const stateDirectory = '.omniroute-subscriptions';

async function put(filename, bytes, mode = 0o644) {
  await fs.mkdir(path.dirname(filename), { recursive: true });
  await fs.writeFile(filename, bytes, { mode });
  await fs.chmod(filename, mode);
}

async function fixture(t) {
  // Canonicalize the OS temp root: macOS commonly exposes /var through a symlink.
  const root = await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()), 'omniroute-extension-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'upstream');
  const bundlePath = path.join(root, 'package', 'bundle');
  const packageJson = Buffer.from('{"name":"omniroute","version":"3.8.52"}\n');
  await put(path.join(target, 'package.json'), packageJson);
  const rows = [
    { path: 'src/existing.mjs', before: 'export const value = "before";\n', after: 'export const value = "after";\n', mode: 0o751 },
    { path: 'src/other.mjs', before: 'export const other = 1;\n', after: 'export const other = 2;\n', mode: 0o640 },
    { path: 'src/additional/new.mjs', before: null, after: 'export const added = true;\n', mode: 0o644 },
  ];
  for (const row of rows) {
    if (row.before !== null) {
      await put(path.join(target, row.path), row.before, row.mode);
      await put(path.join(bundlePath, 'blobs', hash(row.before)), row.before);
    }
    await put(path.join(bundlePath, 'blobs', hash(row.after)), row.after);
  }
  await put(path.join(target, '.env'), 'GATEWAY_KEY=fixture-only-not-a-real-key\n', 0o600);
  await put(path.join(target, 'data/storage.sqlite'), Buffer.from([0, 1, 2, 255, 9]), 0o600);
  await put(path.join(target, 'config/settings.json'), '{"custom":true}\n');
  const manifest = {
    schemaVersion: 1, name: 'omniroute-subscriptions', version: '1.0.0',
    upstream: { version: '3.8.52', commit: UPSTREAM_COMMIT, packageJsonSha256: hash(packageJson) },
    files: rows.map((row) => ({ path: row.path, beforeSha256: row.before === null ? null : hash(row.before), afterSha256: hash(row.after), mode: 420 })),
  };
  const writeManifest = () => put(path.join(bundlePath, 'manifest.json'), `${JSON.stringify(manifest)}\n`);
  await writeManifest();
  return { root, target, bundlePath, rows, manifest, writeManifest,
    execute: (action) => run(action, { target, bundlePath }),
    journalPath: path.join(target, stateDirectory, 'journal.json') };
}

async function snapshot(directory, { omitPrivate = false } = {}) {
  const result = {};
  async function visit(current, relative = '') {
    for (const name of (await fs.readdir(current)).sort()) {
      if (!relative && omitPrivate && name.startsWith('.omniroute-subscriptions')) continue;
      const filename = path.join(current, name);
      const item = relative ? `${relative}/${name}` : name;
      const info = await fs.lstat(filename);
      if (info.isSymbolicLink()) result[item] = { symlink: await fs.readlink(filename) };
      else if (info.isDirectory()) await visit(filename, item);
      else result[item] = { bytes: (await fs.readFile(filename)).toString('hex'), mode: info.mode & 0o7777 };
    }
  }
  await visit(directory);
  return result;
}

async function updateJournal(f, change) {
  const journal = JSON.parse(await fs.readFile(f.journalPath, 'utf8'));
  change(journal);
  await fs.writeFile(f.journalPath, `${JSON.stringify(journal)}\n`);
}

async function child(args, executable = globalThis.process.execPath) {
  const process = spawn(executable, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  process.stdout.setEncoding('utf8');
  process.stderr.setEncoding('utf8');
  process.stdout.on('data', (bytes) => { stdout += bytes; });
  process.stderr.on('data', (bytes) => { stderr += bytes; });
  const code = await new Promise((resolve, reject) => { process.once('error', reject); process.once('close', resolve); });
  return { code, stdout, stderr, pid: process.pid };
}

test('check reports ready without writing source, state, or locks', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  assert.deepEqual(await f.execute('check'), { status: 'ready', files: f.rows.length });
  assert.deepEqual(await snapshot(f.target), before);
});

test('apply, idempotent apply, remove and reapply preserve modes and unrelated settings/env/data', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  assert.equal((await f.execute('apply')).status, 'installed');
  for (const row of f.rows) {
    assert.equal(await fs.readFile(path.join(f.target, row.path), 'utf8'), row.after);
    assert.equal((await fs.stat(path.join(f.target, row.path))).mode & 0o7777, row.mode);
  }
  assert.equal((await fs.stat(path.join(f.target, stateDirectory))).mode & 0o077, 0);
  assert.equal((await fs.stat(f.journalPath)).mode & 0o077, 0);
  assert.equal((await fs.stat(path.join(f.target, stateDirectory, 'backup-0'))).mode & 0o077, 0);
  const installed = await snapshot(f.target);
  assert.equal((await f.execute('check')).status, 'installed');
  assert.equal((await f.execute('apply')).status, 'installed');
  assert.deepEqual(await snapshot(f.target), installed);
  assert.equal((await f.execute('remove')).status, 'removed');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
  assert.equal((await f.execute('check')).status, 'ready');
  assert.equal((await f.execute('remove')).status, 'removed');
  assert.equal((await f.execute('apply')).status, 'installed');
  assert.equal((await f.execute('remove')).status, 'removed');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
});

for (const area of ['files', 'originals']) {
  test(`tampered ${area} payload prevents all target mutations`, async (t) => {
    const f = await fixture(t);
    await fs.writeFile(path.join(f.bundlePath, 'blobs', hash(area === 'files' ? f.rows[1].after : f.rows[1].before)), 'tampered');
    const before = await snapshot(f.target);
    await assert.rejects(f.execute('apply'), /checksum mismatch/);
    await assert.rejects(f.execute('check'), /checksum mismatch/);
    assert.deepEqual(await snapshot(f.target), before);
  });
}

for (const replacement of ['{"name":"omniroute","version":"3.8.53"}', '{"name":"omniroute","version":"3.8.52","custom":true}']) {
  test(`wrong pinned package identity or same-version hash is rejected: ${replacement}`, async (t) => {
    const f = await fixture(t);
    await fs.writeFile(path.join(f.target, 'package.json'), replacement);
    const before = await snapshot(f.target);
    await assert.rejects(f.execute('apply'), /pinned OmniRoute/);
    assert.deepEqual(await snapshot(f.target), before);
  });
}

for (const conflict of ['modified', 'addition']) {
  test(`${conflict} conflict causes no earlier owned-file mutation`, async (t) => {
    const f = await fixture(t);
    const row = conflict === 'modified' ? f.rows[1] : f.rows[2];
    await put(path.join(f.target, row.path), 'user-owned content\n');
    const before = await snapshot(f.target);
    await assert.rejects(f.execute('apply'), /conflict/);
    await assert.rejects(f.execute('check'), /conflict/);
    assert.deepEqual(await snapshot(f.target), before);
  });
}

for (const badPath of ['../escape', '/absolute', 'src/../escape', 'src\\escape', 'src//escape', '.omniroute-subscriptions/journal.json', 'src/line\nbreak', 'C:/escape']) {
  test(`invalid manifest path is rejected before mutation: ${JSON.stringify(badPath)}`, async (t) => {
    const f = await fixture(t);
    f.manifest.files[0].path = badPath;
    await f.writeManifest();
    const before = await snapshot(f.target);
    await assert.rejects(f.execute('apply'), /path/);
    assert.deepEqual(await snapshot(f.target), before);
  });
}

for (const spelling of ['src/existing.mjs', 'src/EXISTING.mjs']) {
  test(`duplicate and case-aliased manifest paths are rejected: ${spelling}`, async (t) => {
    const f = await fixture(t);
    f.manifest.files.push({ ...f.manifest.files[0], path: spelling });
    await f.writeManifest();
    const before = await snapshot(f.target);
    await assert.rejects(f.execute('apply'), /duplicate/);
    assert.deepEqual(await snapshot(f.target), before);
  });
}

test('a symlink target root or ancestor cannot escape into another checkout', async (t) => {
  const f = await fixture(t);
  const link = path.join(f.root, 'target-link');
  await fs.symlink(f.target, link);
  const before = await snapshot(f.target);
  await assert.rejects(run('apply', { target: link, bundlePath: f.bundlePath }), /Symlink/);
  const ancestor = path.join(f.root, 'ancestor-link');
  await fs.symlink(f.root, ancestor);
  await assert.rejects(run('apply', { target: path.join(ancestor, 'upstream'), bundlePath: f.bundlePath }), /Symlink/);
  assert.deepEqual(await snapshot(f.target), before);
});

for (const kind of ['owned-file', 'source-parent', 'lock', 'journal-parent', 'bundle-file']) {
  test(`${kind} symlink is rejected without touching external bytes`, async (t) => {
    const f = await fixture(t);
    const external = path.join(f.root, 'external');
    await put(path.join(external, 'file'), 'outside immutable\n');
    if (kind === 'owned-file') {
      await fs.unlink(path.join(f.target, f.rows[0].path));
      await fs.symlink(path.join(external, 'file'), path.join(f.target, f.rows[0].path));
    } else if (kind === 'source-parent') {
      await fs.symlink(external, path.join(f.target, 'src/additional'));
    } else if (kind === 'lock') {
      await fs.symlink(path.join(external, 'file'), path.join(f.target, '.omniroute-subscriptions.lock'));
    } else if (kind === 'journal-parent') {
      await fs.symlink(external, path.join(f.target, stateDirectory));
    } else {
      const payloadPath = path.join(f.bundlePath, 'blobs', hash(f.rows[0].after));
      await fs.unlink(payloadPath);
      await fs.symlink(path.join(external, 'file'), payloadPath);
    }
    const before = await snapshot(f.target);
    const outside = await snapshot(external);
    await assert.rejects(f.execute('apply'), /Symlink/);
    assert.deepEqual(await snapshot(f.target), before);
    assert.deepEqual(await snapshot(external), outside);
  });
}

test('an edited installed file refuses the entire remove, retaining every other installed file', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await fs.writeFile(path.join(f.target, f.rows[1].path), 'manual user edit\n');
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /conflict/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('reverting one installed file to exact baseline is still an edit, not interrupted-journal recovery', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await fs.writeFile(path.join(f.target, f.rows[1].path), f.rows[1].before);
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /edited/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('changing an installed file mode prevents remove without overwriting that change', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await fs.chmod(path.join(f.target, f.rows[1].path), 0o600);
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /mode changed/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('a corrupt local backup rejects all source removal', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await fs.writeFile(path.join(f.target, stateDirectory, 'backup-1'), 'corrupt');
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /backup checksum/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('only remove recovers a mixed before/after interrupted apply', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await f.execute('apply');
  await updateJournal(f, (journal) => { journal.phase = 'applying'; });
  await fs.writeFile(path.join(f.target, f.rows[0].path), f.rows[0].before);
  await fs.unlink(path.join(f.target, f.rows[2].path));
  const interrupted = await snapshot(f.target);
  await assert.rejects(f.execute('apply'), /Interrupted/);
  await assert.rejects(f.execute('check'), /Interrupted/);
  assert.deepEqual(await snapshot(f.target), interrupted);
  assert.equal((await f.execute('remove')).status, 'removed');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
});

test('interrupted remove resumes safely from the mixed before/after journal', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await f.execute('apply');
  await updateJournal(f, (journal) => { journal.phase = 'removing'; });
  await fs.writeFile(path.join(f.target, f.rows[1].path), f.rows[1].before);
  await fs.unlink(path.join(f.target, f.rows[2].path));
  await f.execute('remove');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
});

test('unknown bytes in interrupted source prevent all recovery writes', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await updateJournal(f, (journal) => { journal.phase = 'applying'; });
  await fs.writeFile(path.join(f.target, f.rows[1].path), 'neither baseline nor payload');
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /conflict/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('interrupted private preparation is recoverable only with untouched source', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await fs.mkdir(path.join(f.target, stateDirectory), { mode: 0o700 });
  await put(path.join(f.target, stateDirectory, 'backup-0'), f.rows[0].before, 0o600);
  await assert.rejects(f.execute('apply'), /Interrupted preparation/);
  await f.execute('remove');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
  assert.equal((await f.execute('check')).status, 'ready');
});

test('preparing journal with a not-yet-written backup can recover without source mutation', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await f.execute('apply');
  await f.execute('remove');
  await updateJournal(f, (journal) => { journal.phase = 'preparing'; });
  await fs.unlink(path.join(f.target, stateDirectory, 'backup-1'));
  await f.execute('remove');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
});

test('remove preserves unrelated new files and never recursively deletes their directory', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  const unrelated = path.join(f.target, 'src/additional/keep.txt');
  await put(unrelated, 'user added after installation\n');
  await f.execute('remove');
  assert.equal(await fs.readFile(unrelated, 'utf8'), 'user added after installation\n');
  await assert.rejects(fs.stat(path.join(f.target, f.rows[2].path)), { code: 'ENOENT' });
});

test('remove does not delete a replacement directory at an installer-created path', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  const addedDirectory = path.join(f.target, 'src/additional');
  const movedDirectory = path.join(f.target, 'src/old-additional');
  await fs.rename(addedDirectory, movedDirectory);
  await fs.mkdir(addedDirectory);
  await put(path.join(addedDirectory, 'new.mjs'), f.rows[2].after);
  await f.execute('remove');
  assert.equal((await fs.stat(addedDirectory)).isDirectory(), true);
  assert.equal(await fs.readFile(path.join(movedDirectory, 'new.mjs'), 'utf8'), f.rows[2].after);
});

test('a live exclusive owner prevents both apply and remove and retains its lock', async (t) => {
  const f = await fixture(t);
  const lock = path.join(f.target, '.omniroute-subscriptions.lock');
  await put(lock, JSON.stringify({ pid: process.pid, token: randomUUID() }), 0o600);
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('apply'), /locked/);
  await assert.rejects(f.execute('remove'), /locked/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('remove reclaims a dead owner and a dead recovery generation, without stale-lock stealing', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await f.execute('apply');
  await updateJournal(f, (journal) => { journal.phase = 'applying'; });
  const departed = await child(['-e', '']);
  assert.equal(departed.code, 0);
  const first = randomUUID(), second = randomUUID();
  await put(path.join(f.target, '.omniroute-subscriptions.lock'), JSON.stringify({ pid: departed.pid, token: first }), 0o600);
  await put(path.join(f.target, `.omniroute-subscriptions-claim-${first}`), JSON.stringify({ pid: departed.pid, token: second }), 0o600);
  await assert.rejects(f.execute('apply'), /locked/);
  await f.execute('remove');
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
  assert.equal((await fs.readdir(f.target)).some((name) => name.startsWith('.omniroute-subscriptions-claim-') || name === '.omniroute-subscriptions.lock'), false);
});

test('CLI help, exit codes, real fixture apply and sanitized conflict output', async (t) => {
  const f = await fixture(t);
  await fs.cp(path.join(packageRoot, 'bin'), path.join(f.root, 'package/bin'), { recursive: true });
  await fs.cp(path.join(packageRoot, 'lib'), path.join(f.root, 'package/lib'), { recursive: true });
  const cli = path.join(f.root, 'package/bin/cli.mjs');
  const help = await child([cli, '--help']);
  assert.equal(help.code, 0);
  assert.match(help.stdout, /check\|apply\|remove/);
  assert.equal((await child([cli, 'apply', '--force'])).code, 2);
  const checked = await child([cli, 'check', '--target', f.target]);
  assert.equal(checked.code, 0);
  assert.match(checked.stdout, /^ready:/);
  assert.equal((await child([cli, 'apply', '--target', f.target])).code, 0);
  const secret = 'private-test-string-that-must-not-be-printed';
  await fs.writeFile(path.join(f.target, f.rows[1].path), secret);
  const rejected = await child([cli, 'remove', '--target', f.target]);
  assert.equal(rejected.code, 1);
  assert.match(rejected.stderr, /conflict/);
  assert.equal(`${rejected.stdout}${rejected.stderr}`.includes(secret), false);
});

for (const kind of ['journal.json', 'backup-0']) {
  test(`installed ${kind} symlink refuses every source restore`, async (t) => {
    const f = await fixture(t);
    await f.execute('apply');
    const owned = path.join(f.target, stateDirectory, kind);
    const external = path.join(f.root, `external-${kind}`);
    await fs.rename(owned, external);
    await fs.symlink(external, owned);
    const before = await snapshot(f.target);
    const externalBefore = await fs.readFile(external);
    await assert.rejects(f.execute('remove'), /Symlink/);
    assert.deepEqual(await snapshot(f.target), before);
    assert.deepEqual(await fs.readFile(external), externalBefore);
  });
}

test('a source symlink introduced after installation rejects the whole remove', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  const filename = path.join(f.target, f.rows[0].path);
  const external = path.join(f.root, 'external-installed-source');
  await fs.rename(filename, external);
  await fs.symlink(external, filename);
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('remove'), /Symlink/);
  assert.deepEqual(await snapshot(f.target), before);
  assert.equal(await fs.readFile(external, 'utf8'), f.rows[0].after);
});

test('an unjournaled mixture or complete payload cannot be adopted or removed', async (t) => {
  const f = await fixture(t);
  for (const row of f.rows) await put(path.join(f.target, row.path), row.after, row.mode);
  const before = await snapshot(f.target);
  await assert.rejects(f.execute('check'), /Untracked/);
  await assert.rejects(f.execute('apply'), /not pristine/);
  await assert.rejects(f.execute('remove'), /untracked/);
  assert.deepEqual(await snapshot(f.target), before);
});

test('empty installer-created directories are removed, but preexisting parents remain', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  assert.equal((await fs.stat(path.join(f.target, 'src/additional'))).isDirectory(), true);
  await f.execute('remove');
  await assert.rejects(fs.stat(path.join(f.target, 'src/additional')), { code: 'ENOENT' });
  assert.equal((await fs.stat(path.join(f.target, 'src'))).isDirectory(), true);
});

test('simultaneous dead-owner recoveries serialize before touching source', async (t) => {
  const f = await fixture(t);
  const before = await snapshot(f.target);
  await f.execute('apply');
  await updateJournal(f, (journal) => { journal.phase = 'applying'; });
  const departed = await child(['-e', '']);
  await put(path.join(f.target, '.omniroute-subscriptions.lock'), JSON.stringify({ pid: departed.pid, token: randomUUID() }), 0o600);
  const results = await Promise.allSettled([f.execute('remove'), f.execute('remove')]);
  assert.equal(results.some((result) => result.status === 'fulfilled'), true);
  assert.deepEqual(await snapshot(f.target, { omitPrivate: true }), before);
  assert.equal((await f.execute('check')).status, 'ready');
});

test('repeated remove preserves an adopted directory even after it becomes empty', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  const directory = path.join(f.target, 'src/additional');
  const retained = path.join(directory, 'user-owned.txt');
  await put(retained, 'keep this directory after removing the extension\n');
  await f.execute('remove');
  await fs.unlink(retained);
  const journalBefore = await fs.readFile(f.journalPath);
  await f.execute('remove');
  assert.equal((await fs.stat(directory)).isDirectory(), true);
  assert.deepEqual(await fs.readFile(f.journalPath), journalBefore);
});

test('a completed removed tombstone never replays historical directory receipts', async (t) => {
  const f = await fixture(t);
  await f.execute('apply');
  await f.execute('remove');
  const directory = path.join(f.target, 'src/additional');
  await fs.mkdir(directory);
  const info = await fs.stat(directory);
  // Model an older completed journal whose stale inode receipt matches a reused inode.
  await updateJournal(f, (journal) => {
    journal.createdDirectories = [{ path: 'src/additional', identity: `${info.dev}:${info.ino}` }];
  });
  const journalBefore = await fs.readFile(f.journalPath);
  await f.execute('remove');
  assert.equal((await fs.stat(directory)).isDirectory(), true);
  assert.deepEqual(await fs.readFile(f.journalPath), journalBefore);
});

test('an npm-packed and offline-installed release can apply and remove dotfile source changes', { timeout: 60000 }, async (t) => {
  const f = await fixture(t);
  const before = '.cache/\n';
  const after = '.cache/\n.local-extension-state/\n';
  await put(path.join(f.target, '.gitignore'), before);
  await put(path.join(f.bundlePath, 'blobs', hash(before)), before);
  await put(path.join(f.bundlePath, 'blobs', hash(after)), after);
  f.manifest.files.push({ path: '.gitignore', beforeSha256: hash(before), afterSha256: hash(after), mode: 420 });
  await f.writeManifest();
  const packageDirectory = path.dirname(f.bundlePath);
  await fs.cp(path.join(packageRoot, 'bin'), path.join(packageDirectory, 'bin'), { recursive: true });
  await fs.cp(path.join(packageRoot, 'lib'), path.join(packageDirectory, 'lib'), { recursive: true });
  await put(path.join(packageDirectory, 'package.json'), JSON.stringify({
    name: 'omniroute-subscriptions', version: '1.0.0', type: 'module',
    files: ['bin/', 'lib/', 'bundle/'], bin: { 'omniroute-subscriptions': 'bin/cli.mjs' },
  }));
  const cache = path.join(f.root, 'npm-cache');
  const packed = await child(['pack', packageDirectory, '--ignore-scripts', '--offline', '--json', '--cache', cache, '--pack-destination', f.root], 'npm');
  assert.equal(packed.code, 0, packed.stderr);
  const filename = JSON.parse(packed.stdout)[0].filename;
  const installedDirectory = path.join(f.root, 'installed');
  const installed = await child(['install', path.join(f.root, filename), '--prefix', installedDirectory,
    '--ignore-scripts', '--offline', '--no-audit', '--no-fund', '--cache', cache], 'npm');
  assert.equal(installed.code, 0, installed.stderr);
  const cli = path.join(installedDirectory, 'node_modules/omniroute-subscriptions/bin/cli.mjs');
  const applied = await child([cli, 'apply', '--target', f.target]);
  assert.equal(applied.code, 0, applied.stderr);
  assert.equal(await fs.readFile(path.join(f.target, '.gitignore'), 'utf8'), after);
  const removed = await child([cli, 'remove', '--target', f.target]);
  assert.equal(removed.code, 0, removed.stderr);
  assert.equal(await fs.readFile(path.join(f.target, '.gitignore'), 'utf8'), before);
});
