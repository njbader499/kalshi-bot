"""Structured JSON logs with rotation and secret redaction (stdlib only)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_STD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_REDACT_KEYS = re.compile(r"(signature|private|secret|token|key_id|access-key|password)", re.I)
_REDACT_INLINE = re.compile(
    r"(KALSHI-ACCESS-(?:SIGNATURE|KEY)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I
)
_TG_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")  # Telegram bot token inside URLs


def _redact(key: str, value: object) -> object:
    if _REDACT_KEYS.search(key):
        return "<redacted>"
    if isinstance(value, str):
        return _REDACT_INLINE.sub(r"\1<redacted>", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _TG_TOKEN.sub(
                "bot<redacted>", _REDACT_INLINE.sub(r"\1<redacted>", record.getMessage())
            ),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                out[k] = _redact(k, v)
        if record.exc_info:
            out["exc"] = _REDACT_INLINE.sub(r"\1<redacted>", self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging(level: str, log_dir: Path | None, max_bytes: int, backup_count: int) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = JsonFormatter()
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "bot.jsonl", maxBytes=max_bytes, backupCount=backup_count
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    # httpx logs full URLs at INFO; keep it at WARNING to reduce noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
