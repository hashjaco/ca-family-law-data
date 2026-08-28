"""Stage 5 — validate.

Everything that must be true before a corpus change can merge. Runs in CI as a
required check, so a bad extraction blocks the PR rather than reaching an app.

The checks that matter most are the ones the model can violate:
  * source_quote must be a verbatim substring of the rule it came from — this is
    what makes "extracted from the source, not recalled" a checkable claim
    rather than a hope,
  * topics and requirement types must come from the closed vocabulary,
  * no county-local record may claim authority over statewide law,
  * nothing may be 'verified' while its confidence is below threshold.

    python scripts/validate.py            # exit 1 on any error
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter

from common import DATA, read_json, topics as load_topics

CONFIDENCE_FLOOR = 0.8
COUNTY_LOCAL_LEVEL = 40


def _normalize(text: str) -> str:
    """Whitespace-insensitive compare: PDF extraction inserts line breaks mid-sentence."""
    return re.sub(r"\s+", " ", text).strip().lower()


def validate_county(county: dict, tax: dict) -> list[str]:
    errors: list[str] = []
    topic_slugs = {t["slug"] for t in tax["topics"]}
    req_types = set(tax["requirement_types"])
    levels = {a["level"] for a in tax["authority_levels"]}
    name = county["id"]

    rules = {r["id"]: r for r in county["rules"]}
    if len(rules) != len(county["rules"]):
        dupes = [k for k, n in Counter(r["id"] for r in county["rules"]).items() if n > 1]
        errors.append(f"{name}: duplicate rule ids: {', '.join(dupes)}")

    for rule in county["rules"]:
        rid = rule["id"]
        if not rule["text"].strip():
            errors.append(f"{name}/{rid}: empty text")
        if not rule.get("source_id"):
            errors.append(f"{name}/{rid}: no source_id — every record must trace to hashed bytes")
        if rule["authority_level"] not in levels:
            errors.append(f"{name}/{rid}: unknown authority_level {rule['authority_level']}")
        if unknown := set(rule.get("topics", [])) - topic_slugs:
            errors.append(f"{name}/{rid}: topics outside the taxonomy: {', '.join(sorted(unknown))}")
        if rule["effective_to"] and rule["effective_from"] >= rule["effective_to"]:
            errors.append(f"{name}/{rid}: effective_from {rule['effective_from']} is not before "
                          f"effective_to {rule['effective_to']}")
        if rule["review_status"] == "verified" and (rule.get("confidence") or 1.0) < CONFIDENCE_FLOOR:
            errors.append(f"{name}/{rid}: marked verified below the confidence floor")

    # Overlapping effective ranges for one rule_key would make a point-in-time
    # query ambiguous — two different answers to "what applied that day".
    by_key: dict[str, list[dict]] = {}
    for rule in county["rules"]:
        by_key.setdefault(rule["rule_key"], []).append(rule)
    for key, group in by_key.items():
        group = sorted(group, key=lambda r: r["effective_from"])
        for earlier, later in zip(group, group[1:]):
            if earlier["effective_to"] is None or earlier["effective_to"] > later["effective_from"]:
                errors.append(f"{name}/{key}: overlapping effective ranges "
                              f"({earlier['id']} and {later['id']})")
        open_ended = [r for r in group if r["effective_to"] is None]
        if len(open_ended) > 1:
            errors.append(f"{name}/{key}: {len(open_ended)} versions claim to be current")

    for req in county.get("requirements", []):
        rid = req["id"]
        rule = rules.get(req["rule_id"])
        if rule is None:
            errors.append(f"{name}/{rid}: references unknown rule {req['rule_id']}")
            continue
        if req["topic"] not in topic_slugs:
            errors.append(f"{name}/{rid}: unknown topic {req['topic']!r}")
        if req["requirement_type"] not in req_types:
            errors.append(f"{name}/{rid}: unknown requirement_type {req['requirement_type']!r}")
        # The core anti-hallucination check.
        quote = req.get("source_quote", "")
        if not quote:
            errors.append(f"{name}/{rid}: no source_quote")
        elif _normalize(quote) not in _normalize(rule["text"]):
            errors.append(f"{name}/{rid}: source_quote is not verbatim in {rule['id']} "
                          f"— extracted rather than quoted: {quote[:60]!r}")
        if (req.get("deadline_amount") is None) != (req.get("deadline_unit") is None):
            errors.append(f"{name}/{rid}: deadline amount and unit must be set together")
        if req["review_status"] == "verified" and (req.get("confidence") or 0) < CONFIDENCE_FLOOR:
            errors.append(f"{name}/{rid}: verified below the confidence floor "
                          f"({req.get('confidence')})")
        if rule["authority_level"] >= COUNTY_LOCAL_LEVEL and req["requirement_type"] == "prohibition":
            # A county rule may add local procedure; it cannot forbid what
            # statewide law permits. Flag for a human rather than publish it.
            if req["review_status"] == "verified":
                errors.append(f"{name}/{rid}: a county-local prohibition cannot be auto-verified")

    for cite in county.get("citations", []):
        if cite["from_rule_id"] not in rules:
            errors.append(f"{name}: citation from unknown rule {cite['from_rule_id']}")
    return errors


def main() -> int:
    tax = load_topics()
    paths = sorted((DATA / "counties").glob("*.json"))
    if not paths:
        print("error: no county data — run scripts/parse.py", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    totals = Counter()
    for path in paths:
        county = read_json(path)
        errors = validate_county(county, tax)
        all_errors.extend(errors)
        totals["rules"] += len(county["rules"])
        totals["requirements"] += len(county.get("requirements", []))
        totals["needs_review"] += sum(
            1 for r in county.get("requirements", []) if r["review_status"] == "needs_review"
        )
        status = f"{len(errors)} errors" if errors else "ok"
        print(f"  {path.stem}: {len(county['rules'])} rules, "
              f"{len(county.get('requirements', []))} requirements — {status}")

    for error in all_errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"\n{totals['rules']} rules, {totals['requirements']} requirements, "
          f"{totals['needs_review']} awaiting review, {len(all_errors)} errors")
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
