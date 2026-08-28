"""Stage 4 — structured extraction.

Turns parsed rule text into `requirements`, `topics`, and `citations`: the
machine-actionable layer. "A Request for Order must be filed 16 court days
before the hearing" becomes a record an application can compute a date from,
instead of 400 words an application has to re-read every time.

Three rules this stage is built around, because its errors are the expensive
kind — a wrong deadline is a missed hearing for someone representing themselves:

  1. The model extracts FROM the rule text it is given. It never recalls law.
     Every requirement must quote the span it came from, and validate.py checks
     that quote actually appears in the source text.
  2. Nothing lands as 'verified'. Everything is 'needs_review' until a human
     merges the PR.
  3. Topics and requirement types are closed vocabularies from schema/topics.json.
     Anything outside them fails validation rather than inventing a category.

Batch API by default: this is entirely non-latency-sensitive and batch is half
price. Pass --sync to run inline when iterating on the prompt.

Credentials: `ANTHROPIC_API_KEY` if set (this is what CI uses), otherwise the
macOS keychain — see `keychain_key()`. Nothing is read from a file in the repo,
so there is no plaintext key to leak into a commit.

    python scripts/extract.py contra-costa
    python scripts/extract.py --sync --limit 5 contra-costa   # prompt iteration
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from anthropic import Anthropic

from common import DATA, read_json, topics as load_topics, write_json

# Sonnet 5 over Haiku 4.5 is about ten dollars across all 58 counties. The output
# of this stage is filing deadlines and mandatory appearances, so the accuracy is
# worth more than the saving. Override with EXTRACT_MODEL if you disagree.
MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5")
BATCH_POLL_S = 30

KEYCHAIN_SERVICE = "ca-family-law-data"
KEYCHAIN_ACCOUNT = "anthropic_api_key"


def keychain_key() -> str | None:
    """Read the API key from the macOS keychain.

    Store it once, interactively (the flag prompts; the value never reaches your
    shell history, a file, or a terminal transcript):

        security add-generic-password -s ca-family-law-data -a anthropic_api_key -w

    Returns None anywhere that is not macOS or where no item exists, so CI falls
    through to ANTHROPIC_API_KEY.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def build_client() -> Anthropic:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Anthropic()          # SDK reads the env var itself
    if key := keychain_key():
        return Anthropic(api_key=key)
    raise SystemExit(
        "No Anthropic credential found.\n"
        "  Local:  security add-generic-password -s ca-family-law-data "
        "-a anthropic_api_key -w\n"
        "  CI:     set the ANTHROPIC_API_KEY repo secret"
    )

SYSTEM = """You extract structured procedural requirements from California county \
superior court local rules.

You are given the verbatim text of ONE local rule. Extract only what that text \
actually states. You must not use outside knowledge of California law, and you \
must not infer requirements the text does not state. If the rule states no \
actionable requirement, return an empty requirements list — that is a normal and \
correct answer for a definitional or introductory rule.

For every requirement you extract, `source_quote` must be a verbatim substring of \
the rule text, copied exactly, that states the requirement. It is checked \
programmatically against the source; a paraphrase fails.

Deadlines: record the number and its unit exactly as the rule states them. \
California distinguishes court days from calendar days and the difference matters. \
If the rule does not state a deadline, leave the deadline fields null rather than \
estimating one.

`subdivision` is the label of the subdivision the requirement comes from as \
printed in the text — "(b)(2)", "A.1", "(iii)" — or null if the rule has no \
subdivisions.

Set confidence below 0.8 whenever the rule's language is conditional, ambiguous, \
cross-references a document you were not given, or you are unsure."""


def schema(topic_slugs: list[str], requirement_types: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["topics", "requirements", "citations"],
        "properties": {
            "topics": {
                "type": "array",
                "description": "Topics this rule concerns. Closed vocabulary.",
                "items": {"type": "string", "enum": topic_slugs},
            },
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "topic", "requirement_type", "description",
                        "source_quote", "subdivision", "confidence",
                    ],
                    "properties": {
                        "topic": {"type": "string", "enum": topic_slugs},
                        "requirement_type": {"type": "string", "enum": requirement_types},
                        "actor": {"type": ["string", "null"]},
                        "action": {"type": ["string", "null"]},
                        "description": {"type": "string"},
                        "trigger_event": {"type": ["string", "null"]},
                        "deadline_amount": {"type": ["integer", "null"]},
                        "deadline_unit": {
                            "type": ["string", "null"],
                            "enum": ["calendar_days", "court_days", "hours", "months", None],
                        },
                        "deadline_direction": {
                            "type": ["string", "null"], "enum": ["before", "after", None]
                        },
                        "exceptions": {"type": "array", "items": {"type": "string"}},
                        "applies_to": {"type": "array", "items": {"type": "string", "enum": topic_slugs}},
                        "subdivision": {"type": ["string", "null"]},
                        "source_quote": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "citations": {
                "type": "array",
                "description": "Authorities this rule cites, e.g. Family Code sections, "
                               "California Rules of Court, Judicial Council forms.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["relation", "to_kind", "to_ref"],
                    "properties": {
                        "relation": {
                            "type": "string",
                            "enum": ["implements", "subject_to", "related_to", "uses_form"],
                        },
                        "to_kind": {"type": "string", "enum": ["rule", "statute", "crc", "form"]},
                        "to_ref": {"type": "string"},
                    },
                },
            },
        },
    }


