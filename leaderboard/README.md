# Leaderboard results

`results/` holds one JSON file per benchmarked model, named after the LiteLLM
model id with `/` replaced by `__` (`gemini/gemini-3-flash-preview` →
`results/gemini__gemini-3-flash-preview.json`). Each file is the harness output
of that model's run, exactly as `wp-bench run` writes `output/results_<timestamp>.json`
for a single-model run: a `metadata` block (suite, model config, grader, dataset,
scoring version, usage, scores, selected and errored test ids) and the per-test
`results` records.

Who ran a model, and when, is the git history: re-running a model overwrites its
file, so `git log -- leaderboard/results` (and the leaderboard page's History tab)
shows every change with its author, date and commit message. Put the context a
reader needs — run date, link to the discussion, caveats — in the commit message.

Every file holds a complete run with its per-test `results`. The figures posted in
[issue #5](https://github.com/WordPress/wp-bench/issues/5) are not carried over:
they have no per-test records and mix suite sizes and scoring eras. To list a model
from that thread, re-run it and add its output.

## Adding or updating a model

1. Check the model does not already have a file (see `AGENTS.md`, "Leaderboard
   results").
2. Copy the run output into place. Single- and multi-model outputs both work; one
   file is written per model (multi-model runs are split into single-model payloads):

   ```sh
   python leaderboard/build_entry.py output/results_<timestamp>.json
   ```

3. Run `pytest python -k leaderboard`, commit with a message that says when the run
   happened and links the discussion, and open a PR.

## Leaderboard page

`site/index.html` is the static leaderboard page, hosted on Spacefast at
https://wpbench-leaderboard.space.fast/ and deployed from this folder through the
Space's Git connection. It has no build step or backend: it lists `results/`
through the GitHub contents API, reads each file's `metadata`, and attributes each
row to the last commit that touched its file. The History tab walks the commits
that touched the folder and diffs any two versions. The data source defaults to
`WordPress/wp-bench@trunk` and falls back to the PR branch while it is open;
override with `?repo=owner/name&ref=branch`.
