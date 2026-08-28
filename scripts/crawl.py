"""Stage 2 — fetch and archive.

For each county: load its local-rules landing page, find the rule documents,
download them, hash them, and record provenance in data/sources.json. The bytes
go to cache/<sha256>.<ext> locally and to R2 under the same key in CI.

Two properties this stage exists to guarantee:
  * every downstream record traces to a sha256 of bytes we actually hold, and
  * re-running is cheap and non-destructive (conditional GET, content hashing).

Court sites differ enormously, so discovery is a heuristic with a manual
override: set "documents" on a county in sources/manifest.json and it wins
outright. That is the intended way to handle a court that hides its rules behind
a portal, not a per-county parser.

    python scripts/crawl.py                    # every county in the manifest
    python scripts/crawl.py contra-costa alameda
    python scripts/crawl.py contra-costa --all-versions   # include superseded editions
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from common import CACHE, DATA, SOURCES, USER_AGENT, read_json, sha256_bytes, throttle, write_json

# Anchor text / filename hints that a link is a rules or local-forms document.
# Ordered by how strongly they imply "this is the thing we want".
_HINTS = (
    (re.compile(r"family|fl[- ]?law", re.I), "family"),
    (re.compile(r"local\s*rule", re.I), "local_rules"),
    (re.compile(r"local\s*form", re.I), "local_forms"),
    (re.compile(r"standing\s*order|general\s*order", re.I), "standing_order"),
)
_ROBOTS: dict[str, RobotFileParser] = {}


def _robots_ok(client: httpx.Client, url: str) -> bool:
    """Honour robots.txt. A missing or unreadable robots.txt means allowed."""
    parts = urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    rp = _ROBOTS.get(origin)
    if rp is None:
        rp = RobotFileParser()
        try:
            throttle(origin)
            resp = client.get(f"{origin}/robots.txt", timeout=15)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _ROBOTS[origin] = rp
    return rp.can_fetch(USER_AGENT, url)


def find_documents(html: str, base_url: str) -> list[dict]:
    """Candidate rule documents linked from a county's rules page."""
    found: dict[str, dict] = {}
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, label = m.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        url = urljoin(base_url, href)
        if not url.lower().split("?")[0].endswith(".pdf"):
            continue
        haystack = f"{label} {url}"
        kinds = [kind for pattern, kind in _HINTS if pattern.search(haystack)]
        if not kinds:
            continue
        found.setdefault(
            url,
            {
                "url": url,
                "title": label or None,
                # 'family' is a relevance marker, not a document kind.
                "kind": next((k for k in kinds if k != "family"), "local_rules"),
                "family_specific": "family" in kinds,
            },
        )
    # A county that publishes one combined rules PDF plus family-specific
    # excerpts: keep both, the parser decides which yields Title 5.
    return sorted(found.values(), key=lambda d: (not d["family_specific"], d["url"]))


def current_only(docs: list[dict], effective_from: str) -> list[dict]:
    """Drop superseded editions.

    Several courts (Contra Costa is the worst) link every historical edition
    from the same page. Those are genuinely useful for the effective_to chain,
    but 58 counties' archives do not fit the free R2 tier, so the default crawl
    keeps only documents that name the current effective year. Pass
    --all-versions to backfill history for a specific county.
    """
    year = effective_from[:4]
    current = [d for d in docs if year in f"{d.get('title') or ''} {d['url']}"]
    # If nothing names the year, the court does not date its filenames — keep
    # everything rather than silently dropping the only copy of the rules.
    return current or docs


def prefer_family(docs: list[dict]) -> list[dict]:
    """Narrow to the documents this corpus will actually cite.

    Courts publish in three shapes and the filter has to survive all of them:
      * split by division (Santa Clara: family.pdf, criminal.pdf, probate.pdf;
        San Diego: division_v_-_family_law.pdf) — take the family one,
      * one combined volume (Contra Costa) — take it whole, Title 5 is inside,
      * rules plus a separate local-forms appendix (Alameda) — take both.

    Standing and general orders are dropped by default: in practice they are
    dominated by things like recording-equipment and traffic-fee orders, and a
    50 MB order about courtroom microphones is not family-law authority.
    ponytail: reinstate behind a --standing-orders flag if a county turns out to
    put real family procedure there.
    """
    rules = [d for d in docs if d["kind"] == "local_rules"]
    family_rules = [d for d in rules if d["family_specific"]]
    keep = (family_rules or rules) + [d for d in docs if d["kind"] == "local_forms"]
    return keep or docs


