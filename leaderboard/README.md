# Leaderboard results

`results/` holds raw `wp-bench run` outputs, copied from `output/` unchanged:
`results_<timestamp>.json` (the run payload) and, when available, the matching
`results_<timestamp>.jsonl` (streamed per-test records). Nothing else belongs in
this folder. The timestamp is when the run started, so a run's JSON and JSONL share
one name.

A payload is either a single-model run (`metadata` with `model`, `scores`, `usage`
plus per-test `results`) or a multi-model run (`metadata` plus `models`, one entry
per model with its `config`, `scores` and `results`). The leaderboard page reads
both: it lists the folder, takes every model from every run, and shows the latest
run per model (older runs stay visible with "Show all runs"). Who published a run,
and when, is the file's git history, which the page shows as well.

## Adding a run

1. Check the model does not already have a run (see `AGENTS.md`, "Leaderboard
   results").
2. Copy the run output in:

   ```sh
   cp output/results_<timestamp>.json output/results_<timestamp>.jsonl leaderboard/results/
   ```

3. Run `pytest python -k leaderboard`, commit with a message that links the
   discussion, and open a PR.

## Leaderboard page

`site/index.html` is the static leaderboard page, hosted on Spacefast at
https://wpbench-leaderboard.space.fast/ and deployed from this folder through the
Space's Git connection. It has no build step or backend: it lists `results/` through
the GitHub contents API, reads each run's `metadata`, and attributes each run to
the commit that added it. The History tab walks the commits that touched the folder
and diffs the leaderboard between any two of them. The data source defaults to
`WordPress/wp-bench@trunk` and falls back to the PR branch while it is open;
override with `?repo=owner/name&ref=branch`.
