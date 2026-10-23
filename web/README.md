# Leaderboard data adapter

The Spacefast frontend consumes display data built from the repository's public
benchmark releases. It does not assign benchmark versions or own category rules.

First validate complete result JSONs with `wp-bench benchmark export` (see
[../benchmark/README.md](../benchmark/README.md)). Then build display data:

```sh
pip install -e './python[leaderboard]'
PYTHONPATH=python python web/build_results.py \
  --export public-data/v1/SNAPSHOT.json \
  --catalog model-catalog.json --source-commit COMMIT_SHA \
  --output-dir SITE_SOURCE/data
```

The small editorial catalog supplies only names, provider labels, generation
labels, and explicit predecessor pairs:

```json
{"models":{"provider/model-id":{"label":"Model name","provider":"OpenAI","generation":"Current"}},"pairs":[{"label":"Model family","previous":"older-model-id","current":"provider/model-id"}]}
```

New models without editorial entries still appear under their API ID. Comparison
claims are made only for explicitly paired, complete models on the same frozen
benchmark. Broad categories, task membership, protocol and the public version
come exclusively from the repository manifest. The adapter verifies the export
fingerprint and complete repeated task coverage before calculating scores.

The generated `YYYY.MM.DD.HASH/` files are immutable display-data snapshots. The
website's catalog selects the latest snapshot within each **public benchmark
version**; visitors see WP-Bench v1/v2 and an update date, while snapshot IDs remain
in downloads. Keep prior snapshots and their benchmark manifest. Never substitute
an old-version score for a missing row in a new benchmark.

This adapter has no campaign-specific paths, provider credentials, or model API
calls. Raw completions remain in private run artifacts. Publish larger scored
answer bundles separately from the frontend source when retention grows; the
frontend only needs compact summaries and stable links to those downloads.

Future display data identifies cost estimates as harness-recorded usage; it does
not silently claim those values were normalized to uncached price tables. Missing
latency/cost stays unavailable rather than appearing as zero.
