import * as fs from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const PACKAGE_NAME = 'omniroute-subscriptions';
export const VERSION = '1.1.0';
export const UPSTREAM_COMMIT = 'edab7c3a59bf7d6f040d581dbda3a46adbee0d57';
const STATE = '.omniroute-subscriptions';
const LOCK = '.omniroute-subscriptions.lock';
const DEFAULT_BUNDLE = fileURLToPath(new URL('../bundle/', import.meta.url));
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const fail = (message) => { throw new Error(message); };
const isHash = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || /[\u0000-\u001f\u007f]/.test(value) ||
      value.split('/').some((part) => !part || part === '.' || part === '..' || part.includes(':')) ||
      path.posix.isAbsolute(value) || value.split('/')[0].toLowerCase().startsWith('.omniroute-subscriptions')) {
    fail('Invalid or reserved bundle path');
  }
  return value;
}

async function statOrNull(filename) {
  try { return await fs.lstat(filename); } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

// Inspect every ancestor, including ancestors of the supplied root. Never follow a symlink.
async function guard(filename, { missing = false, directory = false } = {}) {
  const absolute = path.resolve(filename);
  const root = path.parse(absolute).root;
  const parts = absolute.slice(root.length).split(path.sep).filter(Boolean);
  let current = root;
  for (let index = 0; index < parts.length; index++) {
    current = path.join(current, parts[index]);
    const info = await statOrNull(current);
    if (!info) {
      if (missing) return null;
      fail('Required source or bundle path is missing');
    }
    if (info.isSymbolicLink()) fail('Symlink paths are not supported');
    if (index < parts.length - 1 || directory) {
      if (!info.isDirectory()) fail('Expected a directory in source or bundle path');
    } else if (!info.isFile()) fail('Expected a regular source or bundle file');
  }
  return fs.lstat(absolute);
}

async function readFile(filename) {
  await guard(filename);
  const handle = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat();
    if (!info.isFile()) fail('Expected a regular file');
    return { bytes: await handle.readFile(), mode: info.mode & 0o7777 };
  } finally { await handle.close(); }
}

async function fileState(filename) {
  const info = await guard(filename, { missing: true });
  if (!info) return { hash: null, mode: null };
  const file = await readFile(filename);
  return { hash: digest(file.bytes), mode: file.mode };
}

function parseJson(bytes, label) {
  try { return JSON.parse(bytes.toString('utf8')); } catch { fail(`Invalid ${label}`); }
}

async function loadBundle(bundlePath) {
  const root = path.resolve(bundlePath);
  await guard(root, { directory: true });
  const { bytes } = await readFile(path.join(root, 'manifest.json'));
  const manifest = parseJson(bytes, 'bundle manifest');
  if (manifest.schemaVersion !== 2 || manifest.name !== PACKAGE_NAME || manifest.version !== VERSION ||
      manifest.upstream?.version !== '3.8.52' || manifest.upstream?.commit !== UPSTREAM_COMMIT ||
      !isHash(manifest.upstream?.packageJsonSha256) || !Array.isArray(manifest.files) || !manifest.files.length) {
    fail('Unsupported bundle identity or upstream pin');
  }
  const seen = new Set();
  const files = [];
  for (const row of manifest.files) {
    safeRelative(row.path);
    const folded = row.path.normalize('NFC').toLowerCase();
    if (folded === 'package.json' || seen.has(folded) ||
        !(row.afterSha256 === null || isHash(row.afterSha256)) ||
        !(row.beforeSha256 === null || isHash(row.beforeSha256)) || row.beforeSha256 === row.afterSha256 ||
        row.mode !== 0o644) fail('Invalid or duplicate bundle entry');
    seen.add(folded);
    let payload = null;
    if (row.afterSha256 !== null) {
      payload = (await readFile(path.join(root, 'blobs', row.afterSha256))).bytes;
      if (digest(payload) !== row.afterSha256) fail('Bundle payload checksum mismatch');
    }
    let original = null;
    if (row.beforeSha256 !== null) {
      original = (await readFile(path.join(root, 'blobs', row.beforeSha256))).bytes;
      if (digest(original) !== row.beforeSha256) fail('Bundle original checksum mismatch');
    }
    files.push({ ...row, payload, original });
  }
  for (const row of files) {
    const components = row.path.split('/');
    components.pop();
    while (components.length) {
      if (seen.has(components.join('/').normalize('NFC').toLowerCase())) fail('Bundle file overlaps another file parent');
      components.pop();
    }
  }
  return { manifest, files, hash: digest(bytes) };
}

