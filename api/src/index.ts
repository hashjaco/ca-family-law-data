/**
 * Read-only API over the California family-law corpus.
 *
 * Six endpoints, no auth, no writes. Deliberately no router dependency: six
 * routes do not need one.
 *
 * Two invariants every response upholds:
 *   - it carries `corpus_version`, so a consumer can tell which build answered;
 *   - every rule and requirement carries its `source` and `authority_level`, so
 *     a consumer can never render a legal claim it cannot cite. That is the
 *     whole point of the corpus, and it is enforced here rather than trusted.
 *
 * The 10 ms CPU ceiling on the free plan means: LIMIT everything, no fan-out,
 * no aggregation in the Worker. Let SQLite do the work.
 */

interface Env {
  DB: D1Database;
}

const MAX_LIMIT = 100;
const DEFAULT_LIMIT = 50;
const CACHE = "public, max-age=3600";

function clampLimit(raw: string | null): number {
  const n = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_LIMIT;
  return Math.min(n, MAX_LIMIT);
}

/** ISO date or nothing. Anything else is rejected rather than silently ignored, */
/** because a mistyped as_of that quietly returns today's rules is a wrong answer. */
function asOf(raw: string | null): string | null {
  if (!raw) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) throw new BadRequest("as_of must be YYYY-MM-DD");
  return raw;
}

class BadRequest extends Error {}

async function corpusVersion(env: Env): Promise<string> {
  const row = await env.DB.prepare("SELECT value FROM corpus_meta WHERE key='version'").first<{
    value: string;
  }>();
  return row?.value ?? "unknown";
}

