// Gateway tests: node --test (Node 22.13+). No dependencies: D1 is faked with node:sqlite
// running the real migrations, and fetch to Linear is stubbed.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { beforeEach, describe, it } from "node:test";

import worker, { queuedText, verifySignature } from "../src/index.js";
import { FakeD1 } from "./fake-d1.mjs";

const SECRET = "whsec-test";
const TOKEN = "runner-token-test";
const CLIENT_ID = "client-1";
const BASE = "https://gw.example";
const OWNER = "owner-user-1";

let env, linear, waits;

function stubLinear({ tokenStatus = 200, rejectFirst = false } = {}) {
  const calls = { tokens: 0, activities: [] };
  let rejected = !rejectFirst;
  globalThis.fetch = async (url, init) => {
    if (url === "https://api.linear.app/oauth/token") {
      calls.tokens++;
      calls.tokenAuth = init.headers.Authorization;
      calls.tokenBody = init.body;
      return Response.json(tokenStatus === 200 ? { access_token: `tok${calls.tokens}`, expires_in: 2591999 } : { error: "invalid_client" }, { status: tokenStatus });
    }
    if (!rejected) { rejected = true; return Response.json({ errors: [{ message: "auth", extensions: { code: "AUTHENTICATION_ERROR" } }] }, { status: 401 }); }
    const { variables } = JSON.parse(init.body);
    calls.activities.push({ auth: init.headers.Authorization, ...variables.input });
    return Response.json({ data: { agentActivityCreate: { success: true } } });
  };
  return calls;
}

function ctx() {
  return { waitUntil: (p) => waits.push(p) };
}

// A Linear issue id (a UUID), as POST /sessions/expect takes it.
const RUN_ISSUE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

async function settle() {
  await Promise.all(waits.splice(0));
}

function payload(over = {}) {
  return {
    type: "AgentSessionEvent",
    action: "created",
    oauthClientId: CLIENT_ID,
    appUserId: "app-1",
    organizationId: "org-1",
    webhookTimestamp: Date.now(),
    agentSession: {
      id: "sess-1",
      status: "pending",
      creatorId: OWNER,
      issue: { id: "issue-uuid-1", identifier: "ENG-200", title: "Try it", url: "https://linear.app/x/issue/ENG-200", team: { key: "ENG" } },
      comment: null,
    },
    ...over,
  };
}

function sign(raw, secret = SECRET) {
  return createHmac("sha256", secret).update(raw).digest("hex");
}

