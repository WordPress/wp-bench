"""Test execution callbacks for logging and monitoring."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional


class TestCallback(ABC):
    """Base class for test execution callbacks."""

    def on_test_start(self, test_id: str, test_type: str, model: str) -> None:
        """Called when a test starts execution."""
        pass

    def on_test_complete(
        self, test_id: str, test_type: str, model: str, score: float, duration_ms: float
    ) -> None:
        """Called when a test completes successfully."""
        pass

    def on_test_error(
        self, test_id: str, test_type: str, model: str, error: Exception
    ) -> None:
        """Called when a test fails with an error."""
        pass


class FileLoggerCallback(TestCallback):
    """Logs test lifecycle events to a file in real-time."""

    def __init__(self, log_path: Path, model_name: str):
        self.log_path = log_path
        self.model_name = model_name
        self._setup_logger()

    def _setup_logger(self) -> None:
        """Configure logger with immediate flush for real-time output."""
        self.logger = logging.getLogger(f"wp_bench.tests.{self.model_name}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        # Remove existing handlers
        self.logger.handlers.clear()

        # Create handler with immediate flush
        handler = logging.FileHandler(self.log_path, mode="a")
        handler.setLevel(logging.INFO)

        # Format: timestamp | thread | event | test_type | test_id | details
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d | Thread-%(thread)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)

        # Disable buffering for real-time output
        handler.flush = lambda: handler.stream.flush()

        self.logger.addHandler(handler)

    def on_test_start(self, test_id: str, test_type: str, model: str) -> None:
        """Log test start with thread ID."""
        self.logger.info(f"START | {test_type} | {test_id} | model={model}")
        # Force flush
        for handler in self.logger.handlers:
            handler.flush()

    def on_test_complete(
        self, test_id: str, test_type: str, model: str, score: float, duration_ms: float
    ) -> None:
        """Log test completion with score and duration."""
        self.logger.info(
            f"COMPLETE | {test_type} | {test_id} | model={model} | score={score:.4f} | duration_ms={duration_ms:.1f}"
        )
        # Force flush
        for handler in self.logger.handlers:
            handler.flush()

    def on_test_error(
        self, test_id: str, test_type: str, model: str, error: Exception
    ) -> None:
        """Log test error."""
        self.logger.error(
            f"ERROR | {test_type} | {test_id} | model={model} | error={type(error).__name__}: {error}"
        )
        # Force flush
        for handler in self.logger.handlers:
            handler.flush()


class ConsoleLoggerCallback(TestCallback):
    """Logs test lifecycle to console (useful for debugging)."""

    def on_test_start(self, test_id: str, test_type: str, model: str) -> None:
        print(f"[{datetime.now().isoformat()}] START: {test_type}/{test_id}", flush=True)

    def on_test_complete(
        self, test_id: str, test_type: str, model: str, score: float, duration_ms: float
    ) -> None:
        print(
            f"[{datetime.now().isoformat()}] COMPLETE: {test_type}/{test_id} (score={score:.2f}, {duration_ms:.0f}ms)",
            flush=True,
        )

    def on_test_error(
        self, test_id: str, test_type: str, model: str, error: Exception
    ) -> None:
        print(
            f"[{datetime.now().isoformat()}] ERROR: {test_type}/{test_id} - {error}",
            flush=True,
        )
