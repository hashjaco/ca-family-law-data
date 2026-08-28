"""Stage 3 — deterministic parse.

PDF -> a tree of rule records with page numbers and exact source spans. Nothing
here interprets law; it only recovers the structure the court already published.
The less this stage leaves for the model, the fewer ways the corpus can be wrong.

Scope note (deliberate): this parses to *rule* level, not subdivision level. The
five pilot counties use four mutually incompatible subdivision conventions --
Alameda "(a)/(1)/(A)" on their own lines, Los Angeles "(a)" inline, Santa Clara
"A./b./(iii)", San Diego "A./4 ." -- and a deterministic splitter that survives
all 58 would be guesswork. Subdivision text stays inside rules.text, and the
extraction stage cites the subdivision label it actually read. rules.parent_id
stays in the schema for the statewide Rules of Court, which are well-formed.
ponytail: revisit if a county's rules turn out to need subdivision-level anchors.

    python scripts/parse.py                 # every crawled county
    python scripts/parse.py contra-costa
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

import pymupdf

from common import CACHE, DATA, read_json, write_json

# Per-county parse profiles. `heading` must capture groups `num` and optionally
# `title`; a title on the following line is handled when `title_on_next_line`.
# `strip` removes running headers/footers before structure detection.
DEFAULT_STRIP = (
    re.compile(r"^\s*(page\s+)?\d+\s*(of\s+\d+)?\s*$", re.I),
    re.compile(r"^\s*\d+\s*-\s*\d+\s*$"),           # Alameda's "5 - 5"
    re.compile(r"^\s*-\s*[ivxlcdm]+\s*-\s*$", re.I),  # roman-numeral TOC folios
)


@dataclass
class Profile:
    heading: re.Pattern
    title_on_next_line: bool = False
    strip: tuple[re.Pattern, ...] = ()
    # Trailing "(Effective 1/1/2022)" / "(Adopted 1/1/2005; Rev. 1/1/2026)".
    history: re.Pattern | None = None


# A court's own history note, e.g. "(Adopted 1/1/2005; Rev. 1/1/2022; Rev. 1/1/2026)".
# Matched as a whole parenthetical; every date inside it is then collected, because
# the most recent revision is the one that dates the rule.
_HISTORY = re.compile(r"\((?:Adopted|Effective|Eff\.|Rev\.|Renum\.)[^)]*\)", re.I)
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

PROFILES: dict[str, Profile] = {
    # "Rule 5.70.  Guideline for spousal or partner support"
    "alameda": Profile(
        # The mandatory period after the number, plus a capitalised title, is
        # what separates a heading from a trailing history note
        # ("Rule 5.10 amended and renumbered effective January 1, 2008").
        heading=re.compile(r"^Rule\s+(?P<num>\d+\.\d+[A-Za-z]?)\.\s+(?P<title>[A-Z]\S*.*?)\s*$"),
        strip=(re.compile(r"^Local Rules of the Superior Court of California, County of Alameda\s*$"),),
    ),
    # "5.12" then "FAMILY-CENTERED CASE RESOLUTION" on the next line
    "los-angeles": Profile(
        heading=re.compile(r"^(?P<num>\d+\.\d+)\s*$"),
        title_on_next_line=True,
        strip=(
            re.compile(r"^SUPERIOR COURT OF CALIFORNIA\s*$"),
            re.compile(r"^COUNTY OF LOS ANGELES\s*$"),
            re.compile(r"^Local Rules\s*[–-]\s*Effective\b.*$"),
        ),
    ),
    # "RULE 3" then "CHILD, SPOUSAL AND PARTNER SUPPORT"
    "santa-clara": Profile(
        heading=re.compile(r"^RULE\s+(?P<num>\d+[A-Za-z]?)\s*$"),
        title_on_next_line=True,
        strip=(re.compile(r"^Santa Clara County Court Rules\s*$"),),
        history=_HISTORY,
    ),
    # "Rule 5.2.3" then a blank line then the title
    "san-diego": Profile(
        heading=re.compile(r"^Rule\s+(?P<num>\d+(?:\.\d+)+)\s*$"),
        title_on_next_line=True,
        strip=(
            re.compile(r"^Superior Court of California, County of San Diego\s*$"),
            re.compile(r"^Local Rules, Effective\b.*$"),
        ),
        history=_HISTORY,
    ),
    # Combined volume; rules read "Rule 5.17  Family Court Services Appointments"
    "contra-costa": Profile(
        heading=re.compile(r"^Rule\s+(?P<num>\d+\.\d+[A-Za-z]?)\.?\s+(?P<title>[A-Z]\S*.*?)\s*$"),
        history=_HISTORY,
    ),
}

# A table-of-contents line: dot leaders running to a page number.
_TOC_LINE = re.compile(r"\.{4,}\s*\d+\s*$")
# Contra Costa's combined volume: only Title 5 is family/juvenile.
_FAMILY_RULE = re.compile(r"^5\b")


@dataclass
class Rule:
    rule_number: str
    title: str
    page: int
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self.lines)).strip()


def _clean_page(text: str, profile: Profile) -> list[str]:
    """Drop running headers, folios, and table-of-contents lines."""
    out = []
    for raw in text.splitlines():
        line = raw.replace(" ", " ").rstrip()
        if not line.strip():
            out.append("")
            continue
        if _TOC_LINE.search(line):
            continue
        if any(p.match(line.strip()) for p in (*DEFAULT_STRIP, *profile.strip)):
            continue
        out.append(line)
    return out


def _is_toc_page(lines: list[str]) -> bool:
    """A page is front matter if dot-leader entries dominate it."""
    body = [ln for ln in lines if ln.strip()]
    if len(body) < 5:
        return False
    return sum(bool(_TOC_LINE.search(ln)) for ln in body) > len(body) * 0.4


def parse_document(pdf_path, profile: Profile, family_only: bool = False) -> list[Rule]:
    rules: list[Rule] = []
    current: Rule | None = None
    pending_number: tuple[str, int] | None = None

    with pymupdf.open(pdf_path) as doc:
        for pno in range(doc.page_count):
            raw = doc[pno].get_text()
            # TOC detection must run before stripping, which removes the leaders.
            if _is_toc_page(raw.splitlines()):
                continue
            for line in _clean_page(raw, profile):
                stripped = line.strip()
                if pending_number and stripped:
                    number, page = pending_number
                    pending_number = None
                    current = Rule(rule_number=number, title=stripped, page=page)
                    rules.append(current)
                    continue
                m = profile.heading.match(stripped)
                if m:
                    number = m.group("num")
                    if family_only and not _FAMILY_RULE.match(number):
                        current = None
                        continue
                    if profile.title_on_next_line:
                        pending_number = (number, pno + 1)
                    else:
                        current = Rule(
                            rule_number=number,
                            title=(m.groupdict().get("title") or "").strip(),
                            page=pno + 1,
                        )
                        rules.append(current)
                    continue
                if current is not None:
                    current.lines.append(line)
    return [r for r in rules if r.text()]


def rule_effective_from(rule: Rule, profile: Profile, fallback: str) -> str:
    """Prefer the court's own '(Rev. 1/1/2026)' marker over the document date."""
    if profile.history is None:
        return fallback
    parsed = [
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for note in profile.history.findall(rule.text())
        for month, day, year in _DATE.findall(note)
    ]
    return max(parsed) if parsed else fallback


