"""Durable result artifacts: timestamped paths and streamed records.

Persistence, deliberately kept out of the orchestrator: ``records.py``
owns record *shape* and stays pure in-memory, ``output.py`` renders to the
console, and this module is the only place that turns records into files.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

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
        self._handle: IO[bytes] | None = None
        self._written = 0

    def write(self, record: dict[str, Any]) -> None:
        """Append one record and flush, so ``tail -f`` shows live progress.

        Flush, not fsync: the threat model is process death (crash, quota,
        Ctrl-C, OOM kill), where handing bytes to the OS is enough. Opened
        for append so reopening after a close can never discard what an
        earlier handle already wrote.
        """
        if self.partial_path is None:
            return
        if self._handle is None:
            ensure_dir(self.partial_path.parent)
            self._handle = self.partial_path.open("ab")
        self._handle.write(_line(record))
        self._handle.flush()
        self._written += 1

    def close(self) -> None:
        """Release the handle; safe to call on crash paths and twice."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

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
        os.replace(tmp, self.path)
        if len(records) >= self._written:
            self.partial_path.unlink(missing_ok=True)


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
    return timestamped_path(output.path, moment), open_stream(output.jsonl_path, moment)
