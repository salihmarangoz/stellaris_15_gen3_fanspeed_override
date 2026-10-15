"""Bound caller waits without canceling or overlapping a native operation."""

import threading
import time
from typing import Any, Callable


class OperationBusy(RuntimeError):
    pass


class OperationTimeout(TimeoutError):
    pass


class BoundedOperation:
    """One in-flight operation, no queue, and discarded late results.

    A deadline releases the caller, not the native driver. The slot stays owned
    until the operation and optional late safety action have actually returned.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._active: dict[str, Any] | None = None

    def call(
        self,
        operation: Callable[[], Any],
        timeout: float,
        *,
        on_timeout: Callable[[], None] | None = None,
        after_timeout: Callable[[], None] | None = None,
        wait_for_slot: bool = False,
    ) -> Any:
        if timeout <= 0:
            raise ValueError("Operation timeout must be positive")
        item: dict[str, Any] = {"done": threading.Event(), "abandoned": False}
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._active is not None:
                remaining = deadline - time.monotonic()
                if not wait_for_slot or self._active["abandoned"] or remaining <= 0:
                    raise OperationBusy(
                        f"{self.name} is still running. No second operation was started; "
                        "a timeout does not cancel the driver."
                    )
                self._changed.wait(remaining)
            if time.monotonic() >= deadline:
                raise OperationBusy(f"{self.name} deadline expired before the operation started")
            self._active = item

        def run() -> None:
            try:
                item["result"] = operation()
            except BaseException as exc:
                item["error"] = exc
            with self._lock:
                abandoned = item["abandoned"]
                if not abandoned:
                    self._active = None
                    self._changed.notify_all()
                    item["done"].set()
                    return
            try:
                if after_timeout is not None:
                    after_timeout()
            finally:
                with self._lock:
                    self._active = None
                    self._changed.notify_all()
                    item["done"].set()

        try:
            threading.Thread(target=run, name=self.name, daemon=True).start()
        except BaseException:
            with self._lock:
                self._active = None
                self._changed.notify_all()
            raise
        if not item["done"].wait(max(0.0, deadline - time.monotonic())):
            with self._lock:
                if not item["done"].is_set():
                    # Publish the fault before the worker can finish and repair it.
                    if on_timeout is not None:
                        on_timeout()
                    item["abandoned"] = True
                    self._changed.notify_all()
                    raise OperationTimeout(
                        f"{self.name} exceeded {timeout:g} seconds. The operation "
                        "may still be running; its result is unconfirmed."
                    )
        if "error" in item:
            raise item["error"]
        return item.get("result")
