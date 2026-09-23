CREATE SCHEMA IF NOT EXISTS engine;
CREATE SCHEMA IF NOT EXISTS mock_tool;

CREATE TABLE IF NOT EXISTS engine.runs (
    id uuid PRIMARY KEY,
    workflow_version text NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'completed', 'failed', 'needs_review')),
    input jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS engine.steps (
    run_id uuid NOT NULL REFERENCES engine.runs(id),
    position integer NOT NULL CHECK (position >= 0),
    name text NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'retry_wait', 'completed', 'failed', 'needs_review')),
    idempotency_key text UNIQUE NOT NULL,
    input jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    failures integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
    next_attempt_at timestamptz,
    result jsonb,
    error text,
    outcome_unknown boolean NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, position),
    CHECK ((state = 'completed') = (result IS NOT NULL)),
    CHECK ((state = 'retry_wait') = (next_attempt_at IS NOT NULL))
);

-- This ledger IS the simulated side effect. Its row includes the deduplication key and result:
-- there is no second unprotected "charge" performed outside this transaction.
CREATE TABLE IF NOT EXISTS mock_tool.effects (
    idempotency_key text PRIMARY KEY,
    operation text NOT NULL,
    input jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS mock_tool.requests (
    idempotency_key text PRIMARY KEY,
    count integer NOT NULL CHECK (count > 0)
);
