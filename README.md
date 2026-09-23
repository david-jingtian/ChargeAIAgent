# ChargeAIAgent

A small durable execution engine for `charge → provision → notify`, built with Python 3.12,
FastAPI, Pydantic, and PostgreSQL 16. Includes a separate flaky HTTP mock tool.

**Chosen properties:** crash-safe resume and safe retries. A committed step is never called again.
An interrupted step may send another request with the **same idempotency key**. The mock tool
atomically deduplicates requests so each logical side effect happens once.

## Run

Requires Docker with Compose v2. From this directory:

```sh
docker compose up --build -d --wait
```

Open http://localhost:8000/docs and submit `POST /runs` with `{}` for the sample inputs,
or seed a run directly:

```sh
docker compose run --rm seed
```

The seed command prints a run UUID. Inspect it with `GET /runs/{id}`, and inspect the tool ledger
at `http://localhost:8001/effects?prefix=RUN_UUID`. Both APIs have interactive `/docs`.

```sh
curl -s http://localhost:8000/runs -H 'Content-Type: application/json' \
  -d '{"customer_id":"demo-customer","amount_cents":2500,"currency":"USD"}'
docker compose logs -f worker
```

Ports 5432, 8000, and 8001 must be free. PostgreSQL uses a named volume. `docker compose down`
preserves data; **`docker compose down -v` deletes it**. Credentials are local-demo defaults;
the APIs are unauthenticated and bound to localhost. Do not deploy this Compose file publicly.
Repeated `POST /runs` creates distinct purchases: API-submission deduplication is out of scope.

## Live crash demo

```sh
python3 scripts/demo.py
```

This starts the stack with random failures disabled and a deliberate barrier after the charge
response, before the engine checkpoint. It shows a committed charge while the engine step is
still running, sends real SIGKILL to the worker, restarts it, and asserts:

- The workflow completes in order.
- Charge has two attempts and exactly one durable effect with the original result ID.
- Provision and notify each have one effect.

No data is erased by the script. Run it with no other unfinished workflows to keep the demonstration
focused. The pause triggers only on the first attempt, so restart can recover with the same config.
Restore normal settings afterward: `docker compose up -d --force-recreate tool worker`.

For a manual video: start with
`CHARGE_PAUSE_AT=after_tool CHARGE_TOOL_FAILURE_RATE=0 docker compose up --build -d --wait`,
submit a run, inspect the ledger and worker logs, then execute
`docker compose kill -s SIGKILL worker` followed by `docker compose start worker`.
See [DEMO.md](DEMO.md) for a short recording outline. A narrated submission video still needs to
be recorded by the candidate.

### Manual non-charge crash recovery

To verify recovery on a later workflow step, pause after `provision` returns but before its
completion is checkpointed:

```sh
docker compose down -v
docker compose up --build -d --wait db tool api

CHARGE_PAUSE_AT=after_tool \
CHARGE_PAUSE_STEP=provision \
CHARGE_TOOL_FAILURE_RATE=0 \
docker compose up --build -d worker
```

Submit a run and copy the returned run ID:

```sh
curl -s -X POST http://localhost:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"manual-crash-test","amount_cents":2500,"currency":"USD"}'
```

Confirm the worker paused on `provision`:

```sh
docker compose logs --no-color worker
curl -s http://localhost:8000/runs/<RUN_ID>
curl -s "http://localhost:8001/effects?prefix=<RUN_ID>"
```

Expected pre-crash state:

- `charge` is `completed`
- `provision` is `running`
- `notify` is `pending`
- the tool ledger already contains one charge effect and one provision effect

Kill the worker with SIGKILL, then restart it without the pause:

```sh
docker compose kill -s SIGKILL worker

CHARGE_PAUSE_AT= \
CHARGE_TOOL_FAILURE_RATE=0 \
docker compose up -d --force-recreate worker
```

Inspect the same run again:

```sh
curl -s http://localhost:8000/runs/<RUN_ID>
curl -s "http://localhost:8001/effects?prefix=<RUN_ID>"
```

Expected final state:

- the run and all three steps are `completed`
- attempts are `charge=1`, `provision=2`, `notify=1`
- the ledger contains exactly one charge, one provision, and one notify effect

This demonstrates the key guarantee: `provision` may be attempted twice after a crash, but the
durable external effect is created only once because the same idempotency key is reused.

## Tests and development

The suite uses **real PostgreSQL transactions and actual worker subprocesses**, not an in-memory
storage substitute. Tests use a separate database named `charge_test`, initialized by Compose.

```sh
docker compose run --build --rm test
```

Or use a local Python environment (including PyCharm):

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
docker compose up -d --wait db
pytest -q
ruff check src tests scripts
mypy src tests scripts
```

Run each service in its own terminal using `uvicorn charge_agent.api:app --port 8000`,
`uvicorn charge_agent.mock_api:app --port 8001`, and `python -m charge_agent.worker`.
Integration tests truncate only their dedicated `_test` database. Never point them at real data.
The CI workflow also builds Docker images and executes the complete crash demo.

## Execution model and correctness

**Sequential steps, one worker process.** A PostgreSQL session advisory lock rejects a second
worker. All worker storage operations use that same connection. A connection failure is fatal;
the worker never silently reconnects and continues without reacquiring ownership. This is not a
multi-worker lease/fencing design. An old HTTP request may outlive a dead worker; tool-side
idempotency protects against this overlap.

The worker selects the earliest unfinished step of an eligible run. It commits `running` and
increments the request attempt count **before** calling the tool. It commits the result and
`completed` together **after** a validated response. Final-step completion and run completion
share a transaction. Run creation and all step rows also share a transaction.

```mermaid
stateDiagram-v2
    pending --> running: start attempt
    running --> completed: commit result
    running --> retry_wait: transient error
    retry_wait --> running: persisted deadline reached
    running --> running: restart with same key
    running --> failed: known failure / exhausted retries
    running --> needs_review: unresolved outcome / stop retrying