function json(body: unknown, version: string, status = 200): Response {
  return new Response(JSON.stringify({ corpus_version: version, ...(body as object) }), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": status === 200 ? CACHE : "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

/** Shared shape for a rule row so `source` and `authority_level` can never be dropped. */
const RULE_COLUMNS = `
  r.id, r.rule_key, r.county_id, r.authority_level, r.citation, r.rule_number,
  r.title, r.text, r.topics_json, r.effective_from, r.effective_to,
  r.review_status, r.page, s.url AS source_url, s.sha256 AS source_sha256,
  s.retrieved_at AS source_retrieved_at`;

function shapeRule(row: Record<string, unknown>) {
  const { source_url, source_sha256, source_retrieved_at, topics_json, page, ...rest } = row;
  return {
    ...rest,
    topics: JSON.parse((topics_json as string) ?? "[]"),
    source: { url: source_url, page, sha256: source_sha256, retrieved_at: source_retrieved_at },
  };
}

async function handle(url: URL, env: Env, version: string): Promise<Response> {
  const path = url.pathname.replace(/\/+$/, "");
  const q = url.searchParams;

  switch (path) {
    case "/v1/manifest": {
      const rows = await env.DB.prepare("SELECT key, value FROM corpus_meta").all();
      const meta = Object.fromEntries(rows.results.map((r) => [r.key, r.value]));
      return json({ meta }, version);
    }

    case "/v1/counties": {
      const status = q.get("extraction_status");
      const stmt = status
        ? env.DB.prepare(
            "SELECT * FROM counties WHERE extraction_status = ? ORDER BY name",
          ).bind(status)
        : env.DB.prepare("SELECT * FROM counties ORDER BY name");
      const { results } = await stmt.all();
      return json({ counties: results }, version);
    }

    case "/v1/rules": {
      const day = asOf(q.get("as_of"));
      const where: string[] = [];
      const binds: unknown[] = [];
      if (q.get("id")) (where.push("r.id = ?"), binds.push(q.get("id")));
      if (q.get("county")) (where.push("r.county_id = ?"), binds.push(q.get("county")));
      if (q.get("authority_level"))
        (where.push("r.authority_level = ?"), binds.push(Number(q.get("authority_level"))));
      // topics_json is a JSON array; a LIKE on the quoted slug is exact enough
      // because slugs are a closed vocabulary with no substring collisions.
      if (q.get("topic")) (where.push("r.topics_json LIKE ?"), binds.push(`%"${q.get("topic")}"%`));
      if (day) {
        where.push("r.effective_from <= ? AND (r.effective_to IS NULL OR r.effective_to > ?)");
        binds.push(day, day);
      } else {
        where.push("r.effective_to IS NULL"); // current edition unless asked otherwise
      }
      const limit = clampLimit(q.get("limit"));
      const offset = Number.parseInt(q.get("cursor") ?? "0", 10) || 0;
      const { results } = await env.DB.prepare(
        `SELECT ${RULE_COLUMNS} FROM rules r JOIN sources s ON s.id = r.source_id
         WHERE ${where.join(" AND ")}
         ORDER BY r.authority_level, r.county_id, r.rule_number
         LIMIT ? OFFSET ?`,
      )
        .bind(...binds, limit, offset)
        .all();
      const next = results.length === limit ? String(offset + limit) : null;
      return json({ rules: results.map(shapeRule), next_cursor: next }, version);
    }

    case "/v1/requirements": {
      const day = asOf(q.get("as_of"));
      const where: string[] = [];
      const binds: unknown[] = [];
      if (q.get("county")) (where.push("q.county_id = ?"), binds.push(q.get("county")));
      if (q.get("topic")) (where.push("q.topic = ?"), binds.push(q.get("topic")));
      if (q.get("requirement_type"))
        (where.push("q.requirement_type = ?"), binds.push(q.get("requirement_type")));
      if (day) {
        where.push("r.effective_from <= ? AND (r.effective_to IS NULL OR r.effective_to > ?)");
        binds.push(day, day);
      } else {
        where.push("r.effective_to IS NULL");
      }
      const limit = clampLimit(q.get("limit"));
      const { results } = await env.DB.prepare(
        `SELECT q.*, r.citation, r.authority_level, r.page, s.url AS source_url,
                s.sha256 AS source_sha256
         FROM requirements q
         JOIN rules r ON r.id = q.rule_id
         JOIN sources s ON s.id = r.source_id
         WHERE ${where.join(" AND ")}
         ORDER BY q.county_id, q.topic LIMIT ?`,
      )
        .bind(...binds, limit)
        .all();
      const requirements = results.map((row) => {
        const { exceptions_json, applies_to_json, source_url, source_sha256, citation, page, ...rest } =
          row as Record<string, unknown>;
        return {
          ...rest,
          exceptions: JSON.parse((exceptions_json as string) ?? "[]"),
          applies_to: JSON.parse((applies_to_json as string) ?? "[]"),
          source: { citation, url: source_url, page, sha256: source_sha256 },
        };
      });
      return json({ requirements }, version);
    }

    case "/v1/search": {
      const term = (q.get("q") ?? "").trim();
      if (!term) throw new BadRequest("q is required");
      const where = ["rules_fts MATCH ?", "r.effective_to IS NULL"];
      const binds: unknown[] = [term];
      if (q.get("county")) (where.push("r.county_id = ?"), binds.push(q.get("county")));
      if (q.get("topic")) (where.push("r.topics_json LIKE ?"), binds.push(`%"${q.get("topic")}"%`));
      const limit = clampLimit(q.get("limit"));
      const { results } = await env.DB.prepare(
        `SELECT ${RULE_COLUMNS}, bm25(rules_fts) AS score
         FROM rules_fts JOIN rules r ON r.rowid = rules_fts.rowid
         JOIN sources s ON s.id = r.source_id
         WHERE ${where.join(" AND ")}
         ORDER BY score, r.authority_level LIMIT ?`,
      )
        .bind(...binds, limit)
        .all();
      return json({ results: results.map(shapeRule) }, version);
    }

    case "/v1/changes": {
      const since = q.get("since");
      const stmt = since
        ? env.DB.prepare(
            "SELECT * FROM changes WHERE detected_at >= ? ORDER BY detected_at DESC LIMIT ?",
          ).bind(since, clampLimit(q.get("limit")))
        : env.DB.prepare("SELECT * FROM changes ORDER BY detected_at DESC LIMIT ?").bind(
            clampLimit(q.get("limit")),
          );
      const { results } = await stmt.all();
      return json(
        {
          changes: results.map((row) => ({
            ...row,
            changed_rule_ids: JSON.parse((row.changed_rule_ids_json as string) ?? "[]"),
          })),
        },
        version,
      );
    }

    default:
      return json({ error: "not found" }, version, 404);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "GET") {
      return new Response(JSON.stringify({ error: "method not allowed" }), {
        status: 405,
        headers: { "content-type": "application/json", allow: "GET" },
      });
    }
    const version = await corpusVersion(env);
    try {
      return await handle(new URL(request.url), env, version);
    } catch (err) {
      if (err instanceof BadRequest) return json({ error: err.message }, version, 400);
      console.error(err);
      return json({ error: "internal error" }, version, 500);
    }
  },
} satisfies ExportedHandler<Env>;
