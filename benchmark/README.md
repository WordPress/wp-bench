# Public benchmark releases

**WP-Bench v1** identifies one complete, frozen evaluation. `wp-core` and `wp-cli`
are stable component IDs. They have no independent public versions. The Python
package, result JSON schema, and scoring formula have internal version numbers;
those are reproducibility details, not additional leaderboard versions.

`definition.json` is the working evaluation recipe. `releases.json` indexes the
append-only `releases/vN.json` manifests. A release freezes every dataset file,
complete test membership, runtime and relevant harness source, WordPress target,
scoring rules, broad category mapping, approved runtime images, generation
settings, and required repeats. The manifest fingerprint covers the entire
recipe. Changing any dataset changes the overall evaluation fingerprint.

| Change | Release handling |
| --- | --- |
| Add/remove/edit a dataset task, including a corrected assertion | Next benchmark version |
| Change grading, task weighting, prompting, or the evaluation protocol | Next benchmark version |
| Change runtime behavior or the approved runtime images | Next benchmark version |
| Evaluate another model on the same recipe | Updated results under the same version |
| Re-run the same models under that recipe | Another results snapshot under the same version |
| Edit documentation, website layout, or result serialization | Same benchmark version |

Source fingerprinting is deliberately conservative. Even a dataset formatting or
editorial-only edit changes its frozen file hash. Batch source changes during
development rather than publishing a version for every commit. A changed checkout
can run normally, but records `status: unreleased` and no public version. A new
release is an explicit maintainer action; it is never inferred from directory
names, a date, or a model list.

## Running a release

Inspect the complete protocol before spending on providers:

```sh
wp-bench benchmark show --version v1
wp-bench benchmark check
```

An explicit assertion in the run config refuses mismatching sources, loaded
local/Hub tasks, model settings, or runtime settings before model generation:

```yaml
benchmark:
  version: v1

dataset:
  source: local
  name: wp-core

model:
  name: YOUR_MODEL_ID
  temperature: 1
  max_tokens: 16384

grader:
  kind: docker
  # Use one of runtime_image_ids from the frozen manifest, not an unverified tag.
  image: sha256:6dcab391357137b2ce52d99ef15f342499b6b9f3287e5b96946036628a7dfb1e

run:
  suite: wp-core
  execution_isolation: reset_per_test
```

Run both components and the required independent repeats. A subset remains
useful for diagnostics but cannot enter the overall leaderboard. With Hugging
Face, pin `dataset.revision` to the published export commit and select the stable
suite ID. Loaded task fingerprints must match the frozen release regardless of
transport. The historical Hub repository name `WordPress/wp-bench-v1` is an
address, not a separate dataset-version authority; publish the new stable suite
IDs there before using that export.

Every completed payload carries `metadata.benchmark`; each streamed record also
carries the benchmark identity, task fingerprint and `run_id`. Multi-model
payloads retain a separate identity and run ID in each `models` entry. These
fields survive partial runs, but publication requires actual complete records.
A `full_suite` flag alone never authorizes a score. Source identity is captured
before generation, so an edit during a run cannot retroactively relabel it.

## Publishing a new benchmark version

1. Batch intended evaluation changes in `definition.json`, datasets, and code.
2. Run the normal Python/dataset checks and applicable reference/exploit audits.
3. Freeze the next version, then inspect the generated manifest:

   ```sh
   wp-bench benchmark freeze v2
   wp-bench benchmark check --base-ref origin/trunk
   ```

4. Commit the new manifest and catalog in the release PR. Existing manifests
   cannot be edited, removed, or reassigned. CI compares their bytes with the base
   commit, even if someone recomputes the embedded hashes.
5. After merging, tag the release commit `benchmark-v2` and retain its approved
   runtime images. To reproduce an old version, check out its tag; selecting an
   old version alone does not restore its source into a changed checkout.

If a changed file does not affect evaluation behavior, review that deliberately.
The default fingerprint policy favors refusing an accidental comparison over
silently declaring two evaluations equivalent. No file change should be hidden
by modifying an old manifest.

## Website data contract

Use the harness exporter, rather than recreating release numbers in the website:

```sh
wp-bench benchmark export --version v1 --output-dir public-data \
  --input core-model-a-repeat-1.json --input core-model-a-repeat-2.json \
  --input cli-model-a-repeat-1.json --input cli-model-a-repeat-2.json
```

Supply the complete set of models desired in the new snapshot. Single- and
multi-model result JSONs are supported. The exporter checks the release identity
on both envelopes and individual records, task fingerprints, exact suite
membership, approved runtime image, model/grader settings, baseline policy,
unique run IDs, and the required independent repeats. Reference solutions,
skills variants, errored/partial runs, duplicate repeats and mixed versions fail
before output is created. A legitimate failed model answer is still a scored
answer and remains in the denominator.

The output is a small, stable ingestion contract, not a website theme:

```text
public-data/
  results.json                # mutable index, grouped by benchmark version
  v1/<snapshot-id>.json        # immutable scored-answer snapshots
  v2/<snapshot-id>.json
```

Snapshot schema `1.0` contains `benchmark.version`, `benchmark.fingerprint`, the
release-owned broad category labels, model IDs, per-answer pass/usage data, run
IDs, input hashes, and an update date. It excludes generated code and raw provider
responses. Every answer has a stable component ID, test ID and broad category.
Aggregate pass rates weight every task equally and average the same number of
repeats per task. Use task-paired uncertainty estimates when comparing models.

The website should expose **one benchmark-version selector** and an **updated
date**. Snapshot IDs and internal schemas belong in downloads/details. A model
addition creates a new immutable snapshot under v1; changing any dataset creates
v2 and its own leaderboard. Preserve v1 scores and files. Never fill missing v2
rows with v1 scores. Cross-version charts must disclose test changes and cannot
establish model improvement by themselves.

The first release includes the already-audited four core assertion corrections
and native read-only `wp db columns` allowance behind the initial public scores.
Renaming component IDs and removing per-file versions changes no prompts or
behavioral checks beyond those recorded corrections. Historical files without
the new identity must be explicitly audited/imported for the first release;
the exporter never guesses that an old `*-v1` suite name proves compatibility.
