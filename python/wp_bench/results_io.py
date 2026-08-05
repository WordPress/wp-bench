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

from .utils import ensure_dir


def timestamped_path(path: Path, moment: datetime | None = None) -> Path:
    """Add a timestamp to a filename: results.json -> results_20231216_143052.json

    Runners pass their start time so every artifact of one run (results
    JSON, streamed JSONL) shares a single timestamp.
    """
    timestamp = (moment or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    return path.parent / f"{path.stem}_{timestamp}{path.suffix}"


def _line(record: dict[str, Any]) -> bytes:
    """One JSONL line, the single serialization used by live and final writes."""
    return orjson.dumps(record) + b"\n"


class RecordStream:
    """Appends canonical records to the JSONL file as tests complete.

    Results are otherwise durable only once a run finishes, so a crash
    hours in (dead runtime, exhausted provider quota, Ctrl-C) discards
    every test graded so far. Streamed lines are the same canonical
    records the finished file holds, so a partial run stays readable by
    the notebook, the export tooling, and anything else consuming results.

    The file opens on the first record, so a run that fails before
    grading anything leaves no empty artifact behind.

    Threading: callers write under their own lock; the handle is not
    itself thread-safe. ``MultiModelRunner`` shares one stream across its
    per-model runners, which hold separate locks -- safe only because
    models run sequentially.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._handle: IO[bytes] | None = None

    def write(self, record: dict[str, Any]) -> None:
        """Append one record and flush, so ``tail -f`` shows live progress.

        Flush, not fsync: the threat model is process death (crash, quota,
        Ctrl-C, OOM kill), where handing bytes to the OS is enough.
        """
        if self.path is None:
            return
        if self._handle is None:
            ensure_dir(self.path.parent)
            self._handle = self.path.open("wb")
        self._handle.write(_line(record))
        self._handle.flush()

    def close(self) -> None:
        """Release the handle; safe to call on crash paths and twice."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def finalize(self, records: list[dict[str, Any]]) -> None:
        """Replace the streamed file with the canonical ordered records.

        In flight the file grows in completion order so it can be tailed;
        the finished artifact carries the run's canonical order, keeping
        result diffs stable. The replacement is written beside the live
        file and moved into place, so an interrupted finalize can never
        destroy the streamed records it is replacing.

        No records means no artifact, the same invariant ``write`` keeps by
        opening lazily; leaving a streamed file untouched also beats
        truncating it, should a caller ever finalize with less than it
        streamed.
        """
        self.close()
        if self.path is None or not records:
            return
        ensure_dir(self.path.parent)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            for record in records:
                handle.write(_line(record))
        os.replace(tmp, self.path)


def open_stream(jsonl_path: Path | None, moment: datetime) -> RecordStream:
    """The run's record stream, timestamped to match its JSON artifact."""
    return RecordStream(timestamped_path(jsonl_path, moment) if jsonl_path else None)
