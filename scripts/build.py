"""Stage 7 — build the published artifacts.

Emits four things from the committed JSON:

  ca-law-v<N>.sqlite  the full corpus; also the Cloudflare D1 import (D1 is SQLite)
  research.jsonl      records shaped for Prose's LanceDB research corpus
  court-data.json     Prose's court_data.rs Manifest, requirements filled per county
  manifest.json       artifact index with sha256 for each, served from R2

The two Prose artifacts deliberately match what the app already reads, so the
desktop side needs no new schema:
  * research.jsonl matches research_store.py's table
    {id, title, source_url, jurisdiction, topic, text} — the sidecar embeds it,
  * court-data.json keeps the bundled courthouse directory untouched and only
    replaces `requirements`, which today covers 3 of 58 counties.

    python scripts/build.py --version 2026.08.28
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date

from common import DATA, DIST, SCHEMA, read_json, sha256_bytes, write_json

# Where Prose's bundled baseline lives, so a rebuild preserves the hand-built
# courthouse/branch directory instead of dropping it.
PROSE_COURT_DATA = "../ill-legal/src-tauri/resources/court-data.json"


def load_counties() -> list[dict]:
    return [read_json(p) for p in sorted((DATA / "counties").glob("*.json"))]


def current_rules(county: dict) -> list[dict]:
    return [r for r in county["rules"] if r["effective_to"] is None]


def build_sqlite(counties: list[dict], sources: list[dict], version: str, path):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript((SCHEMA / "db.sql").read_text())

    # Insertion order follows the foreign keys: counties, then the sources that
    # reference them, then the rules that reference both.
    for county in counties:
        con.execute(
            "INSERT INTO counties (id,name,court_name,local_rules_effective_from,extraction_status) "
            "VALUES (?,?,?,?,?)",
            (county["id"], county["county"], county["court_name"],
             county["local_rules_effective_from"], county["extraction_status"]),
        )
    for source in sources:
        con.execute(
            "INSERT OR REPLACE INTO sources (id,county_id,kind,url,title,sha256,bytes,"
            "content_type,r2_key,retrieved_at,effective_from,effective_to) "
            "VALUES (:id,:county_id,:kind,:url,:title,:sha256,:bytes,:content_type,"
            ":r2_key,:retrieved_at,:effective_from,:effective_to)",
            source,
        )

    for county in counties:
        for rule in county["rules"]:
            con.execute(
                "INSERT INTO rules (id,rule_key,parent_id,county_id,authority_level,citation,"
                "rule_number,label,title,text,topics_json,effective_from,effective_to,"
                "source_id,page,extraction_method,confidence,review_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rule["id"], rule["rule_key"], rule["parent_id"], rule["county_id"],
                 rule["authority_level"], rule["citation"], rule["rule_number"], rule["label"],
                 rule["title"], rule["text"], json.dumps(rule["topics"]), rule["effective_from"],
                 rule["effective_to"], rule["source_id"], rule["page"],
                 rule["extraction_method"], rule["confidence"], rule["review_status"]),
            )
        for req in county.get("requirements", []):
            con.execute(
                "INSERT INTO requirements (id,rule_id,county_id,topic,requirement_type,actor,"
                "action,description,trigger_event,deadline_amount,deadline_unit,"
                "deadline_direction,exceptions_json,applies_to_json,confidence,review_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (req["id"], req["rule_id"], req["county_id"], req["topic"],
                 req["requirement_type"], req.get("actor"), req.get("action"),
                 req["description"], req.get("trigger_event"), req.get("deadline_amount"),
                 req.get("deadline_unit"), req.get("deadline_direction"),
                 json.dumps(req.get("exceptions", [])), json.dumps(req.get("applies_to", [])),
                 req.get("confidence"), req["review_status"]),
            )
        for cite in county.get("citations", []):
            con.execute(
                "INSERT OR IGNORE INTO citations (from_rule_id,relation,to_kind,to_ref) "
                "VALUES (?,?,?,?)",
                (cite["from_rule_id"], cite["relation"], cite["to_kind"], cite["to_ref"]),
            )

    # External-content FTS index has to be populated explicitly.
    con.execute("INSERT INTO rules_fts(rowid, title, text) SELECT rowid, title, text FROM rules")
    con.execute("INSERT INTO corpus_meta (key,value) VALUES ('version',?)", (version,))
    con.execute("INSERT INTO corpus_meta (key,value) VALUES ('built_at',?)", (date.today().isoformat(),))
    con.commit()
    con.close()


def dump_sql(db_path) -> str:
    """A replayable SQL dump for `wrangler d1 import`.

    Not sqlite3's iterdump: that emits the FTS5 shadow tables (rules_fts_data,
    _idx, _docsize) as ordinary tables and orders their INSERTs before the
    CREATE VIRTUAL TABLE, so the dump does not replay. Dump the real tables only
    and rebuild the index from them at the end, which is also smaller.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    # No BEGIN TRANSACTION/COMMIT: D1 rejects explicit transaction statements
    # in an import and batches the file itself.
    parts = [(SCHEMA / "db.sql").read_text()]
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'rules_fts%' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        )
    ]
    for table in tables:
        for row in con.execute(f"SELECT * FROM {table}"):
            columns = ",".join(row.keys())
            values = ",".join(
                "NULL" if v is None
                else str(v) if isinstance(v, (int, float))
                else "'" + str(v).replace("'", "''") + "'"
                for v in tuple(row)
            )
            parts.append(f"INSERT INTO {table} ({columns}) VALUES ({values});")
    parts.append(
        "INSERT INTO rules_fts(rowid, title, text) SELECT rowid, title, text FROM rules;"
    )
    con.close()
    return "\n".join(parts) + "\n"