def parse_source(record: dict, county: dict) -> list[dict]:
    profile = PROFILES.get(record["county_id"])
    if profile is None:
        print(f"    - no parse profile for {record['county_id']}", file=sys.stderr)
        return []
    path = CACHE / f"{record['sha256']}.pdf"
    if not path.exists():
        print(f"    ! missing cached bytes for {record['id']}; re-run crawl.py", file=sys.stderr)
        return []

    # A combined county volume carries every division's rules; only Title 5 is
    # family/juvenile. A family-specific document is already scoped.
    family_only = record["county_id"] == "contra-costa"
    rules = parse_document(path, profile, family_only=family_only)

    out = []
    for rule in rules:
        effective_from = rule_effective_from(rule, profile, record["effective_from"])
        out.append(
            {
                "id": f"{record['county_id']}-{rule.rule_number.lower()}@{effective_from}",
                "rule_key": f"{record['county_id']}-{rule.rule_number.lower()}",
                "parent_id": None,
                "county_id": record["county_id"],
                "authority_level": 40,
                "citation": f"{county['name']} County Local Rule {rule.rule_number}",
                "rule_number": rule.rule_number,
                "label": None,
                "title": rule.title,
                "text": rule.text(),
                "topics": [],
                "effective_from": effective_from,
                "effective_to": None,
                "source_id": record["id"],
                "page": rule.page,
                "extraction_method": "deterministic",
                "confidence": None,
                "review_status": "needs_review",
            }
        )
    return out


def main(argv: list[str]) -> int:
    ledger = read_json(DATA / "sources.json", []) or []
    manifest = read_json(__import__("common").SOURCES / "manifest.json") or {}
    counties = {c["id"]: c for c in manifest.get("counties", [])}
    wanted = set(argv) or None

    by_county: dict[str, list[dict]] = {}
    for record in ledger:
        if record["kind"] != "local_rules":
            continue
        if wanted and record["county_id"] not in wanted:
            continue
        print(f"  {record['id']}")
        parsed = parse_source(record, counties[record["county_id"]])
        print(f"    {len(parsed)} rules")
        by_county.setdefault(record["county_id"], []).extend(parsed)

    for county_id, rules in sorted(by_county.items()):
        # A rule's history is one row per edition in which its text actually
        # changed. Editions that reprint a rule verbatim are not new versions —
        # recording them would make every reprint look like an amendment.
        versions: dict[str, list[dict]] = {}
        for rule in rules:
            versions.setdefault(rule["rule_key"], []).append(rule)
        kept: list[dict] = []
        for key, group in versions.items():
            # Two records for one edition means a heading matched twice — almost
            # always table-of-contents residue that survived page-level detection.
            # The substantive copy is the longer one.
            by_date: dict[str, dict] = {}
            for rule in group:
                prior = by_date.get(rule["effective_from"])
                if prior is None or len(rule["text"]) > len(prior["text"]):
                    by_date[rule["effective_from"]] = rule
            group = sorted(by_date.values(), key=lambda r: r["effective_from"])
            history: list[dict] = []
            for rule in group:
                if history and history[-1]["text"] == rule["text"]:
                    continue  # verbatim reprint: keep the earlier effective_from
                history.append(rule)
            for earlier, later in zip(history, history[1:]):
                earlier["effective_to"] = later["effective_from"]
                earlier["review_status"] = "superseded"
            kept.extend(history)
        path = DATA / "counties" / f"{county_id}.json"
        existing = read_json(path, {}) or {}
        write_json(
            path,
            {
                "county": counties[county_id]["name"],
                "id": county_id,
                "court_name": counties[county_id]["court_name"],
                "local_rules_effective_from": counties[county_id]["local_rules_effective_from"],
                "extraction_status": "partial",
                "rules": sorted(kept, key=lambda r: (r["rule_number"], r["effective_from"])),
                "requirements": existing.get("requirements", []),
                "forms": existing.get("forms", []),
                "citations": existing.get("citations", []),
            },
        )
        current = sum(1 for r in kept if r["effective_to"] is None)
        print(f"{county_id}: {len(kept)} rule rows ({current} current) -> data/counties/{county_id}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