def prompt_for(rule: dict, county_name: str) -> str:
    return (
        f"County: {county_name}\n"
        f"Citation: {rule['citation']}\n"
        f"Rule number: {rule['rule_number']}\n"
        f"Title: {rule['title']}\n"
        f"Effective: {rule['effective_from']}\n\n"
        f"Rule text:\n<rule>\n{rule['text']}\n</rule>"
    )


def request_params(rule: dict, county_name: str, json_schema: dict) -> dict:
    return {
        "model": MODEL,
        "max_tokens": 8000,
        # The system prompt and schema are identical for every rule in the run,
        # so they cache; only the rule text after them varies.
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": json_schema}},
        "messages": [{"role": "user", "content": prompt_for(rule, county_name)}],
    }


def to_records(rule: dict, payload: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Shape the model's output into requirement/citation rows."""
    requirements = []
    for index, item in enumerate(payload.get("requirements", [])):
        requirements.append(
            {
                "id": f"{rule['id']}-req-{index}",
                "rule_id": rule["id"],
                "county_id": rule["county_id"],
                "topic": item["topic"],
                "requirement_type": item["requirement_type"],
                "actor": item.get("actor"),
                "action": item.get("action"),
                "description": item["description"],
                "trigger_event": item.get("trigger_event"),
                "deadline_amount": item.get("deadline_amount"),
                "deadline_unit": item.get("deadline_unit"),
                "deadline_direction": item.get("deadline_direction"),
                "exceptions": item.get("exceptions", []),
                "applies_to": item.get("applies_to", []),
                "subdivision": item.get("subdivision"),
                "source_quote": item["source_quote"],
                "confidence": item["confidence"],
                # Never 'verified' straight out of the model: a human merges the PR.
                "review_status": "needs_review",
            }
        )
    citations = [
        {"from_rule_id": rule["id"], **c} for c in payload.get("citations", [])
    ]
    return requirements, citations, payload.get("topics", [])


def run_sync(client: Anthropic, rules: list[dict], county_name: str, json_schema: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n, rule in enumerate(rules, 1):
        print(f"    [{n}/{len(rules)}] {rule['rule_number']}", flush=True)
        message = client.messages.create(**request_params(rule, county_name, json_schema))
        text = "".join(b.text for b in message.content if b.type == "text")
        out[rule["id"]] = json.loads(text)
    return out


def run_batch(client: Anthropic, rules: list[dict], county_name: str, json_schema: dict) -> dict[str, dict]:
    """Half price, and nothing about this pipeline is latency-sensitive."""
    batch = client.messages.batches.create(
        requests=[
            {"custom_id": f"r{i}", "params": request_params(rule, county_name, json_schema)}
            for i, rule in enumerate(rules)
        ]
    )
    print(f"    batch {batch.id}: {len(rules)} rules", flush=True)
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        print(f"    ... {batch.processing_status}", flush=True)
        time.sleep(BATCH_POLL_S)

    out: dict[str, dict] = {}
    # Results arrive in arbitrary order — key by custom_id, never by position.
    for result in client.messages.batches.results(batch.id):
        index = int(result.custom_id[1:])
        rule = rules[index]
        if result.result.type != "succeeded":
            print(f"    ! {rule['rule_number']}: {result.result.type}", file=sys.stderr)
            continue
        text = "".join(b.text for b in result.result.message.content if b.type == "text")
        out[rule["id"]] = json.loads(text)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("counties", nargs="*")
    ap.add_argument("--sync", action="store_true", help="inline instead of the Batch API")
    ap.add_argument("--limit", type=int, help="only the first N rules (prompt iteration)")
    ap.add_argument("--force", action="store_true", help="re-extract rules already done")
    args = ap.parse_args(argv)

    tax = load_topics()
    topic_slugs = [t["slug"] for t in tax["topics"]]
    json_schema = schema(topic_slugs, tax["requirement_types"])
    client = build_client()

    paths = sorted((DATA / "counties").glob("*.json"))
    if args.counties:
        paths = [p for p in paths if p.stem in set(args.counties)]
    if not paths:
        print("error: no matching county data; run scripts/parse.py first", file=sys.stderr)
        return 1

    for path in paths:
        county = read_json(path)
        done = {r["rule_id"] for r in county.get("requirements", [])} if not args.force else set()
        # Superseded editions are historical record; extracting them again buys
        # nothing an application will query.
        rules = [
            r for r in county["rules"]
            if r["effective_to"] is None and (args.force or r["id"] not in done)
        ]
        if args.limit:
            rules = rules[: args.limit]
        if not rules:
            print(f"  {path.stem}: nothing to extract")
            continue

        print(f"  {path.stem}: {len(rules)} rules")
        runner = run_sync if args.sync else run_batch
        payloads = runner(client, rules, county["county"], json_schema)

        requirements = [] if args.force else list(county.get("requirements", []))
        citations = [] if args.force else list(county.get("citations", []))
        by_id = {r["id"]: r for r in county["rules"]}
        for rule_id, payload in payloads.items():
            reqs, cites, rule_topics = to_records(by_id[rule_id], payload)
            requirements.extend(reqs)
            citations.extend(cites)
            by_id[rule_id]["topics"] = rule_topics

        county["requirements"] = requirements
        county["citations"] = citations
        county["extraction_status"] = "needs_review"
        write_json(path, county)
        low = sum(1 for r in requirements if (r.get("confidence") or 0) < 0.8)
        print(f"    {len(requirements)} requirements ({low} low-confidence), {len(citations)} citations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
