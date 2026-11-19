"""V2：Fast 说话/建议动作；主线程校验执行；单 worker 只产出 Analysis。"""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Callable
from uuid import uuid4

from .config import Config
from .fast import FastAgent
from .llm import LLM
from .memory import Memory
from .schemas import Action, ActionExecutionResult, AgentError, Analysis, ChatReply
from .slow import SlowAgent

COMMANDS = {"/status": "get_status", "/result": "get_result",
            "/cancel": "cancel_slow", "/discard": "discard_result"}


@dataclass
class TaskState:
    task_id: str
    user_request: str
    context: list[dict[str, str]]
    analysis_task: str
    status: str = "running"
    actions: list[dict] = field(default_factory=list)
    result: Analysis | None = None
    error: str | None = None
    topic_switched: bool = False
    notified: bool = False
    delivered: bool = False
    started: float = field(default_factory=perf_counter)

    def brief(self):
        return {"task_id": self.task_id, "user_request": self.user_request,
                "analysis_task": self.analysis_task, "status": self.status,
                "result_available": self.result is not None, "topic_switched": self.topic_switched,
                "notified": self.notified, "delivered": self.delivered, "error": self.error}


class Workflow:
    def __init__(self, llm: LLM, memory: Memory, config: Config,
                 trace: Callable[[str, dict], None] | None = None):
        self.fast, self.slow = FastAgent(llm), SlowAgent(llm)
        self.memory, self.config = memory, config
        self.trace = trace or (lambda event, data: None)
        self.state: TaskState | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow")
        self.pending: tuple[str, Future] | None = None
        self.waiting: tuple[str, str, list, str] | None = None
        self.completion_event: tuple[str, str] | None = None
        self.closed = False

    def _submit(self, snapshot):
        task_id, message, history, task = snapshot
        future = self.executor.submit(self.slow.analyze, message, history, task)
        self.pending = (task_id, future)
        self.trace("slow_start", {"task_id": task_id, "task": task})

    def _create(self, message, history, task):
        self.completion_event = None
        self.state = TaskState(uuid4().hex[:10], message, [dict(m) for m in history], task)
        self.state.actions.append({"type": "reasoning", "status": "running", "summary": task})
        snapshot = (self.state.task_id, message, self.state.context, task)
        if self.pending:
            self.waiting = snapshot
            self.trace("slow_queued_latest", {"task_id": self.state.task_id})
        else:
            try:
                self._submit(snapshot)
            except Exception:
                self.state.status = "failed"
                self.state.error = "SLOW_SUBMIT_FAILED"
                self.state.actions[-1]["status"] = "failed"
                raise AgentError("SLOW_SUBMIT_FAILED") from None

    def sync(self):
        """先同步状态，不调用 Fast、不输出；用户路由前与动作执行前均可调用。"""
        if self.closed or not self.pending or not self.pending[1].done():
            return
        task_id, future = self.pending
        self.pending = None
        state = self.state
        if not state or state.task_id != task_id or state.status != "running":
            self.trace("slow_ignored", {"task_id": task_id})
            snapshot, self.waiting = self.waiting, None
            if snapshot and state and state.status == "running" and snapshot[0] == state.task_id:
                try:
                    self._submit(snapshot)
                except Exception:
                    state.status, state.error = "failed", "SLOW_SUBMIT_FAILED"
                    state.actions[-1]["status"] = "failed"
                    self.completion_event = (state.task_id, "slow_failed")
            return
        try:
            state.result = future.result()
            state.status = "completed"
            state.actions[-1]["status"] = "completed"
            event = "slow_completed"
        except Exception:
            state.status, state.error = "failed", "SLOW_ANALYSIS_FAILED_OR_TIMEOUT"
            state.actions[-1]["status"] = "failed"
            event = "slow_failed"
        self.completion_event = (task_id, event)
        self.trace(event, {"task_id": task_id, "seconds": perf_counter() - state.started,
                           "analysis": asdict(state.result) if state.result else None, "error": state.error})

    def _context(self, event, message, history, mode, feedback=None):
        state = self.state
        # 已完成的小型 Analysis 提供给 Fast，允许用户自然地要求查看，无需再走 compose 协议。
        return {"event": event, "message": message, "history": history,
                "task_state": state.brief() if state else None,
                "analysis_result": asdict(state.result) if state and state.result else None,
                "mode": mode, "explicit_intent": COMMANDS.get(message) if event == "user_message" else None,
                "execution_result": asdict(feedback) if feedback else None}

    def _feedback(self, action, accepted, reason):
        state = self.state
        return ActionExecutionResult(accepted, action.type, state.task_id if state else None,
                                     state.status if state else None, reason)

    def _execute(self, action: Action, context):
        state = self.state
        kind = action.type
        expected = context["explicit_intent"]
        if expected and kind != expected and not (state is None and kind == "none"):
            return self._feedback(action, False, "COMMAND_ACTION_MISMATCH")
        if context["event"] != "user_message" and kind not in {"none", "get_result", "defer_result", "get_status"}:
            return self._feedback(action, False, "EVENT_ACTION_FORBIDDEN")
        if kind == "none":
            if (context["event"] == "user_message" and context["mode"] == "slow"
                    and not expected and (not state or state.status in {"failed", "cancelled"} or state.delivered)):
                return self._feedback(action, False, "MODE_REQUIRES_SLOW")
            return self._feedback(action, True, "NO_ACTION")
        if kind not in {"start_slow", "get_status"} or action.target_task_id is not None:
            if not state or action.target_task_id != state.task_id:
                return self._feedback(action, False, "TARGET_MISMATCH")
        if kind == "get_status":
            if state and action.target_task_id != state.task_id:
                return self._feedback(action, False, "TARGET_MISMATCH")
        elif kind in {"start_slow", "update_slow"}:
            if context["mode"] == "fast":
                return self._feedback(action, False, "FAST_MODE_FORBIDS_SLOW")
            if kind == "start_slow" and state:
                if state.status == "running":
                    return self._feedback(action, False, "SLOW_BUSY")
                if state.status == "completed" and not state.delivered:
                    return self._feedback(action, False, "UNDELIVERED_RESULT")
            if kind == "update_slow" and state.status == "cancelled":
                return self._feedback(action, False, "TASK_CANCELLED")
            original = state.user_request if kind == "update_slow" else context["message"]
            if kind == "update_slow":
                state.status = "cancelled"
                state.actions[-1]["status"] = "superseded"
                self.trace("task_updated", {"old_task_id": state.task_id, "before": state.analysis_task,
                                            "after": action.task})
            self._create(original, context["history"], action.task)
        elif kind in {"cancel_slow", "discard_result"}:
            state.status, state.result = "cancelled", None
            state.actions[-1]["status"] = "cancelled"
            self.waiting = self.completion_event = None
            if self.pending:
                self.pending[1].cancel()
        elif kind == "get_result":
            if state.status != "completed" or not state.result:
                return self._feedback(action, False, "RESULT_NOT_READY")
            if context["analysis_result"] is None:
                return self._feedback(action, False, "RESULT_NOT_IN_FAST_INPUT")
        elif kind == "defer_result":
            if state.status not in {"running", "completed"}:
                return self._feedback(action, False, "NO_ACTIVE_RESULT")
            state.notified, state.topic_switched = True, True
        return self._feedback(action, True, "EXECUTED")

    def _turn(self, context):
        started = perf_counter()
        response = self.fast.respond(context)
        self.trace("fast_decision", {"event": context["event"], **asdict(response)})
        # Fast 调用期间 Slow 可能完成，先更新，再执行动作。
        self.sync()
        changed = context["task_state"] != (self.state.brief() if self.state else None)
        feedback = self._execute(response.action, context)
        self.trace("action_execution", asdict(feedback))
        channel = "slow" if feedback.accepted and response.action.type == "get_result" else "fast"
        if not feedback.accepted or changed:
            # 不重试动作，最多调用 Fast 修正一次自然语言回复。
            if changed and feedback.accepted:
                feedback = self._feedback(response.action, True, "STATE_CHANGED_ACTION_EXECUTED")
            correction = self._context("action_failed", context["message"], context["history"], context["mode"], feedback)
            response = self.fast.respond(correction)
            if response.action.type != "none" or response.respond is None:
                raise AgentError("FAST_CORRECTION_INVALID：动作未重试，请查询 /status")
            self.trace("fast_corrected", asdict(response))
        if response.respond is None:
            return None
        # 最终输出确定后才记反馈标志和 assistant 历史。
        if channel == "slow" and self.state:
            self.state.delivered = True
        if context["event"] in {"slow_completed", "slow_failed"} and self.state:
            self.state.notified = True
        if (context["event"] == "user_message" and response.action.type == "none"
                and self.state and self.state.status in {"running", "completed"} and not self.state.delivered
                and not context["explicit_intent"]):
            # 可选 relation；缺省保守当作切话题。只改一个受限字段。
            if response.topic_relation != "same_task":
                self.state.topic_switched = True
        self.memory.add("assistant", response.respond)
        return ChatReply(response.respond, channel, response.route_reason, perf_counter() - started)

    def handle(self, message: str, mode: str = "auto") -> ChatReply:
        if self.closed or mode not in {"auto", "fast", "slow"} or not message.strip():
            raise ValueError("会话关闭、消息为空或模式无效")
        self.sync()
        history = self.memory.recent(self.config.history_turns, self.config.history_max_chars)
        self.memory.add("user", message)
        return self._turn(self._context("user_message", message, history, mode))

    def poll(self) -> ChatReply | None:
        self.sync()
        if self.closed or not self.completion_event:
            return None
        task_id, event = self.completion_event
        self.completion_event = None
        if not self.state or self.state.task_id != task_id or self.state.notified or self.state.delivered:
            return None
        history = self.memory.recent(self.config.history_turns, self.config.history_max_chars)
        # 消费事件至多一次；失败时仍可通过 /status 或 /result 恢复。
        self.state.notified = True
        return self._turn(self._context(event, None, history, "auto"))

    def close(self):
        self.closed = True
        self.waiting = self.completion_event = None
        if self.state and self.state.status == "running":
            self.state.status = "cancelled"
        if self.pending:
            self.pending[1].cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
