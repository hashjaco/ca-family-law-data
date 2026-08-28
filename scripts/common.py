"""Shared helpers: paths, slugs, polite HTTP, hashing."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SCHEMA = ROOT / "schema"
SOURCES = ROOT / "sources"
DIST = ROOT / "dist"
CACHE = ROOT / "cache"

# Identifies the crawler to court webmasters. Court rules are public documents,
# but a bot hitting 58 county sites should still say who it is.
USER_AGENT = (
    "ca-family-law-data/0.1 (+https://github.com/hashjaco/ca-family-law-data; "
    "public court rules ingestion; contact via repo issues)"
)

# One request at a time per host, with a gap. Nothing here is urgent.
_HOST_DELAY_S = 1.5
_last_hit: dict[str, float] = {}


def slug(name: str) -> str:
    """'Contra Costa' -> 'contra-costa'. Stable primary key for a county."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def throttle(url: str) -> None:
    host = urlparse(url).netloc
    gap = time.monotonic() - _last_hit.get(host, 0.0)
    if gap < _HOST_DELAY_S:
        time.sleep(_HOST_DELAY_S - gap)
    _last_hit[host] = time.monotonic()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def topics() -> dict:
    return json.loads((SCHEMA / "topics.json").read_text())
