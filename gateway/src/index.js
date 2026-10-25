/**
 * Autopilot gateway: a Cloudflare Worker + D1 between Linear and the Mac runner (D1 in
 * docs/architecture.md).
 *
 * POST /webhook                 Linear AgentSessionEvent webhooks for the Autopilot app.
 *                               Verifies the signature, stores a job and, within seconds,
 *                               posts a "Queued" thought activity as Autopilot.
 * POST /github                  GitHub App webhooks: an @mention on a PR becomes a job on
 *                               the PR's Linear issue (src/github.js).
 * GET  /jobs/next               The runner's next job (an interrupted one first), or null.
 * GET  /jobs/:id                One job (the runner checks it for a stop request).
 * POST /jobs/:id/ack|done|blocked
 *                               The runner claims or finishes a job.
 * GET  /health                  Liveness only; says nothing about jobs.
 *
 * /jobs/* need "Authorization: Bearer <GATEWAY_TOKEN>". Secrets are Worker secrets only:
 * LINEAR_WEBHOOK_SECRET, GATEWAY_TOKEN, LINEAR_CLIENT_SECRET, GITHUB_WEBHOOK_SECRET,
 * GITHUB_APP_PRIVATE_KEY. None of them, nor the Linear or GitHub tokens, is ever logged or
 * returned. See docs/gateway.md.
 */

import { handleGitHub } from "./github.js";

const LINEAR_API = "https://api.linear.app/graphql";
const LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token";
// Must match SCOPES in bin/autopilot-linear: a token with another scope set revokes
// every existing app token, the runner's included.
const SCOPES = "read,write,app:assignable,app:mentionable";
const TIMESTAMP_TOLERANCE_MS = 60_000; // Linear's documented replay window
const TOKEN_REFRESH_MARGIN_MS = 24 * 3600_000;
const DEFAULT_TOKEN_LIFETIME_S = 30 * 24 * 3600;
const STALE_RUN_MS = 6 * 3600_000; // same as the runner's stale lock
const PROMPT_MAX = 8000;
const EVENT_TTL_MS = 7 * 24 * 3600_000; // Linear retries for at most 6 h
const LINEAR_TIMEOUT_MS = 5000; // the first activity must land within 10 s
const NOTE_MAX = 2000;
// How long a session the runner said it is about to open counts as its own (POST /sessions/expect).
const EXPECT_MS = 2 * 60_000;
const ISSUE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OPEN = "('queued','running')";

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    const method = request.method;
    try {
      if (pathname === "/webhook" && method === "POST") return await handleWebhook(request, env, ctx);
      if (pathname === "/github" && method === "POST") return await handleGitHub(request, env, ctx);
      if (pathname === "/health" && method === "GET") return json({ ok: true });
      if (pathname.startsWith("/jobs/") || pathname.startsWith("/sessions/")) {
        const denied = await checkRunnerAuth(request, env);
        if (denied) return denied;
        if (pathname === "/sessions/expect" && method === "POST") return await expectSession(env, request);
        if (pathname === "/jobs/next" && method === "GET") return await nextJob(env);
        const one = pathname.match(/^\/jobs\/([0-9a-f-]{36})$/);
        if (one && method === "GET") return await getJob(env, one[1]);
        const m = pathname.match(/^\/jobs\/([0-9a-f-]{36})\/(ack|done|blocked)$/);
        if (m && method === "POST") return await updateJob(env, m[1], m[2], request);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      console.error(`gateway error: ${e && e.message}`);
      return json({ error: "internal error" }, 500);
    }
  },
};

// --------------------------------------------------------------------------
// Webhook

