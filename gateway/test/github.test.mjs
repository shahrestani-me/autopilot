// GitHub webhook tests (src/github.js): node --test, no dependencies. Linear and GitHub are
// stubbed at fetch; the app key is generated per run.
import assert from "node:assert/strict";
import { createHmac, createPublicKey, generateKeyPairSync, verify } from "node:crypto";
import { beforeEach, describe, it } from "node:test";

import worker from "../src/index.js";
import { appJwt, linkedIssueIds, mentions, promptText } from "../src/github.js";
import { FakeD1 } from "./fake-d1.mjs";

const SECRET = "gh-whsec-test";
const TOKEN = "runner-token-test";
const BASE = "https://gw.example";
const SLUG = "example-autopilot";
const OWNER_LOGIN = "octo-owner";
const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const KEY_B64 = privateKey.export({ type: "pkcs8", format: "der" }).toString("base64");
const ISSUE = { id: "issue-uuid-1", identifier: "ENG-200", title: "Try it", url: "https://linear.app/x/issue/ENG-200", team: { key: "ENG" } };

let env, calls, waits, linearDown, issues;

function stub() {
  calls = { linear: [], github: [], tokens: 0, sessions: 0 };
  globalThis.fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body.startsWith?.("{") ? init.body : "{}") : null;
    if (url === "https://api.linear.app/oauth/token") {
      if (linearDown) return Response.json({ error: "down" }, { status: 503 });
      return Response.json({ access_token: "lin-tok", expires_in: 2591999 });
    }
    if (url === "https://api.linear.app/graphql") {
      calls.linear.push(body);
      if (body.query.includes("issue(id:")) {
        const found = issues[body.variables.id];
        return Response.json(found ? { data: { issue: found } } : { data: { issue: null }, errors: [{ message: "Entity not found: Issue" }] });
      }
      if (body.query.includes("agentSessionCreateOnIssue")) {
        calls.sessions++;
        return Response.json({ data: { agentSessionCreateOnIssue: { success: true, agentSession: { id: `sess-gh-${calls.sessions}` } } } });
      }
      return Response.json({ data: { agentActivityCreate: { success: true } } });
    }
    if (url.startsWith("https://api.github.com/app/installations/")) {
      calls.tokens++;
      calls.jwt = init.headers.Authorization.replace(/^Bearer /, "");
      calls.installationUrl = url;
      return Response.json({ token: "ghs_test", expires_at: new Date(Date.now() + 3600_000).toISOString() }, { status: 201 });
    }
    if (url.startsWith("https://api.github.com/")) {
      calls.github.push({ method: init.method, path: url.slice("https://api.github.com".length), body, auth: init.headers.Authorization });
      if (init.method === "GET" && /\/pulls\/\d+$/.test(url)) return Response.json({ body: "", head: { ref: "jdoe/eng-200-try-it" } });
      return Response.json({ id: 1 }, { status: 201 });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
}

function ctx() {
  return { waitUntil: (p) => waits.push(p) };
}

async function settle() {
  await Promise.all(waits.splice(0));
}

const sign = (raw, secret = SECRET) => `sha256=${createHmac("sha256", secret).update(raw).digest("hex")}`;

async function gh(event, body, { signature, raw } = {}) {
  raw = raw ?? JSON.stringify(body);
  const req = new Request(`${BASE}/github`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": signature ?? sign(raw) },
    body: raw,
  });
  const res = await worker.fetch(req, env, ctx());
  return { status: res.status, body: await res.json() };
}

async function jobs() {
  const res = await worker.fetch(new Request(`${BASE}/jobs/next`, { headers: { Authorization: `Bearer ${TOKEN}` } }), env, ctx());
  return (await res.json()).jobs;
}

const repository = { full_name: "example-org/autopilot-sandbox" };
const installation = { id: 4242 };
const owner = { login: OWNER_LOGIN, type: "User" };

