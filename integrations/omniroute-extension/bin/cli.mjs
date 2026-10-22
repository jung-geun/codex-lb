#!/usr/bin/env node
import { run } from '../lib/index.mjs';

const help = `Usage: omniroute-subscriptions check|apply|remove --target <path>
       omniroute-subscriptions --help

Offline build-time source extension for pinned OmniRoute 3.8.52 only.
check   Read-only compatibility and installation status; conflicts exit 1.
apply   Verify all files, then journal and install the complete extension.
remove  Restore owned source, or recover an interrupted transaction.

Stop the gateway and all source/build writers before apply or remove.
Rebuild and restart normally afterward. Database/data rollback is not performed.
`;

const args = process.argv.slice(2);
if (args.length === 1 && (args[0] === '--help' || args[0] === '-h')) {
  process.stdout.write(help);
} else if (args.length !== 3 || !['check', 'apply', 'remove'].includes(args[0]) || args[1] !== '--target' || !args[2] || args[2].startsWith('--')) {
  process.stderr.write(help);
  process.exitCode = 2;
} else {
  try {
    const result = await run(args[0], { target: args[2] });
    process.stdout.write(`${result.status}: ${result.files} owned source files\n`);
  } catch (error) {
    // Filesystem diagnostics can include operator paths. Never dump file bytes or stacks.
    const detail = typeof error.code === 'string' ? `Filesystem operation failed (${error.code})` : error.message;
    process.stderr.write(`omniroute-subscriptions: ${detail}\n`);
    process.exitCode = 1;
  }
}
