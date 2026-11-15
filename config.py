"""Centralised configuration loaded from environment variables / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else BASE_DIR / p


ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()

DB_PATH: Path = _path("ERP_DB_PATH", "data/erp.db")
ERP_IMPORT_DIR: Path = _path("ERP_IMPORT_DIR", "erp_import")
SAMPLES_DIR: Path = BASE_DIR / "samples"
MOCK_PAYLOADS_DIR: Path = SAMPLES_DIR / "mock_payloads"
SAMPLE_PDFS_DIR: Path = SAMPLES_DIR / "pdfs"

PRICE_TOLERANCE: float = float(os.getenv("PRICE_TOLERANCE", "0.01"))  # 1 %
MATCH_CONFIDENCE_THRESHOLD: float = float(os.getenv("MATCH_CONFIDENCE_THRESHOLD", "85"))
# Two catalog candidates closer than this many points are considered ambiguous.
AMBIGUITY_MARGIN: float = float(os.getenv("AMBIGUITY_MARGIN", "4"))


def demo_mode() -> bool:
    """Demo/Mock mode is active whenever no API key is configured."""
    return not ANTHROPIC_API_KEY
