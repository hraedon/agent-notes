-- Schema migration tracking (operational hardening).
-- Records which migration files have been applied and when.
-- The migrate script checks this table before applying each file.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id          SERIAL PRIMARY KEY,
    filename    TEXT UNIQUE NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
