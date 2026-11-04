from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "config.example.yaml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "ENV",
        "LOCATION_OK",
        "I_ACCEPT_REAL_MONEY_RISK",
        "KALSHI_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH",
        "TELEGRAM_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def example_cfg() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    shutil.copy(EXAMPLE, p)
    return p
