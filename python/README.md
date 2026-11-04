# WP-Bench Harness

This package provides the Python CLI (`wp-bench`) that orchestrates dataset loading, model prompting, and interaction with the WordPress runtime. See the repository root `README.md` for full instructions, or install directly:

```bash
pip install -e ./python
```

For a Docker-free runtime, install Node.js 24.18+ and run
`npm ci --prefix runtime/playground` from the repository root, then set
`grader.kind: playground` in your YAML. See the root README for supported
artifacts, isolation differences, and integration checks.
