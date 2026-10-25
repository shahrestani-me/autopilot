-- GitHub App mentions (src/github.js): where a job came from, and the PR it is about.
ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'linear'; -- linear | github
ALTER TABLE jobs ADD COLUMN github_repo TEXT;         -- owner/name
ALTER TABLE jobs ADD COLUMN github_pr INTEGER;
ALTER TABLE jobs ADD COLUMN github_comment_url TEXT;  -- the comment or review that asked
