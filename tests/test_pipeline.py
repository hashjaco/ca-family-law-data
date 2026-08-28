"""Checks for the logic that is easy to get quietly wrong.

Run: .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import crawl  # noqa: E402
import discover  # noqa: E402
import parse  # noqa: E402


# --- discover ---------------------------------------------------------------

def test_index_entry_parsing():
    html = """
    <td><a href="https://x/rules">Contra Costa County (Eff. July 1, 2026)</a></td>
    <td><a href="https://y/rules">Alpine County (Eff. January 1, 2025)</a></td>
    <td><a href="https://z/other">Not a county link</a></td>
    """
    found = discover.parse_index(html)
    assert [c["id"] for c in found] == ["contra-costa", "alpine"]
    assert found[0]["local_rules_effective_from"] == "2026-07-01"
    assert found[1]["local_rules_effective_from"] == "2025-01-01"


def test_merge_keeps_hand_added_urls_but_refreshes_effective_date():
    existing = [{"id": "los-angeles", "name": "Los Angeles", "rules_url": "https://hand/set.pdf",
                 "local_rules_effective_from": "2025-01-01", "court_name": "old"}]
    found = [{"id": "los-angeles", "name": "Los Angeles", "court_name": "new",
              "rules_url": "https://spa/portal", "forms_url": None, "fcs_url": None,
              "local_rules_effective_from": "2026-07-01"}]
    merged = discover.merge(existing, found)[0]
    assert merged["rules_url"] == "https://hand/set.pdf"       # hand edit survives
    assert merged["local_rules_effective_from"] == "2026-07-01"  # index is authoritative


# --- crawl ------------------------------------------------------------------

def test_document_effective_from_reads_the_document_not_the_county():
    default = "2026-07-01"
    assert crawl.document_effective_from(
        {"url": "https://x/january-2026-local-rules_0.pdf", "title": None}, default
    ) == "2026-01-01"
    assert crawl.document_effective_from(
        {"url": "https://x/05-title-5-20260701.pdf", "title": None}, default
    ) == "2026-07-01"
    # No date anywhere: fall back to the county's index entry.
    assert crawl.document_effective_from({"url": "https://x/rules.pdf", "title": None}, default) == default


def test_prefer_family_keeps_the_combined_volume_when_nothing_is_family_specific():
    combined = {"url": "a.pdf", "kind": "local_rules", "family_specific": False}
    standing = {"url": "b.pdf", "kind": "standing_order", "family_specific": False}
    # Contra Costa shape: one combined volume plus unrelated standing orders.
    assert crawl.prefer_family([combined, standing]) == [combined]


def test_prefer_family_narrows_to_the_family_division_and_keeps_forms():
    family = {"url": "family.pdf", "kind": "local_rules", "family_specific": True}
    criminal = {"url": "criminal.pdf", "kind": "local_rules", "family_specific": False}
    forms = {"url": "forms.pdf", "kind": "local_forms", "family_specific": False}
    kept = crawl.prefer_family([family, criminal, forms])
    assert kept == [family, forms]


# --- parse ------------------------------------------------------------------

def test_alameda_heading_ignores_citation_history_footers():
    heading = parse.PROFILES["alameda"].heading
    assert heading.match("Rule 5.70.  Guideline for spousal or partner support")
    # The trailing history note must not be mistaken for a new rule.
    assert not heading.match("Rule 5.10 amended and renumbered effective January 1, 2008")


def test_rule_effective_from_prefers_the_courts_own_revision_marker():
    profile = parse.PROFILES["san-diego"]
    rule = parse.Rule(rule_number="5.2.3", title="ADR", page=1,
                      lines=["Text.", "(Adopted 1/1/2005; Rev. 1/1/2022; Rev. 1/1/2026)"])
    assert parse.rule_effective_from(rule, profile, "2026-01-01") == "2026-01-01"
    rule.lines = ["Text with no history marker."]
    assert parse.rule_effective_from(rule, profile, "2020-01-01") == "2020-01-01"


def test_toc_pages_are_skipped():
    toc = ["Rule 5.70.  Spousal support ........................ 13"] * 6
    assert parse._is_toc_page(toc)
    assert not parse._is_toc_page(["Rule 5.70.  Spousal support", "The court may..."] * 4)


# --- point-in-time: the query the versioning design exists for --------------

def test_point_in_time_query_selects_the_edition_in_force():
    con = sqlite3.connect(":memory:")
    con.executescript((ROOT / "schema" / "db.sql").read_text())
    con.execute("INSERT INTO counties VALUES ('contra-costa','Contra Costa','SC',NULL,NULL,NULL,'2026-07-01','{}','partial')")
    con.execute("INSERT INTO sources VALUES ('s1','contra-costa','local_rules','u',NULL,'h',1,NULL,NULL,'2026-08-01','2026-01-01',NULL)")
    rows = [
        ("cc-5.0@2026-01-01", "cc-5.0", "2026-01-01", "2026-07-01", "superseded"),
        ("cc-5.0@2026-07-01", "cc-5.0", "2026-07-01", None, "verified"),
    ]
    for rid, key, eff_from, eff_to, status in rows:
        con.execute(
            "INSERT INTO rules (id,rule_key,county_id,authority_level,citation,rule_number,"
            "title,text,effective_from,effective_to,source_id,extraction_method,review_status) "
            "VALUES (?,?,'contra-costa',40,'c','5.0','t','body',?,?,'s1','deterministic',?)",
            (rid, key, eff_from, eff_to, status),
        )

    def as_of(day: str) -> str:
        return con.execute(
            "SELECT id FROM rules WHERE rule_key='cc-5.0' AND effective_from <= ?1 "
            "AND (effective_to IS NULL OR effective_to > ?1)", (day,)
        ).fetchone()[0]

    assert as_of("2026-03-15") == "cc-5.0@2026-01-01"   # hearing held in March
    assert as_of("2026-08-28") == "cc-5.0@2026-07-01"   # today
    # Boundary: the new edition takes effect on its own start date.
    assert as_of("2026-07-01") == "cc-5.0@2026-07-01"


# --- corpus invariants over whatever is currently committed -----------------

def test_committed_counties_satisfy_schema_invariants():
    topics = {t["slug"] for t in json.loads((ROOT / "schema" / "topics.json").read_text())["topics"]}
    files = sorted((ROOT / "data" / "counties").glob("*.json"))
    assert files, "no county data parsed yet"
    for path in files:
        county = json.loads(path.read_text())
        ids = [r["id"] for r in county["rules"]]
        assert len(ids) == len(set(ids)), f"{path.name}: duplicate rule ids"
        for rule in county["rules"]:
            assert rule["text"].strip(), f"{path.name}: {rule['id']} has empty text"
            assert rule["source_id"], f"{path.name}: {rule['id']} has no provenance"
            assert 10 <= rule["authority_level"] <= 90
            assert set(rule["topics"]) <= topics, f"{path.name}: {rule['id']} has unknown topics"
            if rule["effective_to"]:
                assert rule["effective_from"] < rule["effective_to"]
