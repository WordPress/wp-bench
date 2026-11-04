// One disposable WordPress VM per candidate. No host directories are mounted.
import { runCLI } from '@wp-playground/cli';
import { readdir, readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';

// Keep the stdout protocol separate from Playground diagnostics.
console.log = console.error;
const lines = createInterface({ input: process.stdin });
const input = lines[Symbol.asyncIterator]();
let site;
try {
  site = await runCLI({
    command: 'server', wp: process.argv[2], php: '8.2',
    port: 0, workers: 1, verbosity: 'quiet', login: false,
    'site-url': process.argv[3],
  });
  // Verification uses the PHP API directly; do not serve the candidate site.
  await new Promise((resolve) => site.server.close(resolve));
  const php = site.playground;
  const root = '/wordpress/wp-content/plugins/wp-bench-runtime';
  await php.mkdirTree(root + '/src');
  for (const name of await readdir(new URL('../src/', import.meta.url))) {
    if (name.endsWith('.php')) {
      await php.writeFile(root + '/src/' + name,
        await readFile(new URL('../src/' + name, import.meta.url)));
    }
  }
  await php.writeFile(root + '/verifier-bootstrap.php',
    await readFile(new URL('../verifier-bootstrap.php', import.meta.url)));
  // The shared verifier's stdin entrypoint is specific to native WP-CLI.
  // Call its verification service directly in the WASM PHP request.
  process.stdout.write('WP_BENCH_READY\n');
  const { value } = await input.next();
  const payload = JSON.parse(value);
  if (payload.artifact_kind === 'wp_cli_shell') {
    throw new Error('Playground does not support Bash/wp_cli_shell artifacts.');
  }
  await php.writeFile('/tmp/wp-bench-payload.json', JSON.stringify(payload));
  const response = await php.run({ code: `<?php
    require '/wordpress/wp-load.php';
    require '${root}/verifier-bootstrap.php';
    $payload = json_decode(file_get_contents('/tmp/wp-bench-payload.json'), true);
    echo wp_json_encode((new \\WPBench\\Runtime\\Verifier())->verify_payload($payload));
  ` });
  process.stdout.write(JSON.stringify({
    stdout: new TextDecoder().decode(response.bytes),
    stderr: response.errors, returncode: response.exitCode,
  }) + '\n');
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  lines.close();
  if (site) await site[Symbol.asyncDispose]();
}
