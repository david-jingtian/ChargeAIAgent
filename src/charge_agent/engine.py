import json
import random
from collections.abc import Callable

from charge_agent.config import Settings
from charge_agent.models import StepRecord
from charge_agent.storage import Store
from charge_agent.tool_client import ToolClient, ToolFailure

Hook = Callable[[str, StepRecord], None]


def no_hook(point: str, step: StepRecord) -> None:
    pass


class Engine:
    def __init__(
        self, store: Store, tool: ToolClient, config: Settings, hook: Hook = no_hook
    ) -> None:
        self.store = store
        self.tool = tool
        self.config = config
        self.hook = hook

    def tick(self) -> bool:
        previous = self.store.next_step()
        if previous is None:
            return False
        step = self.store.begin(previous)
        self.hook("before_tool", step)
        try:
            result = self.tool.execute(step)
        except ToolFailure as exc:
            failures = step.failures + 1
            delay = None
            if exc.retryable and failures < self.config.max_failures:
                ceiling = min(
                    self.config.backoff_cap_seconds,
                    self.config.backoff_base_seconds * 2 ** min(step.failures, 30),
                )
                delay = max(random.uniform(0, ceiling), exc.retry_after)
            # An earlier interrupted attempt stays uncertain even if this request gets a 429.
            unknown = previous.outcome_unknown or exc.ambiguous
            self.store.record_failure(step, str(exc), delay, unknown)
            print(
                json.dumps(
                    {
                        "event": "step_error",
                        "key": step.idempotency_key,
                        "delay": delay,
                        "unknown": unknown,
                        "error": str(exc),
                    }
                ),
                flush=True,
            )
            return True
        self.hook("after_tool", step)
        self.store.complete(step, result, lambda: self.hook("during_commit", step))
        self.hook("after_commit", step)
        print(
            json.dumps(
                {
                    "event": "step_completed",
                    "key": step.idempotency_key,
                    "effect_id": str(result.effect_id),
                }
            ),
            flush=True,
        )
        return True