async function handleWebhook(request, env, ctx) {
  if (!env.LINEAR_WEBHOOK_SECRET || !env.LINEAR_CLIENT_ID) return json({ error: "webhook secret or client id not configured" }, 503);
  const raw = await request.text();
  const sigHeader = request.headers.get("linear-signature");
  if (!(await verifySignature(env.LINEAR_WEBHOOK_SECRET, raw, sigHeader))) {
    // Why, never what: no secret, signature or body in the log.
    console.warn(`webhook rejected: bad signature (header ${sigHeader ? `${sigHeader.length} chars` : "missing"}, body ${raw.length} bytes)`);
    return json({ error: "bad signature" }, 401);
  }
  let p;
  try {
    p = JSON.parse(raw);
  } catch {
    return json({ error: "bad json" }, 400);
  }
  if (!(Math.abs(Date.now() - Number(p.webhookTimestamp)) <= TIMESTAMP_TOLERANCE_MS)) {
    console.warn(`webhook rejected: webhookTimestamp ${p.webhookTimestamp} is ${Math.round((Date.now() - Number(p.webhookTimestamp)) / 1000)} s off`);
    return json({ error: "stale or missing webhookTimestamp" }, 401);
  }
  if (p.type !== "AgentSessionEvent") return json({ ok: true, ignored: String(p.type) });
  if (p.oauthClientId !== env.LINEAR_CLIENT_ID) {
    console.warn("webhook ignored: oauthClientId is not this app's");
    return json({ ok: true, ignored: "another app" });
  }

  const session = p.agentSession || {};
  const activity = p.agentActivity || null;
  if (!session.id || !["created", "prompted"].includes(p.action)) return json({ ok: true, ignored: String(p.action) });
  if (p.action === "prompted" && !(activity && activity.id)) return json({ ok: true, ignored: "prompt without activity" });
  const now = Date.now();
  const key = p.action === "created" ? `created:${session.id}` : `prompted:${activity.id}`;
  const seen = env.DB.prepare("INSERT INTO events (key, received_at) VALUES (?, ?) ON CONFLICT (key) DO NOTHING").bind(key, now);
  if (await env.DB.prepare("SELECT 1 FROM events WHERE key = ?").bind(key).first()) return json({ ok: true, duplicate: true });

  // A session Autopilot opened itself comes back as a "created" event with no creator: one for a
  // GitHub @mention (src/github.js), whose job is already queued, or one a run opened to post into
  // (bin/autopilot-linear ensure_session). Answering it would refuse, and close, our own work.
  // Only when that's certain: our own user id, or no creator and either a job already holds this
  // session or the runner announced it (POST /sessions/expect) for this issue.
  // Anything else falls through to the owner check, which answers instead of dropping silently.
  if (p.action === "created") {
    const creator = session.creatorId || (session.creator && session.creator.id);
    const ours = (env.APP_USER_ID && creator === env.APP_USER_ID)
      || (!creator && await env.DB.prepare("SELECT 1 FROM jobs WHERE session_id = ?").bind(session.id).first())
      || (!creator && await takeExpected(env, session.issue && session.issue.id, now));
    if (ours) {
      await seen.run();
      return json({ ok: true, ignored: "own session" });
    }
  }

  // Only the people in ALLOWED_USER_IDS may start work (empty: anyone in the workspace).
  const allowed = String(env.ALLOWED_USER_IDS || "").split(",").map((s) => s.trim()).filter(Boolean);
  const by = p.action === "created"
    ? session.creatorId || (session.creator && session.creator.id)
    : activity.userId || (activity.user && activity.user.id);
  if (allowed.length && !allowed.includes(by)) {
    await seen.run();
    ctx.waitUntil(postActivitySafely(env, session.id, { type: "response", body: "Sorry, I only take work from my owner here." }));
    return json({ ok: true, ignored: "not an allowed user" });
  }

  if (activity && activity.signal === "stop") return stop(env, ctx, session.id, seen, now);

  const issue = session.issue;
  if (!issue || !issue.id) {
    await seen.run();
    ctx.waitUntil(postActivitySafely(env, session.id, { type: "response", body: "I only work on issues for now. Mention me in an issue, or delegate one to me." }));
    return json({ ok: true, ignored: "no issue" });
  }

  const prompt = p.action === "prompted" ? activity.content && activity.content.body : session.comment && session.comment.body;
  // The event key is written in the same batch (one transaction) as the job, so a failed
  // write leaves nothing behind and Linear's retry queues it again.
  if (p.action === "prompted") {
    // A second message before the runner got to the first: one job, both messages.
    const queued = await env.DB.prepare(
      "SELECT id, prompt FROM jobs WHERE session_id = ? AND status = 'queued' ORDER BY created_at DESC LIMIT 1",
    ).bind(session.id).first();
    if (queued) {
      if (await mergeIntoQueued(env, queued, prompt, seen, now)) {
        ctx.waitUntil(ackJob(env, queued.id, session.id, true));
        return json({ ok: true, jobId: queued.id, merged: true });
      }
      // The runner took it in between: this message becomes a job of its own.
      const jobId = crypto.randomUUID();
      await insertJob(env, jobId, p.action, session, issue, prompt, now).run();
      ctx.waitUntil(ackJob(env, jobId, session.id, false));
      return json({ ok: true, jobId, merged: false });
    }
  }
  if (p.action === "created") {
    // One live session per issue. A second delegation or mention while a job for the issue is
    // open joins that job, so the owner sees all progress in one place, not split across sessions.
    const open = await env.DB.prepare(
      `SELECT id, session_id, status, prompt FROM jobs WHERE issue_id = ? AND status IN ${OPEN}
       ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at LIMIT 1`,
    ).bind(issue.id).first();
    if (open && open.session_id !== session.id) {
      const pointer = "I'm already on this issue in its earlier Autopilot session; progress and the result go there.";
      if (open.status === "queued") {
        if (await mergeIntoQueued(env, open, prompt, seen, now)) {
          ctx.waitUntil(postActivitySafely(env, session.id, { type: "response", body: prompt ? `${pointer} Your note is added to it.` : pointer }));
          return json({ ok: true, jobId: open.id, merged: true, joined: open.session_id });
        }
      }
      // Being worked on (or taken just now): a note becomes a reply in the open session, which the
      // run reads at its next checkpoint. A bare re-delegation needs nothing more.
      if (prompt) {
        const jobId = crypto.randomUUID();
        await env.DB.batch([insertJob(env, jobId, "prompted", { ...session, id: open.session_id }, issue, prompt, now), seen]);
        ctx.waitUntil(postActivitySafely(env, session.id, { type: "response", body: `${pointer} I'll read your note there at my next checkpoint.` }));
        return json({ ok: true, jobId, merged: false, joined: open.session_id });
      }
      await seen.run();
      ctx.waitUntil(postActivitySafely(env, session.id, { type: "response", body: pointer }));
      return json({ ok: true, jobId: open.id, merged: true, joined: open.session_id });
    }
  }
  const jobId = crypto.randomUUID();
  await env.DB.batch([insertJob(env, jobId, p.action, session, issue, prompt, now), seen]);
  ctx.waitUntil(ackJob(env, jobId, session.id, false));
  return json({ ok: true, jobId, merged: false });
}