```

A running step after restart is an **unknown outcome**, never evidence that nothing happened.
Each key is derived from run UUID, persisted workflow version, position, and name, and stored with
immutable input at run creation. It never includes an attempt number. Completed results are read
from storage; earlier steps are not replayed. Changes to workflow definitions require a new version;
this submission only executes `purchase-v1` and does not implement upgrades of existing runs.

**External boundary:** the mock tool's ledger row is the simulated charge/provision/notification.
Its key, input, and returned result are inserted atomically under a unique constraint. Overlapping
calls converge on the original result. Reusing a key with another input or operation is rejected.
The engine cannot access this ledger to bypass the HTTP protocol. Separate schemas share one
PostgreSQL instance for setup convenience; no transaction spans engine and tool completion.

The exactly-once guarantee is for **effects**, conditional on the tool's durable idempotency
contract and retaining its keys. HTTP requests are at-least-once while retry/recovery continues.
Finite retries do not guarantee eventual completion. An arbitrary non-idempotent external API
cannot be made exactly-once by this engine; production integrations need provider idempotency
or reconciliation. Database loss, backups, replication/failover, and key expiration are outside
this process-crash failure model.

## Retry decisions

- 429, 5xx, 408, transport failures, and invalid successful responses are retryable.
- Other HTTP errors stop the run. A later step never starts after a failure.
- Full-jitter exponential backoff has an 8-second ceiling. Valid `Retry-After` seconds or dates
  are a lower bound and may exceed that ceiling. The computed deadline and failure count persist.
- Default budget: five recorded failures per step. A process crash increments attempts but not
  the error budget; a `running` step is always allowed to resolve its unknown outcome on restart.
- A timeout, interrupted attempt, invalid response, or 5xx may have produced an effect. That
  uncertainty is sticky until success: a subsequent 429 does not erase it. Exhaustion yields
  `needs_review`, not a false claim of no charge. No automatic refunds or operator retry endpoint.

The mock's default failure rate is 25%, with both 429 and 503 responses before performing the
effect. Failures are pseudorandom using a seed, key, and persisted request count. Set
`CHARGE_TOOL_FAIL_FIRST=1` to force one 429 per step. Completed keys return their saved result
before failure injection. `CHARGE_TOOL_RESPONSE_DELAY_SECONDS` delays responses after commit,
creating a genuine ambiguous-outcome window.

## Failure modes

| Death point | Durable state | Recovery and reason |
|---|---|---|
| After `running`, before sending the request | Step running, no tool effect | Retry same key; first effect is created. |
| Tool committed, worker has not received response | Tool effect exists; step running | Retry same key returns original result. No second effect. |
| Worker received success, before checkpoint | Tool effect exists; step running | Same deduplicated request, then checkpoint. |
| Between checkpoint writes inside the transaction | Uncommitted completion changes | PostgreSQL rolls back; repeat safely. If COMMIT itself succeeded but its acknowledgement was lost, read the committed state and skip. |
| After checkpoint commit, before the next step | Completed result is durable | Skip that step and proceed. Final-step/run completion are atomic. |

The suite kills workers at each controlled boundary, tests concurrent duplicate tool requests,
key conflicts, restart during retry waits, permanent failures, and uncertainty at retry exhaustion.
The `during_commit` barrier is **inside the open transaction before COMMIT**, not a simulated
disk tear or a precise kill inside PostgreSQL's COMMIT implementation. PostgreSQL provides the
atomicity of the actual commit; the test demonstrates rollback of uncommitted application writes.

## Scope and tradeoffs

Built: crash-safe resume, safe retries, durable mock idempotency, basic inspection/logging,
typed API/models, reproducible failure controls, one-command startup, and failure tests.

Cut: concurrent workflow workers, leases/fencing, fan-out, cancellation, adaptive tool-wide
throttling, dashboard, workflow upgrades, auth, deployment, and compensation. The worker checks
SIGTERM between steps and can finish an in-flight request, but full bounded drain is not a claimed
feature. These cuts keep the one-day project focused on the external-effect/checkpoint boundary.

Two choices I would revisit in production:

1. **Unknown outcomes after the retry budget.** Stopping for review is safe but operationally
   incomplete. A provider lookup/reconciliation API and operator tooling would resolve these runs.
2. **Single worker and blocking I/O.** This keeps ownership and flow understandable. The worker uses
   synchronous HTTP and SQL consistently; FastAPI synchronous handlers run in its thread pool.
   Throughput, fair scheduling, pooling, and multi-worker fencing need a separate design.

`engine.py` controls execution; `storage.py` owns engine transactions; `tool_client.py` owns HTTP
classification; `tool_storage.py` owns mock deduplication; API modules validate and route requests.
The mock deliberately receives the same purchase input for each step; this is an ordered execution
example, not a general-purpose result-binding language or a real payment integration.

## AI usage

AI helped scope the project, draft the implementation, generate failure tests, and review the
recovery model. Human review should focus on every transaction boundary and the provider contract.
This repository is AI-assisted; the candidate should understand it before submitting.

An early plan treated retry exhaustion as ordinary failure. Reviewing the case where a charge
commits but the response disappears showed that this was wrong: the outcome can still be unknown.
The implementation now retains uncertainty and uses `needs_review`; tests also verify that a
later 429 cannot accidentally clear an earlier unknown outcome. Another review caught startup
ordering: the worker must wait for the tool's health check, not merely its container creation.
Use actual CI results as evidence; generated tests alone are not proof they pass.