def build_research_jsonl(counties: list[dict], sources: dict[str, dict], path):
    """Records for Prose's shared LanceDB research corpus.

    `jurisdiction` must be the county name exactly as Prose stores it on a case
    (src/lib/counties.ts), because query_research filters on equality.
    """
    lines = []
    for county in counties:
        for rule in current_rules(county):
            source = sources.get(rule["source_id"], {})
            url = source.get("url", "")
            lines.append(
                json.dumps(
                    {
                        "id": rule["id"],
                        "title": f"{rule['citation']} — {rule['title']}",
                        "source_url": f"{url}#page={rule['page']}" if rule["page"] else url,
                        "jurisdiction": county["county"],
                        "topic": (rule["topics"] or ["local-rule"])[0],
                        "text": rule["text"],
                    },
                    ensure_ascii=False,
                )
            )
    path.write_text("\n".join(lines) + "\n")
    return len(lines)


def build_court_data(counties: list[dict], version: str, base_url: str,
                     corpus_sha256: str, path):
    """Prose's court_data.rs Manifest, with per-county requirements filled in.

    The bundled courthouse/branch directory is preserved verbatim — it is
    hand-built address data this pipeline has no better source for. Only
    `requirements` is regenerated.
    """
    base = read_json(DATA.parent / PROSE_COURT_DATA, None)
    if base is None:
        print(f"warning: {PROSE_COURT_DATA} not found; emitting counties as an empty map",
              file=sys.stderr)
        base = {"counties": {}, "requirements": {}}

    # Start from what is already there and overwrite per county only where the
    # pipeline actually produced entries. A rebuild before a county has been
    # extracted must not delete hand-written requirements for it — that would
    # silently make the app's checklist worse with every build.
    requirements = dict(base.get("requirements", {}))
    for county in counties:
        rules = {r["id"]: r for r in county["rules"]}
        forms_by_rule: dict[str, set[str]] = {}
        for cite in county.get("citations", []):
            if cite["to_kind"] == "form":
                forms_by_rule.setdefault(cite["from_rule_id"], set()).add(cite["to_ref"])

        entries = []
        seen: set[tuple[str, str]] = set()
        for req in county.get("requirements", []):
            # Only requirements that name a form can drive Prose's checklist,
            # which is keyed by form id.
            for form_id in sorted(forms_by_rule.get(req["rule_id"], ())):
                kind = "required" if req["requirement_type"] in (
                    "mandatory_form", "mandatory_participation", "filing_deadline"
                ) else "recommended"
                key = (form_id, kind)
                if key in seen:
                    continue
                seen.add(key)
                rule = rules[req["rule_id"]]
                entries.append(
                    {
                        "form_id": form_id,
                        "kind": kind,
                        "note": f"{req['description']} ({rule['citation']})",
                    }
                )
        if entries:
            requirements[county["county"]] = entries

    base["version"] = version
    # court_data::refresh() fetches this URL and parses the response *as a
    # Manifest*, so it must point at the hosted copy of this very file — not at
    # dist/manifest.json, which is the artifact index and a different shape.
    base["manifest_url"] = f"{base_url}/court-data.json"
    # What check_corpus_update() downloads. Without this the app never picks up
    # a corpus revision between releases.
    base["corpus"] = {
        "version": version,
        "sha256": corpus_sha256,
        "url": f"{base_url}/research.jsonl",
    }
    base["requirements"] = requirements
    write_json(path, base)
    return sum(len(v) for v in requirements.values())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=date.today().strftime("%Y.%m.%d"))
    ap.add_argument("--base-url", default="https://corpus.example.invalid",
                    help="public R2 base URL the app fetches artifacts from")
    args = ap.parse_args(argv)

    DIST.mkdir(parents=True, exist_ok=True)
    counties = load_counties()
    if not counties:
        print("error: no county data — run scripts/parse.py", file=sys.stderr)
        return 1
    source_list = read_json(DATA / "sources.json", []) or []
    sources = {s["id"]: s for s in source_list}

    db_path = DIST / f"ca-law-v{args.version}.sqlite"
    build_sqlite(counties, source_list, args.version, db_path)
    research_path = DIST / "research.jsonl"
    n_research = build_research_jsonl(counties, sources, research_path)
    # court-data.json carries the corpus checksum, so research.jsonl is hashed first.
    corpus_sha256 = sha256_bytes(research_path.read_bytes())
    court_path = DIST / "court-data.json"
    n_reqs = build_court_data(counties, args.version, args.base_url.rstrip("/"),
                              corpus_sha256, court_path)

    # D1 imports SQL text, not a .sqlite file, so dump alongside the database.
    sql_path = DIST / "corpus.sql"
    sql_path.write_text(dump_sql(db_path))

    artifacts = []
    for path in (db_path, sql_path, research_path, court_path):
        raw = path.read_bytes()
        artifacts.append(
            {
                "name": path.name,
                "url": f"{args.base_url}/{path.name}",
                "sha256": sha256_bytes(raw),
                "bytes": len(raw),
            }
        )
    write_json(DIST / "manifest.json", {"version": args.version, "artifacts": artifacts})

    print(f"corpus v{args.version}")
    for a in artifacts:
        print(f"  {a['name']:<32} {a['bytes'] // 1024:>6} KB  {a['sha256'][:16]}")
    print(f"  {n_research} research records, {n_reqs} Prose requirement entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
