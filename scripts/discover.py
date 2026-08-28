"""Stage 1 — discover.

The Judicial Branch publishes a single index of every county's local rules, with
the current effective date next to each. That index is the only authoritative
starting point; everything downstream hangs off it.

Writes sources/manifest.json. County court sites differ wildly in layout, so the
per-county `rules_url` this produces is a starting point that a human is expected
to correct by hand where a court buries its family-law rules behind a portal.
Hand edits survive: existing non-empty fields are never overwritten.

    python scripts/discover.py
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone

import httpx

from common import SOURCES, USER_AGENT, read_json, slug, write_json

INDEX_URL = "https://courts.ca.gov/forms-rules/rules-court/local-rules"

# "Alameda County (Eff. July 1, 2026)" -> ("Alameda", date(2026, 7, 1))
_ENTRY = re.compile(
    r"^(?P<name>.+?)\s+County\s*\(\s*Eff\.\s*(?P<eff>[A-Z][a-z]+ \d{1,2}, \d{4})\s*\)\s*$"
)


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def parse_index(html: str) -> list[dict]:
    """Pull (county, effective date, url) out of the index table."""
    out: list[dict] = []
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S):
        href = re.search(r'href="([^"]+)"', cell)
        text = _strip_tags(cell)
        m = _ENTRY.match(text)
        if not (href and m):
            continue
        eff = datetime.strptime(m.group("eff"), "%B %d, %Y").date()
        name = m.group("name").strip()
        out.append(
            {
                "id": slug(name),
                "name": name,
                "court_name": f"Superior Court of California, County of {name}",
                "rules_url": href.group(1),
                "forms_url": None,
                "fcs_url": None,
                "local_rules_effective_from": eff.isoformat(),
            }
        )
    return out


def merge(existing: list[dict], found: list[dict]) -> list[dict]:
    """Refresh what the index owns; never clobber hand-added URLs."""
    by_id = {c["id"]: dict(c) for c in existing}
    for entry in found:
        cur = by_id.get(entry["id"], {})
        for key, value in entry.items():
            # The index is authoritative for the effective date and the county's
            # own name; for everything else a hand-supplied value wins.
            if key in ("local_rules_effective_from", "name", "id", "court_name") or not cur.get(key):
                cur[key] = value
        by_id[entry["id"]] = cur
    return sorted(by_id.values(), key=lambda c: c["id"])


def main() -> int:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
        resp = client.get(INDEX_URL)
        resp.raise_for_status()
    found = parse_index(resp.text)
    if len(found) < 58:
        print(f"error: index yielded {len(found)} counties, expected 58 — layout changed?", file=sys.stderr)
        return 1

    path = SOURCES / "manifest.json"
    prior = read_json(path, {}) or {}
    manifest = {
        "index_url": INDEX_URL,
        "discovered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counties": merge(prior.get("counties", []), found),
    }
    write_json(path, manifest)
    print(f"discovered {len(found)} counties -> {path.relative_to(path.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