_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_YYYYMMDD = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")


def document_effective_from(doc: dict, county_default: str) -> str:
    """Date a document from its own filename, not from the county's index entry.

    Courts that publish both the January and July editions of a rule set link
    both from the same page; stamping both with the county's current effective
    date makes the superseded edition look current and breaks the effective_to
    chain the whole versioning design rests on.
    """
    haystack = f"{doc.get('title') or ''} {doc['url']}".lower()
    if m := _YYYYMMDD.search(haystack):          # "05-title-5-20260701.pdf"
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    for index, month in enumerate(_MONTHS, start=1):  # "july-2026-local-rules"
        if m := re.search(rf"{month}[-_ ]+(20\d{{2}})", haystack):
            return f"{m.group(1)}-{index:02d}-01"
    return county_default


def fetch(client: httpx.Client, url: str) -> tuple[bytes, str] | None:
    throttle(url)
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"    ! {url}: {e}", file=sys.stderr)
        return None
    return resp.content, resp.headers.get("content-type", "").split(";")[0]


def crawl_county(
    client: httpx.Client, county: dict, known: dict[str, dict], all_versions: bool = False
) -> list[dict]:
    print(f"  {county['id']}")
    docs = county.get("documents")
    if not docs:
        if not _robots_ok(client, county["rules_url"]):
            print("    ! robots.txt disallows the rules page; set 'documents' by hand", file=sys.stderr)
            return []
        page = fetch(client, county["rules_url"])
        if page is None:
            return []
        docs = find_documents(page[0].decode("utf-8", "replace"), county["rules_url"])
        if not all_versions:
            docs = prefer_family(current_only(docs, county["local_rules_effective_from"]))
        if not docs:
            print("    - no PDF links found; needs a manual 'documents' entry", file=sys.stderr)

    records: list[dict] = []
    for doc in docs:
        if not _robots_ok(client, doc["url"]):
            print(f"    ! robots.txt disallows {doc['url']}", file=sys.stderr)
            continue
        got = fetch(client, doc["url"])
        if got is None:
            continue
        body, content_type = got
        digest = sha256_bytes(body)
        ext = "pdf" if doc["url"].lower().split("?")[0].endswith(".pdf") else "html"
        blob = CACHE / f"{digest}.{ext}"
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(body)

        prior = known.get(doc["url"])
        record = {
            "id": f"src_{county['id']}_{doc['kind']}_{digest[:12]}",
            "county_id": county["id"],
            "kind": doc["kind"],
            "url": doc["url"],
            "title": doc.get("title"),
            "sha256": digest,
            "bytes": len(body),
            "content_type": content_type or None,
            "r2_key": f"sources/{digest}.{ext}",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "effective_from": document_effective_from(doc, county["local_rules_effective_from"]),
            "effective_to": None,
        }
        if prior and prior["sha256"] != digest:
            print(f"    ~ changed: {doc['url']} ({prior['sha256'][:12]} -> {digest[:12]})")
        elif prior:
            # Unchanged bytes: keep the original retrieval timestamp so the
            # ledger records when we first saw this exact document.
            record["retrieved_at"] = prior["retrieved_at"]
        else:
            print(f"    + {doc['url'].rsplit('/', 1)[-1]} ({len(body) // 1024} KB)")
        records.append(record)
    return records


def main(argv: list[str]) -> int:
    manifest = read_json(SOURCES / "manifest.json")
    if not manifest:
        print("error: run scripts/discover.py first", file=sys.stderr)
        return 1
    all_versions = "--all-versions" in argv
    wanted = {a for a in argv if not a.startswith("--")} or None
    counties = [c for c in manifest["counties"] if not wanted or c["id"] in wanted]
    if wanted and len(counties) != len(wanted):
        missing = wanted - {c["id"] for c in counties}
        print(f"error: unknown counties: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    ledger_path = DATA / "sources.json"
    ledger = read_json(ledger_path, []) or []
    known = {r["url"]: r for r in ledger}

    print(f"crawling {len(counties)} counties")
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60
    ) as client:
        for county in counties:
            for record in crawl_county(client, county, known, all_versions):
                known[record["url"]] = record

    write_json(ledger_path, sorted(known.values(), key=lambda r: (r["county_id"] or "", r["url"])))
    print(f"{len(known)} sources in ledger -> data/sources.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