function issueComment({ id = 11, body = `@${SLUG} please fix the test`, prBody = "Adds a thing.\n\nPart of ENG-200 — https://linear.app/x/issue/ENG-200", sender = owner, pr = true } = {}) {
  return {
    action: "created", repository, installation, sender,
    issue: { number: 7, html_url: "https://github.com/example-org/autopilot-sandbox/pull/7", body: prBody, ...(pr ? { pull_request: { url: "x" } } : {}) },
    comment: { id, body, html_url: `https://github.com/example-org/autopilot-sandbox/pull/7#issuecomment-${id}`, user: sender },
  };
}

function reviewComment() {
  return {
    action: "created", repository, installation, sender: owner,
    pull_request: { number: 7, html_url: "https://github.com/example-org/autopilot-sandbox/pull/7", body: "Part of ENG-200", head: { ref: "jdoe/autopilot-x" } },
    comment: { id: 99, body: `@${SLUG} rename this`, html_url: "https://github.com/x/pull/7#discussion_r99", path: "src/a.py", line: 12, diff_hunk: "@@ -1,2 +1,2 @@\n-old\n+new" },
  };
}

beforeEach(async () => {
  if (waits) await Promise.allSettled(waits);
  env = {
    DB: new FakeD1(), GATEWAY_TOKEN: TOKEN, LINEAR_CLIENT_ID: "c", LINEAR_CLIENT_SECRET: "cs", RUNNER_INTERVAL_MIN: "10",
    GITHUB_WEBHOOK_SECRET: SECRET, GITHUB_APP_ID: "123456", GITHUB_APP_SLUG: SLUG, GITHUB_APP_PRIVATE_KEY: KEY_B64,
    ALLOWED_GITHUB_LOGINS: OWNER_LOGIN,
  };
  waits = [];
  linearDown = false;
  issues = { "ENG-200": ISSUE };
  stub();
});

describe("GitHub webhook: authenticity", () => {
  it("rejects a bad or missing X-Hub-Signature-256 and stores nothing", async () => {
    assert.equal((await gh("issue_comment", issueComment(), { signature: sign("x") })).status, 401);
    assert.equal((await gh("issue_comment", issueComment(), { signature: "" })).status, 401);
    assert.equal((await gh("issue_comment", issueComment(), { signature: sign(JSON.stringify(issueComment()), "other") })).status, 401);
    assert.equal((await jobs()).length, 0);
  });

  it("fails closed (503) when not configured, and answers ping", async () => {
    assert.deepEqual((await gh("ping", { zen: "hi" })).body, { ok: true, pong: true });
    delete env.GITHUB_WEBHOOK_SECRET;
    assert.equal((await gh("ping", { zen: "hi" })).status, 503);
  });

  it("signs the app JWT with RS256 so GitHub's public key verifies it", async () => {
    const now = Date.now();
    const jwt = await appJwt("123456", KEY_B64, now);
    const [h, c, s] = jwt.split(".");
    const pub = createPublicKey(privateKey);
    assert.ok(verify("sha256", Buffer.from(`${h}.${c}`), pub, Buffer.from(s, "base64url")));
    const claims = JSON.parse(Buffer.from(c, "base64url"));
    assert.equal(claims.iss, "123456");
    assert.ok(claims.exp - claims.iat <= 600);
    assert.ok(claims.iat <= Math.floor(now / 1000));
  });
});

