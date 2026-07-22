"""Telemetry capture — the gated observer.

One JSON file per observed step is written into captures/ (numbered,
descriptively named — the same "one file per event" idea as ebb-agent-data-
access's eda_logs/ and finmap's recordings/).

The whole thing is gated by a single `enabled` flag, modelled on eda's
MessageLogWriter(enabled=...). While telemetry is OFF — e.g. during the manual
auth walkthrough — `record()` is a no-op: nothing is observed, nothing is
written. It is a *passive* observer. Turning it off means we simply don't look;
it never records-then-discards, so credentials entered during auth can't leak
into the capture log.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def slugify(text: str) -> str:
    """Filename-safe slug (letters/digits/dashes), capped for the filesystem."""
    slug = re.sub(r"[\s_]+", "-", text.lower())
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80] or "step"


class Capture:
    """Gated, one-file-per-step telemetry writer.

    Constructed disabled by default so the browser can be connected and driven
    through auth with nothing recorded. `enable()` opens the gate (and lazily
    prepares the output dir, picking up where any prior run left off).
    """

    def __init__(self, out_dir: Path, enabled: bool = False) -> None:
        self.out_dir = out_dir
        self.enabled = enabled
        self.sequence = 0
        if enabled:
            self._prepare()

    def _prepare(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sequence = self._detect_sequence()

    def _detect_sequence(self) -> int:
        """Continue numbering after the highest existing step file."""
        max_seq = 0
        for path in self.out_dir.glob("*.json"):
            head = path.stem.split("-", 1)[0]
            if head.isdigit():
                max_seq = max(max_seq, int(head))
        return max_seq

    def enable(self) -> None:
        if not self.enabled:
            self.enabled = True
            self._prepare()
            logger.info("telemetry_enabled", out_dir=str(self.out_dir))

    def disable(self) -> None:
        if self.enabled:
            self.enabled = False
            logger.info("telemetry_disabled")

    def record(self, label: str, payload: dict[str, Any]) -> str | None:
        """Write one step file and return its path.

        No-op while telemetry is off — returns None without touching disk.
        """
        if not self.enabled:
            return None
        self.sequence += 1
        filename = f"{self.sequence:05d}-{slugify(label)}.json"
        path = self.out_dir / filename
        data = {
            "sequence": self.sequence,
            "label": label,
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.info("step_recorded", sequence=self.sequence, label=label, path=str(path))
        return str(path)