/**
 * Add a note to a job that is still queued (one job, every message), in the same batch as the
 * event key. False when the runner took the job in between: the caller then queues a new one.
 */
export async function mergeIntoQueued(env, job, prompt, seen, now) {
  const both = clipTail([job.prompt, prompt].filter(Boolean).join("\n\n---\n\n"), PROMPT_MAX);
  const [upd] = await env.DB.batch([
    env.DB.prepare("UPDATE jobs SET prompt = ?, updated_at = ? WHERE id = ? AND status = 'queued'").bind(both, now, job.id),
    seen,
  ]);
  return upd.meta.changes > 0;
}

function insertJob(env, jobId, kind, session, issue, prompt, now) {
  return env.DB.prepare(
    `INSERT INTO jobs (id, kind, status, session_id, issue_id, issue_identifier, issue_title, issue_url, team_key,
                       comment_id, prompt, created_at, updated_at)
     VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    jobId, kind, session.id, issue.id, issue.identifier || null, issue.title || null, issue.url || null,
    (issue.team && issue.team.key) || null, session.commentId || (session.comment && session.comment.id) || null,
    clipTail(prompt, PROMPT_MAX), now, now,
  );
}

/** Stop in Linear: cancel queued jobs; flag a running one, which the runner checks before a PR or a merge. */
async function stop(env, ctx, sessionId, seen, now) {
  const [cancelled, flagged] = await env.DB.batch([
    env.DB.prepare(
      "UPDATE jobs SET status = 'cancelled', note = 'stopped in Linear', updated_at = ?, finished_at = ? WHERE session_id = ? AND status = 'queued'",
    ).bind(now, now, sessionId),
    env.DB.prepare("UPDATE jobs SET stop_requested = 1, updated_at = ? WHERE session_id = ? AND status = 'running'").bind(now, sessionId),
    seen,
  ]);
  const body = flagged.meta.changes
    ? "Stop noted. The run working on this stops at its next checkpoint (before it opens or merges a PR) and reports here."
    : "Stopped. Nothing is queued for this any more; mention me to start again.";
  ctx.waitUntil(postActivitySafely(env, sessionId, { type: "response", body }));
  return json({ ok: true, cancelled: cancelled.meta.changes, stopRequested: flagged.meta.changes });
}

async function ackJob(env, jobId, sessionId, merged) {
  const body = await queuedText(env, jobId, Date.now());
  await postActivitySafely(env, sessionId, { type: "thought", body: merged ? `Added to the queued job. ${body}` : body });
}

// An interrupted run first; then the owner's replies and mentions about work already under way
// ("prompted"), so talking to Autopilot never waits behind new tickets; then new work ("created").
const KIND_RANK = "CASE kind WHEN 'prompted' THEN 0 ELSE 1 END";
const JOB_ORDER = `CASE status WHEN 'running' THEN 0 ELSE 1 END, ${KIND_RANK}, created_at`;

/** "Queued; I'll start within N minutes", honest about a busy or sleeping runner. */
export async function queuedText(env, jobId, now) {
  const interval = Number(env.RUNNER_INTERVAL_MIN || 10);
  const job = await env.DB.prepare(`SELECT created_at, ${KIND_RANK} AS rank FROM jobs WHERE id = ?`).bind(jobId).first();
  // Jobs that come before this one in JOB_ORDER.
  const ahead = (await env.DB.prepare(
    `SELECT COUNT(*) AS n FROM jobs WHERE status IN ${OPEN} AND id != ?
       AND (status = 'running' OR ${KIND_RANK} < ? OR (${KIND_RANK} = ? AND created_at <= ?))`,
  ).bind(jobId, job ? job.rank : 1, job ? job.rank : 1, job ? job.created_at : now).first()).n;
  if (ahead > 0) {
    return `Queued behind ${ahead} other job${ahead === 1 ? "" : "s"}; I'll start when ${ahead === 1 ? "it's" : "they're"} done.`;
  }
  const last = await getState(env, "last_poll");
  if (!last) return "Queued; my runner hasn't checked in yet. I'll start as soon as it does.";
  const sinceMin = (now - Number(last)) / 60_000;
  if (sinceMin > 3 * interval) {
    return `Queued. My runner last checked in ${Math.round(sinceMin)} minutes ago (the Mac may be asleep); I'll start as soon as it's back.`;
  }
  const n = Math.max(1, Math.ceil(interval - sinceMin));
  return `Queued; I'll start within ${n} minute${n === 1 ? "" : "s"}.`;
}

