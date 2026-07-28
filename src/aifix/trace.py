"""三层嵌套 trace：aifix.run → aifix.failure → aifix.attempt。

框架的 span（run / step / model_call / tool_call:*）会自动挂在这三层
下面 —— OpenTelemetry 的 span 是天然嵌套的，app 层只要在对的位置
开 span，不需要打通任何东西。

事实（facts）与事件（events）分开落盘：events.jsonl 是模型每一步看到
什么、决定做什么的原始素材（回放用）；facts.jsonl 是领域判断的结论
（verdict / rollback / flaky…），也是评测直接取用的数据源。
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Iterable

from harness.persistence.serialize import event_to_dict
from harness.telemetry.tracer import get_tracer
from opentelemetry import trace as otel_trace


class RunTrace:
    def __init__(self, out_dir: Path, run_id: str) -> None:
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._tracer = get_tracer("aifix")
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._facts = (self.dir / "facts.jsonl").open("a", encoding="utf-8")
        self._current: dict[str, Any] = {}
        self._closed = False

    # ---------- span ----------

    @contextlib.contextmanager
    def run_span(self):
        with self._tracer.start_as_current_span("aifix.run") as s:
            s.set_attribute("aifix.run_id", self.run_id)
            yield s

    @contextlib.contextmanager
    def failure_span(self, test_id: str):
        self._current["failure"] = test_id
        with self._tracer.start_as_current_span("aifix.failure") as s:
            s.set_attribute("aifix.test_id", test_id)
            try:
                yield s
            finally:
                self._current.pop("failure", None)

    @contextlib.contextmanager
    def attempt_span(self, attempt: int):
        self._current["attempt"] = attempt
        with self._tracer.start_as_current_span("aifix.attempt") as s:
            s.set_attribute("aifix.attempt", attempt)
            try:
                yield s
            finally:
                self._current.pop("attempt", None)

    # ---------- 落盘 ----------

    def fact(self, key: str, value: Any, **extra: Any) -> None:
        """记一条领域事实，并同时打到当前 span 的属性上。"""
        rec = {"run_id": self.run_id, "key": key, "value": value,
               **self._current, **extra}
        self._facts.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._facts.flush()
        # 同一条事实也打到当前 span 上：没配 provider 时是 no-op，零开销
        cur = otel_trace.get_current_span()
        cur.set_attribute(
            f"aifix.{key}",
            value if isinstance(value, (str, int, float, bool))
            else json.dumps(value, ensure_ascii=False))

    def record_events(self, events: Iterable[Any]) -> None:
        """把 AgentLoop 的事件流落成 jsonl，供 replay 使用。"""
        for ev in events:
            self._events.write(
                json.dumps(event_to_dict(ev), ensure_ascii=False) + "\n")
        self._events.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._events.close()
        self._facts.close()
        self._closed = True
