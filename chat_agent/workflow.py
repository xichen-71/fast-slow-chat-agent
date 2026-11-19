"""状态只由主循环修改；后台线程只计算，完成后由 poll 验货。"""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Callable
from uuid import uuid4

from .config import Config
from .fast import FastChannel
from .llm import LLM
from .memory import Memory
from .schemas import Analysis, ChatReply
from .slow import SlowChannel


@dataclass
class TaskState:
    task_id: str
    user_request: str
    context: list[dict[str, str]]
    analysis_task: str
    revision: int = 1
    status: str = "running"
    actions: list[dict] = field(default_factory=list)
    result: Analysis | None = None
    error: str | None = None
    topic_switched: bool = False
    notified: bool = False
    delivered: bool = False
    started: float = field(default_factory=perf_counter)

    def brief(self) -> dict:
        return {"task_id": self.task_id, "user_request": self.user_request,
                "analysis_task": self.analysis_task, "revision": self.revision,
                "status": self.status, "notified": self.notified, "delivered": self.delivered}


class Workflow:
    def __init__(self, llm: LLM, memory: Memory, config: Config,
                 trace: Callable[[str, dict], None] | None = None):
        self.fast = FastChannel(llm)
        self.slow = SlowChannel(llm)
        self.memory = memory
        self.config = config
        self.trace = trace or (lambda event, data: None)
        self.state: TaskState | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow")
        self.pending: tuple[str, int, Future] | None = None
        # 未提交给线程池的最新快照；连续修改只覆盖这个位置。
        self.waiting: tuple[str, int, str, list[dict[str, str]], str] | None = None
        self.closed = False

    def _reply(self, answer: str, channel="fast", reason="任务管理", started=None) -> ChatReply:
        self.memory.add("assistant", answer)
        return ChatReply(answer, channel, reason, perf_counter() - started if started else 0)

    def _schedule(self):
        state = self.state
        snapshot = (state.task_id, state.revision, state.user_request,
                    [dict(m) for m in state.context], state.analysis_task)
        if self.pending is not None:
            self.waiting = snapshot
            self.trace("slow_queued_latest", {"task_id": state.task_id, "revision": state.revision})
        else:
            self._submit(snapshot)

    def _submit(self, snapshot):
        task_id, revision, message, history, task = snapshot
        future = self.executor.submit(self.slow.analyze, message, history, task)
        self.pending = (task_id, revision, future)
        self.trace("slow_start", {"task_id": task_id, "revision": revision, "task": task})

    def _start(self, message, history, task, acknowledgement, started):
        self.state = TaskState(uuid4().hex[:10], message, history, task)
        self.state.actions.append({"type": "reasoning", "status": "running",
                                   "revision": 1, "summary": task})
        self._schedule()
        return self._reply(acknowledgement or "好，我仔细想一下，你可以继续问别的。", started=started)

    def _replace(self, history, task, acknowledgement, started):
        state = self.state
        old_task = state.analysis_task
        state.actions[-1]["status"] = "superseded"
        state.revision += 1
        state.analysis_task = task
        state.context = history
        state.status = "running"
        state.result = None
        state.error = None
        state.notified = state.delivered = state.topic_switched = False
        state.started = perf_counter()
        state.actions.append({"type": "reasoning", "status": "running",
                              "revision": state.revision, "summary": task})
        self.trace("task_revised", {"task_id": state.task_id, "revision": state.revision,
                                    "before": old_task, "after": task})
        self._schedule()
        return self._reply(acknowledgement, started=started)

    def _deliver(self, started=None):
        state = self.state
        if not state or state.status != "completed" or state.result is None:
            return self._reply(self.fast.notice("status", state.brief() if state else None), started=started)
        self.trace("compose_start", {"task_id": state.task_id})
        # 原任务问题和快照仍保留，防止用新话题替换原问题。
        answer = self.fast.compose(state.analysis_task, state.context, state.result)
        state.delivered = True
        return self._reply(answer, "slow", "慢分析后由快通道整理", started)

    def handle(self, message: str, mode: str = "auto") -> ChatReply:
        if self.closed:
            raise ValueError("会话已关闭")
        if mode not in {"auto", "fast", "slow"} or not message.strip():
            raise ValueError("消息不能为空，模式必须为 auto/fast/slow")
        started = perf_counter()
        history = self.memory.recent(self.config.history_turns, self.config.history_max_chars)
        self.memory.add("user", message)
        brief = self.state.brief() if self.state else None
        commands = {"/status": "status", "/result": "show_result", "/cancel": "cancel", "/discard": "discard"}
        target_task_id = brief["task_id"] if message in commands and brief else None
        acknowledgement = ""
        task = "根据用户要求和上下文深入分析"
        reason = "实验者强制慢分析"
        if message in commands:
            action = commands[message]
            reason = "用户明确任务管理命令"
        elif mode == "slow":
            action = "start_slow"
        else:
            self.trace("fast_start", {})
            phase = perf_counter()
            result = self.fast.respond(message, history, brief)
            self.trace("fast_done", {"seconds": perf_counter() - phase})
            action, reason = result.action, result.route_reason
            task = result.analysis_task
            acknowledgement = result.answer
            target_task_id = result.target_task_id
        self.trace("route", {"action": action, "reason": reason})
        state = self.state
        if action in {"replace", "cancel", "discard", "status", "show_result", "defer"} and message not in commands:
            if state is None or target_task_id != state.task_id:
                return self._reply("你指的是当前哪个任务？我先不修改或取消，请确认一下。", started=started)
        if action in {"cancel", "discard"}:
            if state:
                state.status = "cancelled"
                state.result = None
                self.waiting = None
                state.actions[-1]["status"] = "cancelled"
                if self.pending:
                    self.pending[2].cancel()
            return self._reply(self.fast.notice(action, brief), started=started)
        if action == "status":
            return self._reply(self.fast.notice("status", brief), started=started)
        if action == "show_result":
            return self._deliver(started)
        if action == "defer":
            if state:
                state.notified = True
                state.topic_switched = True
            return self._reply("好，结果先留着，你想看时再告诉我。" if state else "目前没有需要暂存的任务。", started=started)
        if action in {"start_slow", "replace"}:
            if mode == "fast":
                return self._reply("当前是强制 FAST 模式，未启动慢任务。可切换 /mode auto 后重试。", started=started)
            if action == "replace":
                if state is None or state.status == "cancelled":
                    return self._reply("原任务已不可修改，请重新提出完整问题。", started=started)
                return self._replace(history, task, acknowledgement, started)
            if state and state.status == "running":
                return self._reply(self.fast.notice("busy", brief), started=started)
            elif state and state.status == "completed" and not state.delivered:
                return self._reply("上一个问题已经有结果，还没给你看。要先看结果，还是丢弃后再处理这个问题？", started=started)
            return self._start(message, history, task, acknowledgement, started)
        if state and state.status in {"running", "completed"} and not state.delivered:
            state.topic_switched = True
        return self._reply(acknowledgement, reason=reason, started=started)

    def poll(self) -> ChatReply | None:
        """主循环周期调用；只处理一次完成事件，不在后台线程发送消息。"""
        if not self.pending or not self.pending[2].done():
            return None
        task_id, revision, future = self.pending
        self.pending = None
        state = self.state
        if not state or state.task_id != task_id or state.revision != revision or state.status != "running":
            self.trace("slow_ignored", {"task_id": task_id, "revision": revision})
            snapshot, self.waiting = self.waiting, None
            if snapshot and state and state.status == "running" and snapshot[:2] == (state.task_id, state.revision):
                self._submit(snapshot)
            return None
        try:
            state.result = future.result()
        except Exception:
            state.status = "failed"
            state.error = "慢分析失败或超时；未生成可用结果"
            state.actions[-1]["status"] = "failed"
            self.trace("slow_failed", {"task_id": task_id, "error": state.error})
            return self._reply("刚才的分析没能完成，你可以重试。")
        state.status = "completed"
        state.actions[-1]["status"] = "completed"
        self.trace("slow_done", {"task_id": task_id, "revision": revision, "seconds": perf_counter() - state.started,
                                 "analysis": asdict(state.result)})
        if state.topic_switched:
            if not state.notified:
                state.notified = True
                return self._reply("刚才那个问题我想好了，现在要听吗？")
            return None
        return self._deliver()

    def close(self):
        self.closed = True
        self.waiting = None
        if self.state and self.state.status == "running":
            self.state.status = "cancelled"
        if self.pending:
            self.pending[2].cancel()
        # 已发出的 HTTP 无法硬停止；由请求超时收尾，结果不会再输出。
        self.executor.shutdown(wait=False, cancel_futures=True)