describe("GitHub webhook: mentions become jobs", () => {
  it("queues an owner's PR comment mention on the linked Linear issue and acknowledges on both sides", async () => {
    const r = await gh("issue_comment", issueComment());
    assert.equal(r.status, 200);
    assert.equal(r.body.issue, "ENG-200");
    await settle();
    const [job] = await jobs();
    assert.equal(job.id, r.body.jobId);
    assert.equal(job.kind, "prompted");
    assert.equal(job.source, "github");
    assert.equal(job.sessionId, "sess-gh-1");
    assert.equal(job.identifier, "ENG-200");
    assert.deepEqual(job.github, { repo: "example-org/autopilot-sandbox", pr: 7, commentUrl: "https://github.com/example-org/autopilot-sandbox/pull/7#issuecomment-11" });
    assert.match(job.prompt, /^On GitHub, @octo-owner commented on PR example-org\/autopilot-sandbox#7 /);
    assert.match(job.prompt, /please fix the test$/);
    // Linear: a thought in the new session
    const act = calls.linear.find((b) => b.query.includes("agentActivityCreate"));
    assert.equal(act.variables.input.agentSessionId, "sess-gh-1");
    assert.match(act.variables.input.content.body, /^Queued from a GitHub comment on https:\/\/github.com\/.*\/pull\/7\. /);
    // GitHub: 👀 on the comment and a reply, as the installation
    assert.deepEqual(calls.github.map((c) => [c.method, c.path]), [
      ["POST", "/repos/example-org/autopilot-sandbox/issues/comments/11/reactions"],
      ["POST", "/repos/example-org/autopilot-sandbox/issues/7/comments"],
    ]);
    assert.equal(calls.github[0].body.content, "eyes");
    assert.match(calls.github[1].body.body, /^Queued as \[ENG-200\]\(https:\/\/linear.app\/x\/issue\/ENG-200\)\. /);
    assert.equal(calls.github[1].auth, "token ghs_test");
    assert.equal(calls.installationUrl, "https://api.github.com/app/installations/4242/access_tokens");
  });

  it("caches the installation token", async () => {
    await gh("issue_comment", issueComment());
    await settle();
    await gh("issue_comment", issueComment({ id: 12 }));
    await settle();
    assert.equal(calls.tokens, 1);
  });

  it("puts a mention on an issue being worked on into the run's session, not a new one", async () => {
    const first = (await gh("issue_comment", issueComment())).body.jobId;
    await settle();
    await env.DB.prepare("UPDATE jobs SET status = 'running' WHERE id = ?").bind(first).run();
    const second = await gh("issue_comment", issueComment({ id: 12, body: `@${SLUG} also the docs` }));
    await settle();
    assert.equal(calls.sessions, 1);  // no second Linear session
    const job = (await jobs()).find((j) => j.id === second.body.jobId);
    assert.equal(job.sessionId, "sess-gh-1");
    assert.equal(job.status, "queued");
  });

  it("merges a second mention into the still-queued job", async () => {
    const first = (await gh("issue_comment", issueComment())).body.jobId;
    await settle(); // else the two replies race and at(-1) may be the first one's
    const second = await gh("issue_comment", issueComment({ id: 12, body: `@${SLUG} and the docs` }));
    assert.deepEqual([second.body.jobId, second.body.merged], [first, true]);
    await settle();
    const [job] = await jobs();
    assert.match(job.prompt, /please fix the test\n\n---\n\nOn GitHub, .*\n\n@example-autopilot and the docs$/s);
    assert.equal(calls.sessions, 1);
    assert.match(calls.github.at(-1).body.body, /^Added to the queued job for \[ENG-200\]/);
  });

  it("finds the issue from a Linear-style branch when the body has none", async () => {
    const r = await gh("issue_comment", issueComment({ prBody: "no reference here" }));
    assert.equal(r.body.issue, "ENG-200");
    assert.deepEqual([calls.github[0].method, calls.github[0].path], ["GET", "/repos/example-org/autopilot-sandbox/pulls/7"]);
  });

  it("replies to a review comment in its thread, with the file, line and diff in the prompt", async () => {
    const r = await gh("pull_request_review_comment", reviewComment());
    await settle();
    const [job] = await jobs();
    assert.equal(job.id, r.body.jobId);
    assert.match(job.prompt, /commented on src\/a\.py:12 on PR/);
    assert.match(job.prompt, /```diff\n@@ -1,2 \+1,2 @@\n-old\n\+new\n```/);
    assert.deepEqual(calls.github.map((c) => c.path), [
      "/repos/example-org/autopilot-sandbox/pulls/comments/99/reactions",
      "/repos/example-org/autopilot-sandbox/pulls/7/comments/99/replies",
    ]);
  });

  it("takes a submitted review with a mention (reply only: reviews have no reactions)", async () => {
    const p = { ...reviewComment(), action: "submitted", review: { id: 5, body: `@${SLUG} changes please`, html_url: "https://github.com/x/pull/7#pullrequestreview-5", state: "CHANGES_REQUESTED" } };
    delete p.comment;
    const r = await gh("pull_request_review", p);
    await settle();
    assert.ok(r.body.jobId);
    assert.match((await jobs())[0].prompt, /reviewed \(changes_requested\) on PR/);
    assert.deepEqual(calls.github.map((c) => c.path), ["/repos/example-org/autopilot-sandbox/issues/7/comments"]);
  });

  it("does not queue a redelivered comment twice", async () => {
    await gh("issue_comment", issueComment());
    assert.deepEqual((await gh("issue_comment", issueComment())).body, { ok: true, duplicate: true });
    assert.equal((await jobs()).length, 1);
  });
});

describe("GitHub webhook: what queues nothing", () => {
  it("ignores comments without a mention, bots (our own replies), plain issues and other events", async () => {
    assert.equal((await gh("issue_comment", issueComment({ body: "looks good" }))).body.ignored, "no mention");
    assert.equal((await gh("issue_comment", issueComment({ sender: { login: `${SLUG}[bot]`, type: "Bot" } }))).body.ignored, "bot");
    assert.equal((await gh("issue_comment", issueComment({ pr: false }))).body.ignored, "issue_comment.created");
    assert.equal((await gh("issue_comment", { ...issueComment(), action: "edited" })).body.ignored, "issue_comment.created".replace("created", "edited"));
    assert.equal((await gh("push", { repository })).body.ignored, "push.undefined");
    await settle();
    assert.equal((await jobs()).length, 0);
    assert.equal(calls.github.length, 0);
  });

  it("answers someone not in ALLOWED_GITHUB_LOGINS and queues nothing", async () => {
    const r = await gh("issue_comment", issueComment({ sender: { login: "stranger", type: "User" } }));
    assert.equal(r.body.ignored, "not an allowed login");
    await settle();
    assert.equal((await jobs()).length, 0);
    assert.equal(calls.github[0].body.body, "Sorry, I only take work from my owner here.");
  });

  it("replies on a PR with no linked Linear issue and queues nothing (owner's decision)", async () => {
    stub();
    globalThis.fetch = ((orig) => async (url, init) => {
      if (/\/pulls\/\d+$/.test(url) && (!init.method || init.method === "GET")) return Response.json({ body: "", head: { ref: "feature/no-ticket" } });
      return orig(url, init);
    })(globalThis.fetch);
    const r = await gh("issue_comment", issueComment({ prBody: "no reference" }));
    assert.equal(r.body.ignored, "no linked issue");
    await settle();
    assert.equal((await jobs()).length, 0);
    assert.match(calls.github.at(-1).body.body, /^I only work on PRs linked to a Linear issue\. Add `Part of <ISSUE-ID>`/);
  });

  it("falls back to the branch when Linear doesn't know the body's reference, and gives up when it knows neither", async () => {
    const r = await gh("issue_comment", issueComment({ prBody: "Refs UTF-8 handling" }));
    assert.equal(r.body.issue, "ENG-200"); // from jdoe/eng-200-try-it
    issues = {};
    const r2 = await gh("issue_comment", issueComment({ id: 13, prBody: "Part of ENG-999" }));
    assert.equal(r2.body.ignored, "no linked issue");
  });

  it("says so, and keeps the comment retryable, when GitHub won't show the PR", async () => {
    globalThis.fetch = ((orig) => async (url, init) => {
      if (/\/pulls\/\d+$/.test(url) && (!init.method || init.method === "GET")) return new Response("boom", { status: 502 });
      return orig(url, init);
    })(globalThis.fetch);
    const r = await gh("issue_comment", issueComment({ prBody: "no reference" }));
    assert.equal(r.body.ignored, "github unreachable");
    await settle();
    assert.match(calls.github.at(-1).body.body, /couldn't reach Linear or GitHub/);
    stub();
    assert.equal((await gh("issue_comment", issueComment({ prBody: "no reference" }))).body.issue, "ENG-200");
  });

  it("closes the new Linear session and says so on GitHub when storing the job fails", async () => {
    // e.g. the Worker deployed before migration 0002: the INSERT fails, after the session exists.
    const realBatch = env.DB.batch.bind(env.DB);
    env.DB.batch = async () => { throw new Error("D1_ERROR: table jobs has no column named source"); };
    const r = await gh("issue_comment", issueComment());
    env.DB.batch = realBatch;
    assert.equal(r.status, 500);
    await settle();
    const act = calls.linear.find((b) => b.query.includes("agentActivityCreate"));
    assert.deepEqual([act.variables.input.agentSessionId, act.variables.input.content.type], ["sess-gh-1", "error"]);
    assert.match(calls.github.at(-1).body.body, /nothing is queued/);
    assert.ok((await gh("issue_comment", issueComment())).body.jobId); // not marked seen
  });

  it("says so when Linear is unreachable, and lets the same comment queue later", async () => {
    linearDown = true;
    const r = await gh("issue_comment", issueComment());
    assert.equal(r.body.ignored, "linear unreachable");
    await settle();
    assert.match(calls.github.at(-1).body.body, /couldn't reach Linear/);
    linearDown = false;
    assert.ok((await gh("issue_comment", issueComment())).body.jobId);
  });
});

describe("GitHub webhook: parsing", () => {
  it("matches only a real @mention of the app", () => {
    assert.ok(mentions(`@${SLUG} go`, SLUG));
    assert.ok(mentions(`thanks, @${SLUG.toUpperCase()}!`, SLUG));
    assert.ok(mentions(`(@${SLUG}[bot])`, SLUG));
    assert.ok(!mentions(`@${SLUG}-other go`, SLUG));
    assert.ok(!mentions(`mail me@${SLUG}.dev`, SLUG));
    assert.ok(!mentions(`see github.com/@${SLUG}`, SLUG));
    assert.ok(!mentions("@autopilot go", SLUG));
    // quoting an earlier ask, or writing the handle in code, isn't a new ask
    assert.ok(!mentions(`> @${SLUG} fix the test\n\nThanks, that worked.`, SLUG));
    assert.ok(!mentions(`Mention \`@${SLUG}\` to start it.`, SLUG));
    assert.ok(!mentions(`\`\`\`\n@${SLUG} go\n\`\`\``, SLUG));
    assert.ok(mentions(`> old text\n@${SLUG} now this`, SLUG));
  });

  it("links the issue from the PR body first, then a Linear-style branch", () => {
    assert.deepEqual(linkedIssueIds("Part of ENG-123 — https://linear.app/x", "jdoe/eng-9-x"), ["ENG-123", "ENG-9"]);
    assert.deepEqual(linkedIssueIds("Fixes: [ENG-5]", null), ["ENG-5"]);
    assert.deepEqual(linkedIssueIds("UTF-8 everywhere", "jdoe/eng-77-try"), ["ENG-77"]);
    assert.deepEqual(linkedIssueIds("", "jdoe/autopilot-slug"), []);
    assert.deepEqual(linkedIssueIds(null, null), []);
  });

  it("keeps the last lines of a long diff hunk", () => {
    const hunk = Array.from({ length: 20 }, (_, i) => `+line${i}`).join("\n");
    const p = promptText({ kind: "review comment", repo: "o/r", pr: 1, url: "u", body: "x", path: "a", line: 3, hunk }, "me");
    assert.ok(p.includes("+line19") && !p.includes("+line11\n"));
  });
});
