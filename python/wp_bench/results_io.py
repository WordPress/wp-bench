"""Durable result artifacts: timestamped paths and streamed records.

Persistence, deliberately kept out of the orchestrator: ``records.py``
owns record *shape* and stays pure in-memory, ``output.py`` renders to the
console, and this module is the only place that turns records into files.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self

import orjson

from .config import OutputConfig
from .utils import ensure_dir


def timestamped_path(path: Path, moment: datetime) -> Path:
    """Add a timestamp to a filename: results.json -> results_20231216_143052.json

    The moment is required so every artifact of one run (results JSON,
    streamed JSONL) shares a single timestamp; ``open_run_artifacts``
    captures it once for both.
    """
    timestamp = moment.strftime("%Y%m%d_%H%M%S")
    return path.parent / f"{path.stem}_{timestamp}{path.suffix}"


def _line(record: dict[str, Any]) -> bytes:
    """One JSONL line, the single serialization used by live and final writes."""
    return orjson.dumps(record) + b"\n"


class RecordStream:
    """Streams canonical records to disk as tests complete.

    Results are otherwise durable only once a run finishes, so a crash
    hours in (dead runtime, exhausted provider quota, Ctrl-C) discards
    every test graded so far. Streamed lines are the same canonical
    records the finished file holds, so a partial run stays readable by
    the notebook, the export tooling, and anything else consuming results.

    Live records go to ``<artifact>.partial``; ``finalize`` writes the
    canonical artifact and drops the partial. A crashed run therefore
    leaves a file whose name says it is incomplete, so nobody computes a
    suite score from half a run.

    The file opens on the first record, so a run that fails before
    grading anything leaves no artifact behind at all.

    Threading: callers write under their own lock; the handle is not
    itself thread-safe. ``MultiModelRunner`` shares one stream across its
    per-model runners, which hold separate locks -- safe only because
    models run sequentially.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.partial_path = path.with_suffix(path.suffix + ".partial") if path else None
        self._fd: int | None = None
        self._written = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release the descriptor however the run ends.

        Owning the lifecycle here is what keeps every run mode from having
        to re-thread its own close on each crash path.
        """
        self.close()

    def write(self, record: dict[str, Any]) -> None:
        """Append one record, so ``tail -f`` shows live progress.

        Each record goes out as one unbuffered ``os.write`` to an O_APPEND
        descriptor. Records embed whole completions and generated code, so
        they routinely exceed a stream buffer; a buffered write plus flush
        would let a kill land between the underlying syscalls and truncate
        a line mid-record. Readers of a live file should still tolerate a
        short final line: only the process that dies mid-write can leave
        one, and the records before it stay valid.

        No fsync: the threat model is process death (crash, quota, Ctrl-C,
        OOM kill), where handing the bytes to the OS is enough.
        """
        if self.partial_path is None:
            return
        if self._fd is None:
            ensure_dir(self.partial_path.parent)
            self._fd = os.open(self.partial_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(self._fd, _line(record))
        self._written += 1

    def close(self) -> None:
        """Release the descriptor; safe to call on crash paths and twice."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def finalize(self, records: list[dict[str, Any]]) -> None:
        """Write the canonical artifact and retire the live file.

        The artifact is staged beside its destination and moved into
        place, so an interrupted finalize leaves no half-written results.
        The partial is removed only once the artifact demonstrably covers
        it: a caller that finalizes with fewer records than were streamed
        has lost track of some, and keeping the partial keeps them
        recoverable instead of deleting them.

        No records means no artifact, the same invariant ``write`` keeps by
        opening lazily.
        """
        self.close()
        if self.path is None or self.partial_path is None or not records:
            return
        ensure_dir(self.path.parent)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("wb") as handle:
                for record in records:
                    handle.write(_line(record))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if len(records) >= self._written:
            # Retire the live file first: a crash between these two steps
            # can then only lose the duplicate, never leave a ".partial"
            # sitting next to a finished artifact and lying about it.
            self.partial_path.unlink(missing_ok=True)
        os.replace(tmp, self.path)


class ResultsReadError(ValueError):
    """A path could not be read as a results payload."""


def read_results_payload(path: Path) -> dict[str, Any]:
    """Read a results JSON this module wrote.

    The read side lives here so payload-shape knowledge stays in the one
    module that writes it, rather than being asserted again by whoever
    consumes an earlier run.
    """
    try:
        payload = orjson.loads(path.expanduser().read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise ResultsReadError(f"Cannot read results file {path}: {exc}") from exc
    if not isinstance(payload, dict) or "models" not in payload:
        raise ResultsReadError(f"Not a results payload: {path}")
    return payload


def write_results_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a run's results JSON atomically.

    Staged and moved into place like the JSONL, so a kill mid-write cannot
    leave a truncated results file that parses as valid JSON to nobody.
    """
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def open_stream(jsonl_path: Path | None, moment: datetime) -> RecordStream:
    """The run's record stream, timestamped to match its JSON artifact."""
    return RecordStream(timestamped_path(jsonl_path, moment) if jsonl_path else None)


def open_run_artifacts(output: OutputConfig) -> tuple[Path, RecordStream]:
    """The run's results-JSON path and record stream, stamped with one moment.

    Capturing the timestamp here makes the artifacts correlate by filename
    by construction, instead of every runner threading a start time to two
    call sites.
    """
    moment = datetime.now(timezone.utc)
    json_path = timestamped_path(output.path, moment)
    stream = open_stream(output.jsonl_path, moment)
    suffix = 2
    while _artifacts_taken(json_path, stream):
        # Second granularity, so a sweep script or a supervisor restart can
        # launch two runs into the same names; they would then append into
        # one .partial and clobber each other at finalize.
        json_path = _suffixed(timestamped_path(output.path, moment), suffix)
        stream = RecordStream(
            _suffixed(timestamped_path(output.jsonl_path, moment), suffix)
            if output.jsonl_path
            else None
        )
        suffix += 1
    return json_path, stream


def _suffixed(path: Path, suffix: int) -> Path:
    return path.parent / f"{path.stem}-{suffix}{path.suffix}"


def _artifacts_taken(json_path: Path, stream: RecordStream) -> bool:
    """Whether another run already owns either of this run's filenames."""
    if json_path.exists():
        return True
    return bool(
        stream.path and (stream.path.exists() or (stream.partial_path and stream.partial_path.exists()))
    )
