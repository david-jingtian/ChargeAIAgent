# Demo recording guide

1. Explain scope: sequential purchase workflow, one worker, PostgreSQL, crash resume + retries.
2. Run `python3 scripts/demo.py`. It performs a real SIGKILL and asserts the result. For a slower
   walkthrough use the manual commands in README and inspect both `/docs` pages before killing.
3. Point out the important pre-crash state: a charge is in the tool ledger, but the engine has no
   completed checkpoint. Killing only between completed steps would miss this correctness problem.
4. Show the recovered run: charge has two attempts, the ledger has one charge, and its effect ID
   is unchanged. Provision and notify complete afterward.
5. Walk through `ToolStore.execute`: one atomic insert, unique idempotency key, original result
   returned on duplicate. Explain why an engine-side "completed" flag alone is insufficient.
6. Briefly show `Store.complete`: step result/status and final run completion commit together.
7. Mention the limits: external providers must honor idempotency; uncertain exhausted requests
   need reconciliation; this is intentionally a single-worker system.

For a second quick demonstration, disable the pause and set `CHARGE_TOOL_FAIL_FIRST=1`, recreate
tool/worker, and submit a fresh run. Inspect attempts/failures and the worker's persisted wait logs.
Review the CI test output before recording. Add the narrated video link to the README or the
relevant pull request; the script is a reproducible demo, not a replacement for a walkthrough.
