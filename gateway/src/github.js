/**
 * POST /github: GitHub App webhooks (docs/gateway.md, "GitHub").
 *
 * An @<GITHUB_APP_SLUG> mention by an allowed login, in a PR comment, review or review
 * comment, becomes a `prompted` job on the PR's Linear issue ("Part of ENG-123" in the PR
 * body, or a Linear-style branch name), in a new Autopilot session on that issue. The
 * gateway acknowledges on both sides: a thought in Linear, and 👀 plus a reply on GitHub.
 * A PR with no linked issue gets a reply and no job (owner's decision, 2026-10-19).
 *
 * Secrets (Worker secrets only, never logged): GITHUB_WEBHOOK_SECRET, GITHUB_APP_PRIVATE_KEY
 * (base64 PKCS#8 DER). Vars: GITHUB_APP_ID, GITHUB_APP_SLUG, ALLOWED_GITHUB_LOGINS.
 */

import { clipTail, expect, getState, json, linearGql, mergeIntoQueued, postActivitySafely, putState, queuedText, verifySignature } from "./index.js";

const GITHUB_API = "https://api.github.com";
const GITHUB_TIMEOUT_MS = 8000;
const PROMPT_MAX = 8000;
const HUNK_LINES = 8;
const TOKEN_MARGIN_MS = 5 * 60_000;
// "Part of ENG-123" (what Autopilot writes), or a closing keyword some PRs use.
const LINKED_RE = /\b(?:part of|fixes|fixed|closes|closed|resolves|resolved|refs?)\s*:?\s*\[?([A-Z][A-Z0-9]{0,9}-\d+)\b/i;
// Linear's suggested branch names: "<user>/eng-123-some-title".
const BRANCH_RE = /(?:^|\/)([a-z][a-z0-9]{0,9}-\d+)(?:-|$)/i;
const UNREACHABLE = "I couldn't reach Linear or GitHub just now, so nothing is queued. Mention me again in a few minutes.";
const NO_ISSUE = "I only work on PRs linked to a Linear issue. Add `Part of <ISSUE-ID>` to the PR description and mention me again.";

export async function handleGitHub(request, env, ctx) {
  if (!env.GITHUB_WEBHOOK_SECRET || !env.GITHUB_APP_SLUG) return json({ error: "GitHub webhook not configured" }, 503);
  const raw = await request.text();
  const header = request.headers.get("x-hub-signature-256") || "";
  const sig = header.startsWith("sha256=") ? header.slice(7) : "";
  if (!(await verifySignature(env.GITHUB_WEBHOOK_SECRET, raw, sig))) {
    console.warn(`github webhook rejected: bad signature (header ${header ? `${header.length} chars` : "missing"}, body ${raw.length} bytes)`);
    return json({ error: "bad signature" }, 401);
  }
  const event = request.headers.get("x-github-event") || "";
  if (event === "ping") return json({ ok: true, pong: true });
  let p;
  try {
    p = JSON.parse(raw);
  } catch {
    return json({ error: "bad json" }, 400);
  }

  const m = mentionFrom(event, p);
  if (!m) return json({ ok: true, ignored: `${event}.${p.action}` });
  // Never react to a bot: our own replies come back as issue_comment events.
  if (!p.sender || p.sender.type === "Bot") return json({ ok: true, ignored: "bot" });
  if (!mentions(m.body, env.GITHUB_APP_SLUG)) return json({ ok: true, ignored: "no mention" });

  const key = `github:${m.kind}:${m.id}`;
  if (await env.DB.prepare("SELECT 1 FROM events WHERE key = ?").bind(key).first()) return json({ ok: true, duplicate: true });
  const now = Date.now();
  const seen = env.DB.prepare("INSERT INTO events (key, received_at) VALUES (?, ?) ON CONFLICT (key) DO NOTHING").bind(key, now);
  const gh = new GitHub(env, p.installation && p.installation.id);

  const allowed = String(env.ALLOWED_GITHUB_LOGINS || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  if (!allowed.includes(String(p.sender.login).toLowerCase())) {
    await seen.run();
    ctx.waitUntil(gh.reply(m, "Sorry, I only take work from my owner here."));
    return json({ ok: true, ignored: "not an allowed login" });
  }

  // The body's reference first, then the branch's, until Linear knows one.
  let issue = await firstKnownIssue(env, linkedIssueIds(m.prBody, m.branch));
  if (issue === null && m.branch == null) {
    // issue_comment payloads have the PR's body but not its branch.
    const pr = await gh.call("GET", `/repos/${m.repo}/pulls/${m.pr}`).catch((e) => {
      console.error(`github mention: PR lookup failed: ${e && e.message}`);
      return null;
    });
    if (!pr) {
      // GitHub doesn't retry deliveries, so say so instead of going quiet (and keep it retryable).
      ctx.waitUntil(gh.reply(m, UNREACHABLE));
      return json({ ok: true, ignored: "github unreachable" });
    }
    issue = await firstKnownIssue(env, linkedIssueIds("", pr.head && pr.head.ref));
  }
  if (issue === undefined) {
    ctx.waitUntil(gh.reply(m, UNREACHABLE));
    return json({ ok: true, ignored: "linear unreachable" });
  }
  if (!issue) {
    await seen.run();
    ctx.waitUntil(gh.reply(m, NO_ISSUE));
    return json({ ok: true, ignored: "no linked issue" });
  }

  const prompt = promptText(m, p.sender.login);
  // A second mention before the runner got to the first: one job, both asks.
  const queued = await env.DB.prepare(
    "SELECT id, prompt, session_id FROM jobs WHERE issue_id = ? AND status = 'queued' ORDER BY created_at DESC LIMIT 1",
  ).bind(issue.id).first();
  if (queued) {
    if (await mergeIntoQueued(env, queued, prompt, seen, now)) {
      ctx.waitUntil(acknowledge(env, gh, m, issue, queued.id, queued.session_id, true));
      return json({ ok: true, jobId: queued.id, merged: true });
    }
  }

  // A run is working on the issue: the mention goes into that run's session (it reads it at its
  // next checkpoint), not a new one, so all progress stays in one place.
  const running = await env.DB.prepare(
    "SELECT session_id FROM jobs WHERE issue_id = ? AND status = 'running' ORDER BY created_at LIMIT 1",
  ).bind(issue.id).first();
  let sessionId = running ? running.session_id : null;
  if (!sessionId) try {
    sessionId = await createSession(env, issue.id);
  } catch (e) {
    console.error(`github mention: agent session on ${issue.identifier} failed: ${e && e.message}`);
    ctx.waitUntil(gh.reply(m, "I couldn't open a session in Linear just now, so nothing is queued. Mention me again in a few minutes."));
    return json({ ok: true, ignored: "linear session failed" });
  }
  const jobId = crypto.randomUUID();
  try {
    await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO jobs (id, kind, status, session_id, issue_id, issue_identifier, issue_title, issue_url, team_key,
                         prompt, source, github_repo, github_pr, github_comment_url, created_at, updated_at)
       VALUES (?, 'prompted', 'queued', ?, ?, ?, ?, ?, ?, ?, 'github', ?, ?, ?, ?, ?)`,
    ).bind(
      jobId, sessionId, issue.id, issue.identifier, issue.title || null, issue.url || null, (issue.team && issue.team.key) || null,
      clipTail(prompt, PROMPT_MAX), m.repo, m.pr, m.url || null, now, now,
      ),
      seen,
    ]);
  } catch (e) {
    // The session exists but the job doesn't: close it in Linear and say so on GitHub.
    console.error(`github mention: storing the job failed: ${e && e.message}`);
    ctx.waitUntil(postActivitySafely(env, sessionId, { type: "error", body: "I couldn't queue this GitHub mention. Mention me again on the PR." }));
    ctx.waitUntil(gh.reply(m, "Something went wrong queueing this, so nothing is queued. Mention me again in a few minutes."));
    return json({ error: "could not store the job" }, 500);
  }
  ctx.waitUntil(acknowledge(env, gh, m, issue, jobId, sessionId, false));
  return json({ ok: true, jobId, merged: false, issue: issue.identifier });
}

/** The comment or review in a supported event, normalised; null for anything else. */
export function mentionFrom(event, p) {
  const repo = p.repository && p.repository.full_name;
  if (!repo) return null;
  if (event === "issue_comment" && p.action === "created" && p.issue && p.issue.pull_request && p.comment) {
    return {
      kind: "comment", id: p.comment.id, body: p.comment.body || "", url: p.comment.html_url, repo,
      pr: p.issue.number, prUrl: p.issue.html_url, prBody: p.issue.body || "", branch: null,
    };
  }
  if (event === "pull_request_review" && p.action === "submitted" && p.review && p.pull_request) {
    return {
      kind: "review", id: p.review.id, body: p.review.body || "", url: p.review.html_url, state: p.review.state, repo,
      pr: p.pull_request.number, prUrl: p.pull_request.html_url, prBody: p.pull_request.body || "",
      branch: (p.pull_request.head && p.pull_request.head.ref) || null,
    };
  }
  if (event === "pull_request_review_comment" && p.action === "created" && p.comment && p.pull_request) {
    return {
      kind: "review comment", id: p.comment.id, body: p.comment.body || "", url: p.comment.html_url, repo,
      path: p.comment.path, line: p.comment.line || p.comment.original_line, hunk: p.comment.diff_hunk,
      pr: p.pull_request.number, prUrl: p.pull_request.html_url, prBody: p.pull_request.body || "",
      branch: (p.pull_request.head && p.pull_request.head.ref) || null,
    };
  }
  return null;
}

/** A live @mention: not in a quoted reply ("> ...") or in code, which repeat an older ask. */
export function mentions(body, slug) {
  const s = String(slug).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const live = String(body || "")
    .replace(/```[\s\S]*?(```|$)/g, "")
    .replace(/`[^`\n]*`/g, "")
    .split("\n").filter((l) => !/^\s*>/.test(l)).join("\n");
  return new RegExp(`(^|[^\\w/@-])@${s}(\\[bot\\])?(?![\\w-])`, "i").test(live);
}

/** Candidate Linear identifiers, in order: the PR body's reference, then the branch's. */
export function linkedIssueIds(prBody, branch) {
  const out = [];
  const a = LINKED_RE.exec(prBody || "");
  if (a) out.push(a[1].toUpperCase());
  const b = BRANCH_RE.exec(branch || "");
  if (b) out.push(b[1].toUpperCase());
  return out;
}

export function promptText(m, login) {
  const what = m.kind === "review"
    ? `reviewed (${String(m.state || "commented").toLowerCase()})`
    : m.kind === "review comment" ? `commented on ${m.path}${m.line ? `:${m.line}` : ""}` : "commented";
  const lines = [`On GitHub, @${login} ${what} on PR ${m.repo}#${m.pr} (${m.url || m.prUrl}):`, "", m.body.trim()];
  if (m.hunk) lines.push("", "Diff context:", "```diff", ...String(m.hunk).split("\n").slice(-HUNK_LINES), "```");
  return lines.join("\n");
}

async function acknowledge(env, gh, m, issue, jobId, sessionId, merged) {
  const eta = await queuedText(env, jobId, Date.now());
  await postActivitySafely(env, sessionId, {
    type: "thought",
    body: `${merged ? "Added to the queued job" : "Queued"} from a GitHub ${m.kind} on ${m.prUrl}. ${eta}`,
  });
  await gh.react(m);
  await gh.reply(m, `${merged ? "Added to the queued job for" : "Queued as"} [${issue.identifier}](${issue.url}). ${eta}`);
}

/** The first of the identifiers Linear knows; null for none, undefined when Linear is unreachable. */
async function firstKnownIssue(env, idents) {
  for (const ident of new Set(idents)) {
    const issue = await findIssue(env, ident);
    if (issue !== null) return issue;
  }
  return null;
}

/** undefined: Linear unreachable; null: no such issue. */
async function findIssue(env, ident) {
  try {
    const data = await linearGql(env, "query($id: String!) { issue(id: $id) { id identifier title url team { key } } }", { id: ident });
    return data.issue || null;
  } catch (e) {
    const msg = String(e && e.message);
    if (/not found|entity not found|could not find/i.test(msg)) return null;
    console.error(`github mention: Linear lookup of ${ident} failed: ${msg}`);
    return undefined;
  }
}

async function createSession(env, issueId) {
  // Its "created" event can come back before the job below is written.
  await expect(env, issueId);
  const data = await linearGql(
    env,
    "mutation($input: AgentSessionCreateOnIssue!) { agentSessionCreateOnIssue(input: $input) { success agentSession { id } } }",
    { input: { issueId } },
  );
  const r = data.agentSessionCreateOnIssue;
  if (!r || !r.success || !r.agentSession) throw new Error("agentSessionCreateOnIssue failed");
  return r.agentSession.id;
}

// --------------------------------------------------------------------------
// GitHub, as the app's installation

class GitHub {
  constructor(env, installationId) {
    this.env = env;
    this.installationId = installationId;
  }

  async call(method, path, body) {
    const token = await installationToken(this.env, this.installationId);
    const res = await fetch(GITHUB_API + path, {
      method,
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `token ${token}`,
        "User-Agent": "autopilot-gateway",
        "X-GitHub-Api-Version": "2022-11-28",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(GITHUB_TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`GitHub ${method} ${path.replace(/\?.*/, "")}: HTTP ${res.status}`);
    return res.status === 204 ? null : res.json();
  }

  async react(m) {
    const path = m.kind === "comment" ? `/repos/${m.repo}/issues/comments/${m.id}/reactions`
      : m.kind === "review comment" ? `/repos/${m.repo}/pulls/comments/${m.id}/reactions` : null;
    if (!path) return; // GitHub has no reactions on reviews; the reply says it
    await this.call("POST", path, { content: "eyes" }).catch((e) => console.error(`github reaction failed: ${e.message}`));
  }

  async reply(m, body) {
    const path = m.kind === "review comment"
      ? `/repos/${m.repo}/pulls/${m.pr}/comments/${m.id}/replies`
      : `/repos/${m.repo}/issues/${m.pr}/comments`;
    await this.call("POST", path, { body }).catch((e) => console.error(`github reply failed: ${e.message}`));
  }
}

/** An installation access token (1 h), cached in D1 until 5 minutes before it expires. */
async function installationToken(env, installationId) {
  if (!installationId) throw new Error("webhook without an installation id");
  const key = `github_token:${Number(installationId)}`;
  try {
    const cached = JSON.parse((await getState(env, key)) || "null");
    if (cached && cached.token && cached.exp - Date.now() > TOKEN_MARGIN_MS) return cached.token;
  } catch {
    // Unreadable: mint a new one.
  }
  if (!env.GITHUB_APP_ID || !env.GITHUB_APP_PRIVATE_KEY) throw new Error("GitHub App id or key not configured");
  const jwt = await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY, Date.now());
  const res = await fetch(`${GITHUB_API}/app/installations/${Number(installationId)}/access_tokens`, {
    method: "POST",
    headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${jwt}`, "User-Agent": "autopilot-gateway", "X-GitHub-Api-Version": "2022-11-28" },
    signal: AbortSignal.timeout(GITHUB_TIMEOUT_MS),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || !data.token) throw new Error(`GitHub installation token failed (HTTP ${res.status})`);
  const exp = Date.parse(data.expires_at) || Date.now() + 3600_000;
  await putState(env, key, JSON.stringify({ token: data.token, exp }));
  return data.token;
}

const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const enc = new TextEncoder();

export async function appJwt(appId, keyB64, now) {
  const der = Uint8Array.from(atob(String(keyB64).replace(/-----[A-Z ]+-----|\s/g, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const iat = Math.floor(now / 1000) - 60;
  const header = b64url(enc.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const claims = b64url(enc.encode(JSON.stringify({ iat, exp: iat + 600 - 60, iss: String(appId) })));
  const sig = new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(`${header}.${claims}`)));
  return `${header}.${claims}.${b64url(sig)}`;
}