async function syncDirectory(directory) {
  const handle = await fs.open(directory, constants.O_RDONLY | constants.O_NOFOLLOW);
  try { await handle.sync(); } finally { await handle.close(); }
}

async function atomicWrite(filename, bytes, mode, stagingDirectory = path.dirname(filename)) {
  await guard(path.dirname(filename), { directory: true });
  await guard(stagingDirectory, { directory: true });
  await guard(filename, { missing: true });
  const temporary = path.join(stagingDirectory, `.omniroute-subscriptions-write-${randomUUID()}`);
  const handle = await fs.open(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
  try {
    await handle.writeFile(bytes);
    await handle.chmod(mode);
    await handle.sync();
  } catch (error) {
    await handle.close();
    await fs.unlink(temporary);
    throw error;
  }
  await handle.close();
  try {
    await guard(filename, { missing: true });
    await fs.rename(temporary, filename);
    await syncDirectory(path.dirname(filename));
  } catch (error) {
    if (await statOrNull(temporary)) await fs.unlink(temporary);
    throw error;
  }
}

async function inspectTarget(target, bundle) {
  await guard(target, { directory: true });
  const packageFile = await readFile(path.join(target, 'package.json'));
  const metadata = parseJson(packageFile.bytes, 'target package.json');
  if (metadata.name !== 'omniroute' || metadata.version !== '3.8.52' ||
      digest(packageFile.bytes) !== bundle.manifest.upstream.packageJsonSha256) {
    fail('Target package.json does not match pinned OmniRoute 3.8.52');
  }
  const states = [];
  for (const row of bundle.files) {
    const state = await fileState(path.join(target, row.path));
    if (state.hash !== row.beforeSha256 && state.hash !== row.afterSha256) fail(`Owned source conflict: ${row.path}`);
    states.push(state);
  }
  return states;
}

function deletionParents(bundle) {
  const parents = new Set();
  for (const row of bundle.files) {
    if (row.afterSha256 !== null) continue;
    let parent = path.posix.dirname(row.path);
    while (true) {
      parents.add(parent);
      if (parent === '.') break;
      parent = path.posix.dirname(parent);
    }
  }
  return [...parents].sort();
}

function installedState(row, state, entry) {
  return state.hash === row.afterSha256 && (state.hash === null || state.mode === entry.mode);
}

async function readJournal(target, bundle) {
  const directory = path.join(target, STATE);
  const directoryInfo = await guard(directory, { missing: true, directory: true });
  if (!directoryInfo) return null;
  if ((directoryInfo.mode & 0o077) !== 0) fail('Recovery directory must remain private');
  if (!await guard(path.join(directory, 'journal.json'), { missing: true })) return { orphan: true };
  const journal = parseJson((await readFile(path.join(directory, 'journal.json'))).bytes, 'transaction journal');
  if (journal.schemaVersion !== 2 || journal.bundleSha256 !== bundle.hash ||
      !['preparing', 'applying', 'installed', 'removing', 'removed'].includes(journal.phase) ||
      !Array.isArray(journal.entries) || journal.entries.length !== bundle.files.length || !Array.isArray(journal.createdDirectories) ||
      !Array.isArray(journal.deletionParents)) {
    fail('Transaction journal does not match this bundle');
  }
  for (let index = 0; index < bundle.files.length; index++) {
    const row = bundle.files[index];
    const entry = journal.entries[index];
    if (entry.path !== row.path || !Number.isInteger(entry.mode) || entry.mode < 0 || entry.mode > 0o7777 ||
        (row.beforeSha256 === null && entry.mode !== row.mode) ||
        entry.backup !== (row.beforeSha256 === null ? null : `backup-${index}`)) fail('Invalid transaction journal entry');
    if (entry.backup !== null) {
      const backupPath = path.join(directory, entry.backup);
      if (!await guard(backupPath, { missing: true }) && journal.phase === 'preparing') continue;
      const backup = (await readFile(backupPath)).bytes;
      if (digest(backup) !== row.beforeSha256) fail('Transaction backup checksum mismatch');
    }
  }
  const parents = deletionParents(bundle);
  if (journal.deletionParents.length !== parents.length) fail('Invalid deletion parent records');
  for (let index = 0; index < parents.length; index++) {
    const receipt = journal.deletionParents[index];
    if (receipt?.path !== parents[index] || typeof receipt.identity !== 'string' || !/^\d+:\d+$/.test(receipt.identity)) {
      fail('Invalid deletion parent record');
    }
    if (journal.phase !== 'removed') {
      const info = await guard(path.join(target, receipt.path), { directory: true });
      if (`${info.dev}:${info.ino}` !== receipt.identity) fail('Deletion source ancestor changed');
    }
  }
  const possibleDirectories = new Set();
  for (const row of bundle.files) {
    let parent = path.posix.dirname(row.path);
    while (parent !== '.') { possibleDirectories.add(parent); parent = path.posix.dirname(parent); }
  }
  const seen = new Set();
  for (const item of journal.createdDirectories) {
    safeRelative(item?.path);
    if (!possibleDirectories.has(item.path) || seen.has(item.path) ||
        !(item.identity === null || (typeof item.identity === 'string' && /^\d+:\d+$/.test(item.identity)))) fail('Invalid transaction directory record');
    seen.add(item.path);
    await guard(path.join(target, item.path), { missing: true, directory: true });
  }
  return journal;
}

async function saveJournal(target, journal) {
  await atomicWrite(path.join(target, STATE, 'journal.json'), Buffer.from(`${JSON.stringify(journal)}\n`), 0o600);
}

async function lockTarget(target, action) {
  const filename = path.join(target, LOCK);
  await guard(filename, { missing: true });
  const token = randomUUID();
  const temporary = path.join(target, `.omniroute-subscriptions-owner-${token}`);
  const handle = await fs.open(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
  try { await handle.writeFile(JSON.stringify({ pid: process.pid, token })); await handle.sync(); }
  finally { await handle.close(); }
  const chain = [];
  const records = new Map();
  const seen = new Set();
  let candidate = filename;
  try {
    while (true) {
      try {
        // Linking a fully written immutable owner record is atomic and exclusive.
        await fs.link(temporary, candidate);
        for (const [ancestor, expected] of records) {
          if (digest((await readFile(ancestor)).bytes) !== expected) {
            await fs.unlink(candidate);
            fail('Source lock changed during recovery');
          }
        }
        chain.push(candidate);
        await syncDirectory(target);
        break;
      } catch (error) { if (error.code !== 'EEXIST') throw error; }
      const ownerBytes = (await readFile(candidate)).bytes;
      const owner = parseJson(ownerBytes, 'source lock');
      records.set(candidate, digest(ownerBytes));
      if (!Number.isSafeInteger(owner.pid) || owner.pid <= 0 ||
          typeof owner.token !== 'string' || !/^[a-f0-9-]{36}$/.test(owner.token) || seen.has(owner.token)) {
        fail('Invalid source lock; manual inspection required');
      }
      seen.add(owner.token);
      let alive = true;
      try { process.kill(owner.pid, 0); } catch (probe) { if (probe.code === 'ESRCH') alive = false; }
      if (alive || action !== 'remove') fail('Source is locked; only remove can recover an abandoned transaction');
      chain.push(candidate);
      // Never unlink a dead lock to reclaim it: that races with another recoverer.
      // Claim each dead generation once; a crash during recovery adds another generation.
      candidate = path.join(target, `.omniroute-subscriptions-claim-${owner.token}`);
    }
  } finally { await fs.unlink(temporary); }
  return async () => {
    const owner = parseJson((await readFile(candidate)).bytes, 'source lock');
    if (owner.token !== token || owner.pid !== process.pid) fail('Source lock ownership changed');
    // Remove the root first. New owners use new tokens and cannot share our claim paths.
    await fs.unlink(filename);
    for (const item of chain.slice(1)) await fs.unlink(item);
    await syncDirectory(target);
  };
}

async function prepare(target, bundle, states, reuse = false) {
  const directory = path.join(target, STATE);
  if (!reuse) await fs.mkdir(directory, { mode: 0o700 });
  // Validate any surviving preparation backups before writing any private state.
  for (let index = 0; index < bundle.files.length; index++) {
    if (bundle.files[index].original !== null) {
      const filename = path.join(directory, `backup-${index}`);
      const existing = await fileState(filename);
      if (existing.hash !== null && existing.hash !== bundle.files[index].beforeSha256) fail('Transaction backup checksum mismatch');
    }
  }
  const parents = [];
  for (const relative of deletionParents(bundle)) {
    const info = await guard(path.join(target, relative), { directory: true });
    parents.push({ path: relative, identity: `${info.dev}:${info.ino}` });
  }
  const journal = {
    schemaVersion: 2, bundleSha256: bundle.hash, phase: 'preparing',
    entries: bundle.files.map((row, index) => ({ path: row.path, mode: states[index].mode ?? row.mode,
      backup: row.beforeSha256 === null ? null : `backup-${index}` })),
    createdDirectories: [], deletionParents: parents,
  };
  await saveJournal(target, journal);
  await syncDirectory(target);
  for (let index = 0; index < bundle.files.length; index++) {
    if (bundle.files[index].original !== null) {
      await atomicWrite(path.join(directory, `backup-${index}`), bundle.files[index].original, 0o600);
    }
  }
  journal.phase = 'applying';
  await saveJournal(target, journal);
  return journal;
}

async function ensureParents(target, relative, journal) {
  const parts = relative.split('/');
  parts.pop();
  let parent = '';
  for (const part of parts) {
    parent = parent ? `${parent}/${part}` : part;
    if (!await guard(path.join(target, parent), { missing: true, directory: true })) {
      const receipt = { path: parent, identity: null };
      journal.createdDirectories.push(receipt);
      await saveJournal(target, journal);
      await fs.mkdir(path.join(target, parent), { mode: 0o755 });
      const info = await guard(path.join(target, parent), { directory: true });
      receipt.identity = `${info.dev}:${info.ino}`;
      await syncDirectory(path.dirname(path.join(target, parent)));
      await saveJournal(target, journal);
    }
  }
}

async function undo(target, bundle, journal) {
  // Validate the complete scope and every backup before restoring even one path.
  const states = await inspectTarget(target, bundle);
  await readJournal(target, bundle);
  if (['preparing', 'removed'].includes(journal.phase)) {
    if (states.some((state, index) => state.hash !== bundle.files[index].beforeSha256)) fail('Preparation or removed transaction source changed');
    if (journal.phase === 'preparing') {
      for (let index = 0; index < bundle.files.length; index++) {
        if (bundle.files[index].original !== null) {
          await atomicWrite(path.join(target, STATE, `backup-${index}`), bundle.files[index].original, 0o600);
        }
      }
    }
  }
  if (journal.phase === 'installed' && states.some((state, index) => state.hash !== bundle.files[index].afterSha256)) {
    fail('Installed source was edited; remove refused');
  }
  for (let index = 0; index < states.length; index++) {
    if (states[index].hash !== null && states[index].mode !== journal.entries[index].mode) fail('Owned source mode changed; remove refused');
  }
  // A completed removal no longer owns any directories. Never replay old receipts.
  if (journal.phase === 'removed') return;
  journal.phase = 'removing';
  await saveJournal(target, journal);
  for (let index = bundle.files.length - 1; index >= 0; index--) {
    const row = bundle.files[index];
    const filename = path.join(target, row.path);
    const current = await fileState(filename);
    if (current.hash !== row.beforeSha256 && current.hash !== row.afterSha256) fail(`Source changed during removal: ${row.path}`);
    if (current.hash === row.beforeSha256) continue;
    if (row.beforeSha256 === null) {
      await fs.unlink(filename);
      await syncDirectory(path.dirname(filename));
    } else {
      if (row.afterSha256 === null) await readJournal(target, bundle);
      const backup = (await readFile(path.join(target, STATE, journal.entries[index].backup))).bytes;
      if (digest(backup) !== row.beforeSha256) fail('Transaction backup changed during removal');
      await atomicWrite(filename, backup, journal.entries[index].mode, path.join(target, STATE));
    }
  }
  for (const receipt of [...journal.createdDirectories].sort((a, b) => b.path.split('/').length - a.path.split('/').length)) {
    const directory = path.join(target, receipt.path);
    const info = await guard(directory, { missing: true, directory: true });
    if (!info || receipt.identity === null || receipt.identity !== `${info.dev}:${info.ino}`) continue;
    try { await fs.rmdir(directory); await syncDirectory(path.dirname(directory)); }
    catch (error) { if (error.code !== 'ENOTEMPTY' && error.code !== 'EEXIST') throw error; }
  }
  // Retain private recovery metadata: deleting backups and journal cannot be atomic.
  // A durable removed marker makes repeated remove and later apply safe across crashes.
  journal.phase = 'removed';
  journal.createdDirectories = [];
  await saveJournal(target, journal);
  await syncDirectory(target);
}

/** Offline, source-only operation. bundlePath is injectable for public fixture tests. */
export async function run(action, { target, bundlePath = DEFAULT_BUNDLE } = {}) {
  if (!['check', 'apply', 'remove'].includes(action) || typeof target !== 'string' || !target) fail('Expected check, apply or remove and a target directory');
  const root = path.resolve(target);
  const bundle = await loadBundle(bundlePath);
  await guard(root, { directory: true });
  if (action === 'check') {
    const states = await inspectTarget(root, bundle);
    const journal = await readJournal(root, bundle);
    if (await guard(path.join(root, LOCK), { missing: true })) fail('Source is locked; stop the owner or recover with remove');
    if (journal?.orphan || (journal?.phase && !['installed', 'removed'].includes(journal.phase))) fail('Interrupted transaction; recover with remove');
    if (journal?.phase === 'installed') {
      if (states.some((state, index) => !installedState(bundle.files[index], state, journal.entries[index]))) fail('Installed source was edited');
      return { status: 'installed', files: bundle.files.length };
    }
    if (states.some((state, index) => state.hash !== bundle.files[index].beforeSha256)) fail('Untracked or mixed extension source; transaction journal required');
    return { status: 'ready', files: bundle.files.length };
  }
  const unlock = await lockTarget(root, action);
  try {
    const states = await inspectTarget(root, bundle);
    let journal = await readJournal(root, bundle);
    if (journal?.orphan) {
      if (action !== 'remove') fail('Interrupted preparation; recover with remove');
      if (states.some((state, index) => state.hash !== bundle.files[index].beforeSha256)) fail('Interrupted preparation source changed');
      journal = await prepare(root, bundle, states, true);
    }
    if (action === 'remove') {
      if (!journal) {
        if (states.some((state, index) => state.hash !== bundle.files[index].beforeSha256)) fail('Cannot remove untracked extension source');
        return { status: 'ready', files: bundle.files.length };
      }
      await undo(root, bundle, journal);
      return { status: 'removed', files: bundle.files.length };
    }
    if (journal && journal.phase !== 'removed') {
      if (journal.phase !== 'installed') fail('Interrupted transaction; recover with remove');
      if (states.some((state, index) => !installedState(bundle.files[index], state, journal.entries[index]))) fail('Installed source was edited');
      return { status: 'installed', files: bundle.files.length };
    }
    if (states.some((state, index) => state.hash !== bundle.files[index].beforeSha256)) fail('Target is not pristine within the extension scope');
    journal = await prepare(root, bundle, states, journal?.phase === 'removed');
    try {
      for (let index = 0; index < bundle.files.length; index++) {
        const row = bundle.files[index];
        if (row.beforeSha256 === null) await ensureParents(root, row.path, journal);
        if ((await fileState(path.join(root, row.path))).hash !== row.beforeSha256) fail(`Source changed during apply: ${row.path}`);
        if (row.afterSha256 === null) {
          await readJournal(root, bundle);
          await fs.unlink(path.join(root, row.path));
          await syncDirectory(path.dirname(path.join(root, row.path)));
        } else {
          await atomicWrite(path.join(root, row.path), row.payload, journal.entries[index].mode, path.join(root, STATE));
        }
      }
      journal.phase = 'installed';
      await saveJournal(root, journal);
      return { status: 'installed', files: bundle.files.length };
    } catch (error) {
      try { journal.phase = 'applying'; await undo(root, bundle, journal); }
      catch { fail('Apply interrupted; guarded rollback could not complete. Stop all writers and use remove to recover'); }
      throw error;
    }
  } finally { await unlock(); }
}
