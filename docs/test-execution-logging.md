# Test Execution Logging

WP-Bench includes a test execution logging feature that tracks when each test starts, completes, or encounters errors. This is particularly useful for debugging hanging tests and understanding performance characteristics of your benchmark runs.

## Overview

The logging system uses a callback-based architecture that logs test lifecycle events in real-time:
- **START**: When a test begins execution
- **COMPLETE**: When a test finishes successfully (with score and duration)
- **ERROR**: When a test encounters an error

## Configuration

Enable test logging in your `wp-bench.yaml` configuration file:

```yaml
output:
  path: output/results.json
  jsonl_path: output/results.jsonl
  enable_test_logging: true
  test_log_path: output/test_execution.log
```

**Note:** The log file will be automatically timestamped to match the results files:
- Single model: `test_execution_20260131_142305.log`
- Multiple models: `test_execution_gpt-4o_20260131_142305.log`, `test_execution_claude-opus-4-5_20260131_142308.log`, etc.

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enable_test_logging` | bool | `false` | Enable/disable test execution logging |
| `test_log_path` | Path | `test_execution.log` | Base path for the log file (timestamp added automatically) |

### Log File Naming

Log files are automatically timestamped to match the results files, making it easy to correlate logs with benchmark results:

**Single Model Runs:**
```
results_20260131_142305.json       # Results
test_execution_20260131_142305.log # Log (same timestamp)
```

**Multi-Model Runs:**

Each model gets its own log file with the model name and timestamp:
```
results_20260131_142305.json                        # Combined results
test_execution_gpt-4o_20260131_142305.log          # gpt-4o logs
test_execution_claude-opus-4-5_20260131_142308.log # claude logs (slightly later timestamp)
```

Model names with special characters (`/`, `:`) are sanitized (replaced with `-`) for filesystem compatibility.

## Log Format

Each log entry follows this format:

```
TIMESTAMP.milliseconds | Thread-ID | EVENT | test_type | test_id | details
```

### Example Log Output

```log
2026-01-31 14:23:45.123 | Thread-12345 | START | knowledge | wp-001 | model=gpt-4o-mini
2026-01-31 14:23:47.456 | Thread-12345 | COMPLETE | knowledge | wp-001 | model=gpt-4o-mini | score=1.0000 | duration_ms=2333.1
2026-01-31 14:23:47.500 | Thread-12346 | START | execution | wp-exec-001 | model=gpt-4o-mini
2026-01-31 14:24:15.234 | Thread-12346 | COMPLETE | execution | wp-exec-001 | model=gpt-4o-mini | score=0.8750 | duration_ms=27734.2
2026-01-31 14:24:15.300 | Thread-12347 | START | knowledge | wp-002 | model=gpt-4o-mini
2026-01-31 14:24:16.100 | Thread-12347 | ERROR | knowledge | wp-002 | model=gpt-4o-mini | error=TimeoutError: Request timeout
```

### Log Fields

- **TIMESTAMP**: When the event occurred (YYYY-MM-DD HH:MM:SS.mmm)
- **Thread-ID**: The thread ID executing the test (useful for parallel execution tracking)
- **EVENT**: One of `START`, `COMPLETE`, or `ERROR`
- **test_type**: Either `knowledge` or `execution`
- **test_id**: Unique identifier for the test
- **model**: The model being benchmarked
- **score**: Test score (0.0 to 1.0, only in COMPLETE events)
- **duration_ms**: Test execution duration in milliseconds (only in COMPLETE events)
- **error**: Error type and message (only in ERROR events)

## Use Cases

### 1. Debugging Hanging Tests

If your benchmark run hangs, watch the log file to identify which test is stuck:

```bash
# Watch the log in real-time
tail -f output/test_execution.log
```

If a test hangs, you'll see a START entry without a corresponding COMPLETE entry:

```log
2026-01-31 14:23:47.500 | Thread-12346 | START | execution | wp-exec-042 | model=gpt-4o-mini
# ... no COMPLETE line appears for wp-exec-042
```

To find hanging tests after a run:

```bash
# Extract all test IDs that started
grep "START" output/test_execution.log | awk '{print $8}' | sort > started.txt

# Extract all test IDs that completed
grep "COMPLETE" output/test_execution.log | awk '{print $8}' | sort > completed.txt

# Find tests that started but didn't complete
comm -23 started.txt completed.txt
```

### 2. Performance Analysis

Identify slow tests by analyzing duration:

```bash
# Find tests that took longer than 30 seconds (30000ms)
grep "COMPLETE" output/test_execution.log | \
  awk -F'|' '{print $5}' | \
  awk '{if ($3 > 30000) print $0}'
```

Sort tests by duration:

```bash
grep "COMPLETE" output/test_execution.log | \
  awk -F'|' '{print $5}' | \
  sort -t'=' -k4 -n
```

### 3. Error Analysis

Find all tests that encountered errors:

```bash
grep "ERROR" output/test_execution.log
```

Count errors by type:

```bash
grep "ERROR" output/test_execution.log | \
  awk -F'error=' '{print $2}' | \
  cut -d':' -f1 | \
  sort | uniq -c
```

### 4. Multi-Model Comparison

When running multiple models, the log includes all models in a single file:

```bash
# Count tests per model
grep "COMPLETE" output/test_execution.log | \
  awk -F'model=' '{print $2}' | \
  cut -d' ' -f1 | \
  sort | uniq -c
```

Average duration per model:

```bash
for model in gpt-4o gpt-4o-mini; do
  echo -n "$model: "
  grep "model=$model" output/test_execution.log | \
    grep "COMPLETE" | \
    awk -F'duration_ms=' '{print $2}' | \
    awk '{sum+=$1; count++} END {print sum/count " ms"}'
done
```

## Real-Time Monitoring

Monitor test execution progress in real-time:

```bash
# Watch the log file
tail -f output/test_execution.log

# Or filter for specific events
tail -f output/test_execution.log | grep "ERROR"

# Or monitor a specific test type
tail -f output/test_execution.log | grep "execution"
```

## Thread Safety

The logging system is fully thread-safe and designed to work with WP-Bench's parallel test execution. Each log entry includes the thread ID, allowing you to trace execution across multiple concurrent threads.

## Performance Impact

The logging system has minimal performance overhead:
- Logs are written asynchronously with immediate flush for real-time visibility
- No buffering delays or blocking operations
- Negligible impact on test execution time

## Troubleshooting

### Log file not created

Ensure the output directory exists or enable automatic directory creation:

```yaml
output:
  test_log_path: /path/to/output/test_execution.log
```

The parent directory will be created automatically if it doesn't exist.

### Logs appear delayed

Logs are flushed immediately after each event, but if you're piping through other commands, those commands might buffer. Use `tail -f` for real-time viewing.

### Multiple model runs

When running multiple models sequentially, all logs are appended to the same file with the model name included in each entry. To separate logs per model, you can either:

1. Parse the log file by model name:
   ```bash
   grep "model=gpt-4o" output/test_execution.log > gpt-4o.log
   ```

2. Or run models separately with different log files.

## Architecture

The logging system uses a callback-based architecture:

- **TestCallback**: Abstract base class defining the callback interface
- **FileLoggerCallback**: Writes events to a log file with immediate flush
- **ConsoleLoggerCallback**: Available for debugging (prints to stdout)

The callback is integrated at the runner level and invoked at three points in the test lifecycle:
1. Before test execution (`on_test_start`)
2. After successful completion (`on_test_complete`)
3. On error (`on_test_error`)

This design keeps logging separate from test execution logic and allows for easy extension (e.g., adding metrics collection, database logging, etc.).
