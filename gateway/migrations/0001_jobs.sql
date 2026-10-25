-- Jobs: one row per Linear AgentSessionEvent worth working (a delegation, a mention,
-- or a follow-up prompt in an Autopilot session).
CREATE TABLE jobs (
  id               TEXT PRIMARY KEY,           -- random UUID
  kind             TEXT NOT NULL,              -- created | prompted
  status           TEXT NOT NULL DEFAULT 'queued', -- queued | running | done | blocked | cancelled
  session_id       TEXT NOT NULL,
  issue_id         TEXT,
  issue_identifier TEXT,
  issue_title      TEXT,
  issue_url        TEXT,
  team_key         TEXT,
  comment_id       TEXT,
  prompt           TEXT,                       -- the comment or prompt that started the job
  attempts         INTEGER NOT NULL DEFAULT 0, -- how often a run acked it
  stop_requested   INTEGER NOT NULL DEFAULT 0, -- the owner pressed Stop while it was running
  note             TEXT,                       -- why it ended (done / blocked)
  created_at       INTEGER NOT NULL,           -- epoch ms
  updated_at       INTEGER NOT NULL,
  acked_at         INTEGER,
  finished_at      INTEGER
);
CREATE INDEX jobs_open ON jobs (status, created_at);
CREATE INDEX jobs_session ON jobs (session_id, status);

-- Events already handled, so Linear's retries (1 min, 1 h, 6 h) never queue or ack twice.
-- Key: "created:<session id>" or "prompted:<activity id>".
CREATE TABLE events (
  key         TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL
);

-- Small key/value state: the cached Linear app token and the runner's last poll.
CREATE TABLE state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