// --------------------------------------------------------------------------
// Runner API

async function checkRunnerAuth(request, env) {
  if (!env.GATEWAY_TOKEN) return json({ error: "runner token not configured" }, 503);
  const h = request.headers.get("authorization") || "";
  const given = h.startsWith("Bearer ") ? h.slice(7) : "";
  // Compare digests, so neither the length nor a prefix of the token leaks through timing.
  const [a, b] = await Promise.all([sha256(given), sha256(env.GATEWAY_TOKEN)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0 && given ? null : json({ error: "unauthorized" }, 401);
}

/** The runner is about to open an Autopilot session on this issue itself: its "created" event is ours. */
async function expectSession(env, request) {
  const body = await request.json().catch(() => null);
  const issueId = body && body.issueId;
  if (typeof issueId !== "string" || !ISSUE_UUID.test(issueId)) return json({ error: "issueId must be a Linear issue id (UUID)" }, 400);
  await expect(env, issueId);
  return json({ ok: true, issueId, expiresInMs: EXPECT_MS });
}

export async function expect(env, issueId) {
  await putState(env, `expect:${issueId}`, String(Date.now()));
}

/** Whether an announced session on this issue is due, and if so, uses the announcement up. */
async function takeExpected(env, issueId, now) {
  if (!issueId) return false;
  const key = `expect:${issueId}`;
  const at = Number(await getState(env, key));
  if (!at) return false;
  await env.DB.prepare("DELETE FROM state WHERE key = ?").bind(key).run();
  return now - at <= EXPECT_MS;
}

async function getJob(env, id) {
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first();
  return job ? json({ job: publicJob(job) }) : json({ error: "no such job" }, 404);
}

async function nextJob(env) {
  const now = Date.now();
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO state (key, value, updated_at) VALUES ('last_poll', ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    ).bind(String(now), now),
    env.DB.prepare("DELETE FROM events WHERE received_at < ?").bind(now - EVENT_TTL_MS),
  ]);
  // A job still "running" here belongs to a run that died, or (runs work side by side since
  // 0.7.0) to a live one: offer it first; the runner takes it only if it gets the issue's lock.
  const job = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status IN ${OPEN} ORDER BY ${JOB_ORDER} LIMIT 1`,
  ).first();
  const { n } = await env.DB.prepare(`SELECT COUNT(*) AS n FROM jobs WHERE status IN ${OPEN}`).first();
  // The next few too, in the same order, so the runner can pass over a project it must skip
  // (its preprod is broken) without stalling the others.
  const { results } = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status IN ${OPEN} ORDER BY ${JOB_ORDER} LIMIT 20`,
  ).all();
  return json({ pending: n, job: job ? publicJob(job) : null, jobs: results.map(publicJob) });
}

