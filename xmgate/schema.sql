-- Applied at every start; every statement must be safe to run again.

CREATE TABLE IF NOT EXISTS users (
  tg_user_id      bigint PRIMARY KEY,
  verified_at     timestamptz,                 -- NULL = unknown
  verified_method text CHECK (verified_method IN ('captcha', 'manual')),
  verified_chat   bigint,                      -- where they solved it or were approved
  first_seen_at   timestamptz NOT NULL DEFAULT now(),
  last_username   text
);

-- "Delete and ignore me": HMAC-SHA256(OPTOUT_PEPPER, user id). The pepper is not in the database.
CREATE TABLE IF NOT EXISTS opt_outs (
  marker      bytea PRIMARY KEY,
  created_at  date NOT NULL DEFAULT current_date
);

CREATE TABLE IF NOT EXISTS join_requests (
  id              bigserial PRIMARY KEY,
  chat_id         bigint NOT NULL,
  chat_title      text,
  chat_username   text,
  tg_user_id      bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  user_chat_id    bigint NOT NULL,
  path            text NOT NULL CHECK (path IN ('query', 'dm')),
  query_id        text,
  outcome         text NOT NULL DEFAULT 'pending'
                  CHECK (outcome IN ('pending', 'captcha', 'approved', 'queued', 'declined_timeout', 'admin_approved')),
  requested_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz,
  dm_message_id   bigint,
  last_heartbeat  timestamptz,
  grace_until     timestamptz
);
CREATE INDEX IF NOT EXISTS join_requests_user ON join_requests (tg_user_id) WHERE outcome IN ('pending', 'captcha', 'queued');
CREATE INDEX IF NOT EXISTS join_requests_open ON join_requests (grace_until) WHERE outcome IN ('pending', 'captcha');

CREATE TABLE IF NOT EXISTS attempts (
  id              text PRIMARY KEY,
  tg_user_id      bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  join_request_id bigint REFERENCES join_requests ON DELETE CASCADE,  -- NULL when started from the DM menu
  tiles           jsonb NOT NULL,                                     -- [{id, key, answer}]
  issued_at       timestamptz NOT NULL DEFAULT now(),
  answered_at     timestamptz,
  correct         boolean,
  solve_ms        integer,
  had_touch       boolean
);
CREATE INDEX IF NOT EXISTS attempts_user ON attempts (tg_user_id, answered_at);
