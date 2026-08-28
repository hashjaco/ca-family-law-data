# ca-family-law-data

A versioned, cited corpus of California family-law procedure: statewide authority
plus each county's local rules, local forms, and Family Court Services procedures.

California family law is **statewide in substance and county-specific in
procedure**. California Rule of Court 5.4 lets each of the 58 Superior Courts
adopt local family rules and forms that supplement — but cannot contradict —
statewide law. So "what do I have to do to modify custody?" has 58 different
procedural answers on top of one legal answer, and none of that is available as
data anywhere.

This repository is the ingestion pipeline that turns the courts' published PDFs
into queryable records, and it feeds [Prose](../ill-legal), a desktop app for
people representing themselves in California family court.

## What makes a record trustworthy here

Getting this wrong is not a bad search result — a wrong filing deadline is a
missed hearing for someone with no lawyer. Four properties are enforced, not
assumed:

- **Provenance.** Every record traces to a SHA-256 of bytes actually downloaded
  and archived, with the page number it came from.
- **Quoted, not recalled.** Every extracted requirement carries a `source_quote`
  that `validate.py` proves is a verbatim substring of the cited rule. The model
  extracts *from* the text it is given; it never supplies law from memory.
- **Authority ranking.** Every record carries an `authority_level` (10 statute →
  90 self-help guidance) so a county rule can never be presented as outranking
  the Family Code.
- **Point-in-time.** Rules are versioned by effective date, so *"what rule
  applied when my hearing happened"* is answerable, not just *"what applies
  today"*.

Nothing reaches `verified` automatically. Extraction lands as `needs_review`; a
human merges the PR.

## Pipeline

```
discover.py  Judicial Branch index -> 58 counties, URLs, effective dates
crawl.py     fetch -> sha256 -> archive (R2)      -> data/sources.json
parse.py     PyMuPDF -> structured rule tree      -> data/counties/*.json
extract.py   Claude -> requirements, topics, cites -> data/counties/*.json
validate.py  schema + legal invariants             (CI gate)
build.py     -> dist/{sqlite, corpus.sql, research.jsonl, court-data.json}
```

Review is a **GitHub PR diff** over the committed JSON. There is no admin UI,
and there is not going to be one.

```bash
uv sync
uv run python scripts/discover.py
uv run python scripts/crawl.py contra-costa
uv run python scripts/parse.py contra-costa
ANTHROPIC_API_KEY=... uv run python scripts/extract.py contra-costa
uv run python scripts/validate.py
uv run python scripts/build.py
uv run pytest tests -q
```

## Free-tier infrastructure

Runs at **$0/month**. Limits verified 2026-08-28.

| Service | Free allowance | Used for |
|---|---|---|
| GitHub Actions (public repo) | unlimited standard runners | every pipeline stage; this is why the repo is public |
| Cloudflare R2 | 10 GB, free egress | immutable source archive + published artifacts |
| Cloudflare D1 | 5 GB, 5M reads/day, 100k writes/day | the hosted corpus (D1 *is* SQLite, so the build artifact imports directly) |
| Cloudflare Workers | 100k req/day, 10 ms CPU | the read API |
| GitHub PRs | — | the review queue |

The only real cost is model extraction: roughly **$10–20 for a full 58-county
pass** on Sonnet 5 via the Batch API, and cents for weekly incremental re-runs.

## API

Read-only, edge-cached. Every response carries `corpus_version`; every rule and
requirement carries its `source` and `authority_level`.

| Endpoint | Notable params |
|---|---|
| `GET /v1/manifest` | — |
| `GET /v1/counties` | `extraction_status` |
| `GET /v1/rules` | `id`, `county`, `topic`, `authority_level`, `as_of`, `limit`, `cursor` |
| `GET /v1/requirements` | `county`, `topic`, `requirement_type`, `as_of` |
| `GET /v1/search` | `q`, `county`, `topic` (FTS5) |
| `GET /v1/changes` | `since` |

`as_of=YYYY-MM-DD` does the point-in-time query; omit it for current rules.

```bash
cd api && npm ci
npx wrangler d1 execute ca-family-law --local --file=../dist/corpus.sql
npx wrangler dev --local
```

## Feeding Prose

Two artifacts land in the app, both matching structures it already reads:

- **`research.jsonl`** → the shared LanceDB research corpus
  (`{id, title, source_url, jurisdiction, topic, text}`, embedded with
  bge-small-en-v1.5). `jurisdiction` is the county name exactly as
  `src/lib/counties.ts` spells it, because retrieval filters on equality.
- **`court-data.json`** → `court_data.rs`'s manifest. The hand-built courthouse
  directory is preserved verbatim; only `requirements` is regenerated.

Both ship bundled as the offline baseline and update out of band via the
manifest's `corpus` entry, which reuses the app's existing SHA-256-verified
download path.

## Coverage

Pilot counties: Alameda, Contra Costa, Los Angeles, San Diego, Santa Clara —
chosen because they publish in four incompatible shapes, which is the thing that
has to work before scaling to 53 more.

`extraction_status` per county is published rather than hidden: an app must be
able to say *"no local-rule data for this county"* instead of implying that
statewide-only means nothing local applies.

## Crawling conduct

These are public court publications, but the crawler still identifies itself
with a contact URL, respects robots.txt, makes one request at a time per host
with a delay, and uses conditional GET so a weekly re-check downloads nothing
when nothing changed.

## Not legal advice

A corpus of court rules is not advice, and neither is anything built on it.
Every record links to the official publication it came from; when it matters,
read that.