async function hook(body, { signature, raw } = {}) {
  raw = raw ?? JSON.stringify(body);
  const req = new Request(`${BASE}/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Linear-Signature": signature ?? sign(raw), "Linear-Event": "AgentSessionEvent" },
    body: raw,
  });
  const res = await worker.fetch(req, env, ctx());
  return { status: res.status, body: await res.json() };
}

async function runner(method, path, { token = TOKEN, body } = {}) {
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await worker.fetch(new Request(`${BASE}${path}`, { method, headers, body: body && JSON.stringify(body) }), env, ctx());
  return { status: res.status, body: await res.json() };
}

beforeEach(async () => {
  if (waits) await Promise.allSettled(waits); // no ack from the last test lands in this one
  env = { DB: new FakeD1(), LINEAR_WEBHOOK_SECRET: SECRET, GATEWAY_TOKEN: TOKEN, LINEAR_CLIENT_ID: CLIENT_ID, LINEAR_CLIENT_SECRET: "cs", RUNNER_INTERVAL_MIN: "10", ALLOWED_USER_IDS: OWNER };
  linear = stubLinear();
  waits = [];
});

describe("webhook signature and freshness", () => {
  it("accepts Linear's HMAC-SHA256 hex of the raw body", async () => {
    const raw = '{"a": 1}';
    assert.equal(await verifySignature(SECRET, raw, sign(raw)), true);
    assert.equal(await verifySignature(SECRET, raw, sign(raw).toUpperCase()), true);
  });

  it("rejects a wrong, missing or malformed signature, and a re-serialised body", async () => {
    const raw = '{"a": 1}';
    assert.equal(await verifySignature(SECRET, raw, sign(raw, "other")), false);
    assert.equal(await verifySignature(SECRET, raw, null), false);
    assert.equal(await verifySignature(SECRET, raw, "zz"), false);
    assert.equal(await verifySignature(SECRET, '{"a":1}', sign(raw)), false);
  });

  it("returns 401 and stores nothing for a bad signature", async () => {
    const r = await hook(payload(), { signature: sign("x") });
    assert.equal(r.status, 401);
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
    await settle();
    assert.equal(linear.activities.length, 0);
  });

  it("rejects a replayed delivery older than a minute", async () => {
    const r = await hook(payload({ webhookTimestamp: Date.now() - 61_000 }));
    assert.equal(r.status, 401);
  });

  it("fails closed (503) without a signing secret or client id", async () => {
    delete env.LINEAR_WEBHOOK_SECRET;
    assert.equal((await hook(payload())).status, 503);
    env.LINEAR_WEBHOOK_SECRET = SECRET;
    delete env.LINEAR_CLIENT_ID;
    assert.equal((await hook(payload())).status, 503);
  });

  it("queues nothing for someone not in ALLOWED_USER_IDS, and answers them", async () => {
    const r = await hook(payload({ agentSession: { ...payload().agentSession, creatorId: "stranger" } }));
    assert.equal(r.body.ignored, "not an allowed user");
    const p2 = await hook(payload({ action: "prompted", agentActivity: { id: "act-x", userId: "stranger", content: { body: "do it" } } }));
    assert.equal(p2.body.ignored, "not an allowed user");
    await settle();
    assert.equal(linear.activities[0].content.type, "response");
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
  });

  it("ignores a prompt without an activity id instead of colliding on one key", async () => {
    assert.equal((await hook(payload({ action: "prompted", agentActivity: null }))).body.ignored, "prompt without activity");
  });

  it("ignores other event types and other apps with 200", async () => {
    assert.deepEqual((await hook(payload({ type: "Issue" }))).body, { ok: true, ignored: "Issue" });
    assert.equal((await hook(payload({ oauthClientId: "someone-else" }))).body.ignored, "another app");
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
  });
});

describe("queueing and acknowledging", () => {
  it("stores a delegation and posts a Queued thought into its session", async () => {
    const r = await hook(payload());
    assert.equal(r.status, 200);
    await settle();
    assert.equal(linear.activities.length, 1);
    const a = linear.activities[0];
    assert.equal(a.agentSessionId, "sess-1");
    assert.equal(a.content.type, "thought");
    assert.match(a.content.body, /^Queued; my runner hasn't checked in yet/);
    assert.equal(a.auth, "Bearer tok1");
    // client credentials, the runner's scope set, credentials in Basic auth only
    assert.equal(linear.tokenAuth, `Basic ${btoa(`${CLIENT_ID}:cs`)}`);
    assert.equal(linear.tokenBody, "grant_type=client_credentials&scope=read%2Cwrite%2Capp%3Aassignable%2Capp%3Amentionable");

    const next = await runner("GET", "/jobs/next");
    assert.equal(next.body.pending, 1);
    assert.equal(next.body.job.id, r.body.jobId);
    assert.equal(next.body.job.identifier, "ENG-200");
    assert.equal(next.body.job.sessionId, "sess-1");
    assert.equal(next.body.job.kind, "created");
    assert.equal(next.body.job.status, "queued");
  });

  it("says within N minutes once the runner polls", async () => {
    await runner("GET", "/jobs/next");
    await hook(payload());
    await settle();
    assert.equal(linear.activities[0].content.body, "Queued; I'll start within 10 minutes.");
  });

  it("counts down from the last poll and flags a runner that stopped polling", async () => {
    const { jobId } = (await hook(payload())).body;
    await settle();
    const db = env.DB.db;
    db.prepare("INSERT INTO state (key, value, updated_at) VALUES ('last_poll', ?, 0)").run(String(Date.now() - 4 * 60_000));
    assert.equal(await queuedText(env, jobId, Date.now()), "Queued; I'll start within 6 minutes.");
    db.prepare("UPDATE state SET value = ? WHERE key = 'last_poll'").run(String(Date.now() - 95 * 60_000));
    assert.match(await queuedText(env, jobId, Date.now()), /last checked in 95 minutes ago \(the Mac may be asleep\)/);
  });

  it("tells a second job how many are ahead of it", async () => {
    await hook(payload());
    await settle(); // the activities are asserted by position: don't let the two acks race
    await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-2", issue: { id: "issue-uuid-2", identifier: "ENG-201", title: "Other", url: "https://linear.app/x/issue/ENG-201", team: { key: "ENG" } } } }));
    await settle();
    assert.equal(linear.activities[1].content.body, "Queued behind 1 other job; I'll start when it's done.");
  });

  it("keeps the mention's comment as the job's prompt", async () => {
    const s = { ...payload().agentSession, comment: { id: "c-1", body: "@Autopilot please retry" }, commentId: "c-1" };
    await hook(payload({ agentSession: s }));
    const job = (await runner("GET", "/jobs/next")).body.job;
    assert.equal(job.commentId, "c-1");
    assert.equal(job.prompt, "@Autopilot please retry");
  });

  it("does not queue or ack a retried delivery twice", async () => {
    const body = payload();
    await hook(body);
    const again = await hook({ ...body, webhookTimestamp: Date.now() });
    assert.deepEqual(again.body, { ok: true, duplicate: true });
    await settle();
    assert.equal(linear.activities.length, 1);
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 1);
  });

  it("queues a follow-up prompt once the first job finished, and merges into a still-queued one", async () => {
    const first = (await hook(payload())).body.jobId;
    const prompt = (id, body) => payload({ action: "prompted", agentActivity: { id, userId: OWNER, agentSessionId: "sess-1", content: { type: "prompt", body } } });
    await settle(); // the activities are asserted by position: don't let the acks race
    const merged = await hook(prompt("act-1", "also do X"));
    assert.deepEqual([merged.body.jobId, merged.body.merged], [first, true]);
    await settle();
    await runner("POST", `/jobs/${first}/ack`);
    await runner("POST", `/jobs/${first}/blocked`, { body: { note: "asked a question" } });
    const second = await hook(prompt("act-2", "answer: yes"));
    assert.notEqual(second.body.jobId, first);
    const job = (await runner("GET", "/jobs/next")).body.job;
    assert.equal(job.kind, "prompted");
    assert.equal(job.prompt, "answer: yes");
    await settle();
    assert.match(linear.activities[1].content.body, /^Added to the queued job\. /);
  });

  it("cancels queued work on a stop signal and confirms with a response", async () => {
    await hook(payload());
    await settle(); // else the Queued thought may land after the stop's response
    await hook(payload({ action: "prompted", agentActivity: { id: "act-s", userId: OWNER, signal: "stop", content: { type: "prompt", body: "stop" } } }));
    await settle();
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
    assert.deepEqual(linear.activities.at(-1).content, { type: "response", body: "Stopped. Nothing is queued for this any more; mention me to start again." });
  });

  it("flags a running job on stop, so the runner can see it", async () => {
    const id = (await hook(payload())).body.jobId;
    await settle(); // else the Queued thought may land after the stop's reply
    await runner("POST", `/jobs/${id}/ack`);
    await hook(payload({ action: "prompted", agentActivity: { id: "act-s", userId: OWNER, signal: "stop", content: { body: "stop" } } }));
    await settle();
    const job = (await runner("GET", `/jobs/${id}`)).body.job;
    assert.equal(job.status, "running");
    assert.equal(job.stopRequested, true);
    assert.match(linear.activities.at(-1).content.body, /stops at its next checkpoint/);
  });

  it("does not merge a reply into a job the runner took in the meantime", async () => {
    const first = (await hook(payload())).body.jobId;
    // The runner acks between the webhook's SELECT and its UPDATE.
    const realPrepare = env.DB.prepare.bind(env.DB);
    env.DB.prepare = (sql) => {
      if (sql.startsWith("SELECT id, prompt FROM jobs")) env.DB.db.prepare("UPDATE jobs SET status = 'running' WHERE id = ?").run(first);
      return realPrepare(sql);
    };
    const r = await hook(payload({ action: "prompted", agentActivity: { id: "act-r", userId: OWNER, content: { body: "late reply" } } }));
    env.DB.prepare = realPrepare;
    assert.equal(r.body.merged, false);
    assert.notEqual(r.body.jobId, first);
    const jobs = (await runner("GET", "/jobs/next")).body.jobs;
    assert.equal(jobs.find((j) => j.id === r.body.jobId).prompt, "late reply");
  });

  it("keeps the newest text when a merged prompt is too long", async () => {
    await hook(payload());
    const prompt = (id, body) => payload({ action: "prompted", agentActivity: { id, userId: OWNER, content: { body } } });
    const second = (await hook(prompt("act-1", "a".repeat(5000)))).body.jobId;
    await hook(prompt("act-2", "b".repeat(5000) + "END"));
    const job = (await runner("GET", `/jobs/${second}`)).body.job;
    assert.ok(job.prompt.endsWith("END"));
    assert.equal(job.prompt.length, 8000);
  });

  it("leaves no event key behind when storing the job fails, so Linear's retry queues it", async () => {
    const body = payload();
    env.DB.db.exec("ALTER TABLE jobs RENAME TO jobs_gone");
    assert.equal((await hook(body)).status, 500);
    env.DB.db.exec("ALTER TABLE jobs_gone RENAME TO jobs");
    const retry = await hook({ ...body, webhookTimestamp: Date.now() });
    assert.equal(retry.status, 200);
    assert.ok(retry.body.jobId);
  });

  it("answers a session without an issue and queues nothing", async () => {
    await hook(payload({ agentSession: { id: "sess-doc", creatorId: OWNER, issue: null } }));
    await settle();
    assert.equal(linear.activities[0].content.type, "response");
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
  });

  it("caches the Linear token and re-mints once after a 401", async () => {
    await hook(payload());
    await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-2" } }));
    await settle();
    assert.equal(linear.tokens, 1);
    linear = stubLinear({ rejectFirst: true });
    await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-3" } }));
    await settle();
    assert.equal(linear.tokens, 1);
    assert.equal(linear.activities[0].auth, "Bearer tok1");
  });

  it("still returns 200 and keeps the job when Linear is down", async () => {
    linear = stubLinear({ tokenStatus: 500 });
    const r = await hook(payload());
    assert.equal(r.status, 200);
    await settle();
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 1);
  });
});

describe("runner API", () => {
  it("needs the shared token", async () => {
    assert.equal((await runner("GET", "/jobs/next", { token: null })).status, 401);
    assert.equal((await runner("GET", "/jobs/next", { token: "wrong" })).status, 401);
    delete env.GATEWAY_TOKEN;
    assert.equal((await runner("GET", "/jobs/next")).status, 503);
  });

  it("acks, then finishes a job; a finished job can't be acked again", async () => {
    const id = (await hook(payload())).body.jobId;
    const ack = await runner("POST", `/jobs/${id}/ack`);
    assert.equal(ack.body.job.status, "running");
    assert.equal(ack.body.job.attempts, 1);
    const done = await runner("POST", `/jobs/${id}/done`, { body: { note: "PR merged" } });
    assert.equal(done.body.job.status, "done");
    assert.equal(done.body.job.note, "PR merged");
    const again = await runner("POST", `/jobs/${id}/ack`);
    assert.equal(again.status, 409);
    // A retried done after a lost reply is not an error; flipping to blocked is.
    assert.equal((await runner("POST", `/jobs/${id}/done`)).status, 200);
    assert.equal((await runner("POST", `/jobs/${id}/blocked`)).status, 409);
    assert.equal((await runner("GET", "/jobs/next")).body.job, null);
    assert.equal((await runner("POST", "/jobs/00000000-0000-0000-0000-000000000000/done")).status, 404);
  });

  it("offers an interrupted (running) job before older queued ones, and counts re-acks", async () => {
    const a = (await hook(payload())).body.jobId;
    const b = (await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-2", issue: { id: "issue-uuid-2", identifier: "ENG-201", title: "Other", url: "https://linear.app/x/issue/ENG-201", team: { key: "ENG" } } } }))).body.jobId;
    await runner("POST", `/jobs/${b}/ack`);
    const next = (await runner("GET", "/jobs/next")).body;
    assert.equal(next.job.id, b);
    assert.equal(next.pending, 2);
    assert.deepEqual(next.jobs.map((j) => j.id), [b, a]);
    assert.equal((await runner("POST", `/jobs/${b}/ack`)).body.job.attempts, 2);
    assert.notEqual(a, b);
  });

  it("takes the owner's reply about started work before newer and older new tickets", async () => {
    const prompt = (id, body) => payload({ action: "prompted", agentActivity: { id, userId: OWNER, agentSessionId: "sess-1", content: { type: "prompt", body } } });
    const first = (await hook(payload())).body.jobId;
    await runner("POST", `/jobs/${first}/ack`);
    await runner("POST", `/jobs/${first}/blocked`, { body: { note: "asked a question" } });
    const fresh = (await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-2" } }))).body.jobId;
    const reply = (await hook(prompt("act-9", "option 2, please"))).body.jobId;
    const next = (await runner("GET", "/jobs/next")).body;
    assert.deepEqual(next.jobs.map((j) => j.id), [reply, fresh]);
    assert.equal(next.job.id, reply);
    // The reply is told it's next; the new ticket now waits behind it.
    assert.doesNotMatch(await queuedText(env, reply, Date.now()), /behind/);
    assert.match(await queuedText(env, fresh, Date.now()), /^Queued behind 1 other job/);
  });

  it("ignores the created event of a session Autopilot opened itself, without answering", async () => {
    env.APP_USER_ID = "app-user-9";
    const r = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-gh", creatorId: "app-user-9" } }));
    assert.equal(r.body.ignored, "own session");
    await settle();
    assert.equal(linear.activities.length, 0);
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 0);
    // The owner's own sessions still queue.
    assert.ok((await hook(payload())).body.jobId);
  });

  it("answers, rather than drops, a session with no creator it doesn't know", async () => {
    env.APP_USER_ID = "app-user-9";
    const r = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-x", creatorId: undefined } }));
    assert.equal(r.body.ignored, "not an allowed user");
    await settle();
    assert.equal(linear.activities[0].content.type, "response");
  });

  it("ignores a creator-less event for a session that already has a job (a GitHub mention)", async () => {
    const now = Date.now();
    await env.DB.prepare(
      "INSERT INTO jobs (id, kind, status, session_id, source, created_at, updated_at) VALUES ('j-gh', 'prompted', 'queued', 'sess-gh2', 'github', ?, ?)",
    ).bind(now, now).run();
    const r = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-gh2", creatorId: undefined } }));
    assert.equal(r.body.ignored, "own session");
    await settle();
    assert.equal(linear.activities.length, 0);
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 1);
  });

  it("ignores the creator-less session a run announced for that issue, once", async () => {
    const own = (id) => payload({ agentSession: { ...payload().agentSession, id, creatorId: undefined, issue: { ...payload().agentSession.issue, id: RUN_ISSUE } } });
    const r = await runner("POST", "/sessions/expect", { body: { issueId: RUN_ISSUE } });
    assert.equal(r.status, 200);
    assert.equal(r.body.issueId, RUN_ISSUE);
    assert.equal((await hook(own("sess-run"))).body.ignored, "own session");
    await settle();
    assert.equal(linear.activities.length, 0);
    // Used up: a second creator-less session on the issue is answered again.
    assert.equal((await hook(own("sess-other"))).body.ignored, "not an allowed user");
  });

  it("answers a creator-less session whose announcement has expired", async () => {
    await env.DB.prepare("INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)").bind(`expect:${RUN_ISSUE}`, String(Date.now() - 3 * 60_000), 0).run();
    const r = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-late", creatorId: undefined, issue: { ...payload().agentSession.issue, id: RUN_ISSUE } } }));
    assert.equal(r.body.ignored, "not an allowed user");
  });

  it("an announcement doesn't cover a session someone else created", async () => {
    await runner("POST", "/sessions/expect", { body: { issueId: RUN_ISSUE } });
    const r = await hook(payload({ agentSession: { ...payload().agentSession, creatorId: "stranger", issue: { ...payload().agentSession.issue, id: RUN_ISSUE } } }));
    assert.equal(r.body.ignored, "not an allowed user");
  });

  it("only the runner can announce a session, and only for an issue id", async () => {
    assert.equal((await runner("POST", "/sessions/expect", { token: "wrong", body: { issueId: RUN_ISSUE } })).status, 401);
    assert.equal((await runner("POST", "/sessions/expect", { body: { issueId: "ENG-67" } })).status, 400);
    assert.equal((await runner("POST", "/sessions/expect", { body: {} })).status, 400);
  });

  it("joins a second delegation of the same issue to its queued job, and points the new session there", async () => {
    const first = (await hook(payload())).body.jobId;
    const again = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-dup", comment: { id: "c-9", body: "@Autopilot also check X" } } }));
    assert.deepEqual([again.body.jobId, again.body.merged, again.body.joined], [first, true, "sess-1"]);
    const next = (await runner("GET", "/jobs/next")).body;
    assert.equal(next.pending, 1);
    assert.match(next.job.prompt, /also check X/);
    await settle();
    const toDup = linear.activities.filter((a) => a.agentSessionId === "sess-dup");
    assert.equal(toDup.length, 1);
    assert.equal(toDup[0].content.type, "response");
    assert.match(toDup[0].content.body, /earlier Autopilot session/);
  });

  it("turns a note on a running issue into a reply in the run's session", async () => {
    const first = (await hook(payload())).body.jobId;
    await runner("POST", `/jobs/${first}/ack`);
    const again = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-dup", comment: { id: "c-9", body: "@Autopilot use option 2" } } }));
    assert.equal(again.body.joined, "sess-1");
    const jobs = (await runner("GET", "/jobs/next")).body.jobs;
    const reply = jobs.find((j) => j.id === again.body.jobId);
    assert.deepEqual([reply.kind, reply.sessionId], ["prompted", "sess-1"]);
    // A bare re-delegation of a running issue queues nothing new.
    const bare = await hook(payload({ agentSession: { ...payload().agentSession, id: "sess-dup2" } }));
    assert.equal(bare.body.jobId, first);
    assert.equal((await runner("GET", "/jobs/next")).body.pending, 2);
  });

  it("records the poll time and serves health without auth", async () => {
    await runner("GET", "/jobs/next");
    assert.ok(env.DB.db.prepare("SELECT value FROM state WHERE key = 'last_poll'").get());
    const res = await worker.fetch(new Request(`${BASE}/health`), env, ctx());
    assert.deepEqual(await res.json(), { ok: true });
    assert.equal((await worker.fetch(new Request(`${BASE}/jobs`), env, ctx())).status, 404);
  });
});