const TRANSITIONS = {
  ack: { to: "running", from: ["queued", "running"] },
  done: { to: "done", from: ["queued", "running"] },
  blocked: { to: "blocked", from: ["queued", "running"] },
};

async function updateJob(env, id, action, request) {
  const t = TRANSITIONS[action];
  let note = null;
  if (action !== "ack") {
    const body = await request.json().catch(() => null);
    note = clip(body && typeof body.note === "string" ? body.note : null, NOTE_MAX);
  }
  const now = Date.now();
  const from = t.from.map(() => "?").join(", ");
  const sql = action === "ack"
    ? `UPDATE jobs SET status = ?, acked_at = ?, attempts = attempts + 1, updated_at = ? WHERE id = ? AND status IN (${from})`
    : `UPDATE jobs SET status = ?, finished_at = ?, note = ?, updated_at = ? WHERE id = ? AND status IN (${from})`;
  const args = action === "ack" ? [t.to, now, now, id, ...t.from] : [t.to, now, note, now, id, ...t.from];
  const res = await env.DB.prepare(sql).bind(...args).run();
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first();
  if (!job) return json({ error: "no such job" }, 404);
  // done/blocked twice (a retry after a lost reply) is fine; anything else is a conflict.
  if (!res.meta.changes && !(action !== "ack" && job.status === t.to)) {
    return json({ error: `job is ${job.status}`, job: publicJob(job) }, 409);
  }
  return json({ ok: true, job: publicJob(job) });
}

function publicJob(r) {
  return {
    id: r.id,
    kind: r.kind,
    status: r.status,
    sessionId: r.session_id,
    issueId: r.issue_id,
    identifier: r.issue_identifier,
    title: r.issue_title,
    url: r.issue_url,
    team: r.team_key,
    commentId: r.comment_id,
    prompt: r.prompt,
    source: r.source || "linear",
    github: r.github_repo ? { repo: r.github_repo, pr: r.github_pr, commentUrl: r.github_comment_url } : null,
    attempts: r.attempts,
    stopRequested: !!r.stop_requested,
    note: r.note,
    createdAt: iso(r.created_at),
    ackedAt: iso(r.acked_at),
    finishedAt: iso(r.finished_at),
    stale: r.status === "running" && r.acked_at != null && Date.now() - r.acked_at > STALE_RUN_MS,
  };
}

