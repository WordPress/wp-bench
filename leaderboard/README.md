# Leaderboard

Published WP-Bench runs and the leaderboard built from them live in a separate
repository so that multi-megabyte run outputs stay out of this one:

- **Leaderboard:** https://wpbench-leaderboard.space.fast/
- **Results repository:** https://github.com/joaopamaral/wp-bench-results

That repository stores each run's raw `wp-bench run` output (`results_<timestamp>.json`
plus `.jsonl`) unchanged under `results/`, and builds the leaderboard page from them.
Its README explains how to publish a run; in short: check the model is not already
listed for the suite, copy the two output files from `output/` into `results/`,
commit, rebuild the page data with `python3 site/build_data.py`, and open a pull
request there.