// --------------------------------------------------------------------------
// Linear (as the Autopilot app)

export async function postActivitySafely(env, sessionId, content) {
  try {
    await postActivity(env, sessionId, content);
  } catch (e) {
    // Messages carry no secrets: Linear's error text, or our own.
    console.error(`activity for session ${sessionId} failed: ${e && e.message}`);
  }
}

export async function postActivity(env, sessionId, content) {
  const data = await linearGql(
    env,
    "mutation($input: AgentActivityCreateInput!) { agentActivityCreate(input: $input) { success } }",
    { input: { agentSessionId: sessionId, content } },
  );
  if (!data.agentActivityCreate || !data.agentActivityCreate.success) throw new Error("agentActivityCreate failed");
}

export async function linearGql(env, query, variables, retried = false) {
  const token = await linearToken(env, retried);
  const res = await fetch(LINEAR_API, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ query, variables }),
    signal: AbortSignal.timeout(LINEAR_TIMEOUT_MS),
  });
  const data = await res.json().catch(() => null);
  const errors = (data && data.errors) || [];
  const authFailed = res.status === 401 || errors.some((e) => e.extensions && e.extensions.code === "AUTHENTICATION_ERROR");
  if (authFailed && !retried) return linearGql(env, query, variables, true);
  if (errors.length) throw new Error(errors.map((e) => e.message).join("; "));
  if (!res.ok || !data || !data.data) throw new Error(`Linear API HTTP ${res.status}`);
  return data.data;
}

/** A client-credentials app token, cached in D1 until a day before it expires. */
async function linearToken(env, forceNew) {
  const now = Date.now();
  if (!forceNew) {
    let cached = null;
    try {
      cached = JSON.parse((await getState(env, "linear_token")) || "null");
    } catch {
      // Unreadable: mint a new one. (No parse error text: it could quote the token.)
    }
    if (cached && cached.token && cached.exp - now > TOKEN_REFRESH_MARGIN_MS) return cached.token;
  }
  if (!env.LINEAR_CLIENT_ID || !env.LINEAR_CLIENT_SECRET) throw new Error("Linear client credentials not configured");
  const res = await fetch(LINEAR_TOKEN_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Authorization: `Basic ${btoa(`${env.LINEAR_CLIENT_ID}:${env.LINEAR_CLIENT_SECRET}`)}`,
    },
    body: new URLSearchParams({ grant_type: "client_credentials", scope: SCOPES }).toString(),
    signal: AbortSignal.timeout(LINEAR_TIMEOUT_MS),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || !data.access_token) throw new Error(`Linear token request failed (HTTP ${res.status})`);
  const exp = now + Number(data.expires_in || DEFAULT_TOKEN_LIFETIME_S) * 1000;
  await putState(env, "linear_token", JSON.stringify({ token: data.access_token, exp }));
  return data.access_token;
}

// --------------------------------------------------------------------------
// Helpers

const enc = new TextEncoder();

export async function verifySignature(secret, raw, header) {
  if (!header || !/^[0-9a-f]{64}$/i.test(header)) return false;
  const sig = new Uint8Array(header.match(/../g).map((h) => parseInt(h, 16)));
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, sig, enc.encode(raw)); // constant-time
}

async function sha256(s) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

export async function getState(env, key) {
  const row = await env.DB.prepare("SELECT value FROM state WHERE key = ?").bind(key).first();
  return row ? row.value : null;
}

export async function putState(env, key, value) {
  await env.DB.prepare(
    "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
  ).bind(key, value, Date.now()).run();
}

function clip(s, max) {
  if (s == null) return null;
  s = String(s);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** Keep the end: in a merged prompt the newest message is the one that matters. */
export function clipTail(s, max) {
  if (s == null) return null;
  s = String(s);
  return s.length > max ? `…${s.slice(s.length - max + 1)}` : s;
}

function iso(ms) {
  return ms == null ? null : new Date(ms).toISOString();
}

export function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
