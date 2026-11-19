"""模块之间的明确约定；拒绝格式错误，避免把字符串 false 当成真值。"""

from dataclasses import dataclass
from typing import Any


class AgentError(Exception):
    """可向用户显示的运行错误，不包含凭据或原始服务响应。"""


def text(data: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise AgentError(f"模型输出字段 {key} 应为{'非空' if required else ''}字符串")
    return value.strip()


@dataclass(frozen=True)
class Action:
    type: str
    task: str | None = None
    target_task_id: str | None = None

    @classmethod
    def parse(cls, data: Any) -> "Action":
        allowed = {"none", "start_slow", "update_slow", "cancel_slow", "get_status",
                   "get_result", "defer_result", "discard_result"}
        if not isinstance(data, dict) or not isinstance(data.get("type"), str) or data["type"] not in allowed:
            raise AgentError("action.type 不在白名单中")
        kind, task, target = data["type"], data.get("task"), data.get("target_task_id")
        if kind in {"start_slow", "update_slow"}:
            if not isinstance(task, str) or not task.strip():
                raise AgentError("启动或修改必须提供完整 task")
        elif task is not None:
            raise AgentError("此动作的 task 必须为 null")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise AgentError("target_task_id 必须为非空字符串或 null")
        if kind in {"none", "start_slow"} and target is not None:
            raise AgentError("none/start_slow 不允许 target_task_id")
        if kind not in {"none", "start_slow", "get_status"} and target is None:
            raise AgentError("当前动作必须指定 target_task_id")
        return cls(kind, task.strip() if task else None, target)


@dataclass(frozen=True)
class FastResponse:
    respond: str | None
    action: Action
    route_reason: str = ""
    topic_relation: str = "unknown"

    @classmethod
    def parse(cls, data: dict[str, Any], *, allow_silent=False) -> "FastResponse":
        action = Action.parse(data.get("action"))
        respond = data.get("respond")
        if respond is None:
            if not allow_silent or action.type != "none":
                raise AgentError("仅系统事件的 none 动作允许 respond=null")
        elif not isinstance(respond, str) or not respond.strip():
            raise AgentError("respond 必须为非空字符串")
        relation = data.get("topic_relation", "unknown")
        reason = data.get("route_reason", "")
        if relation not in {"same_task", "other_topic", "unknown"} or not isinstance(reason, str):
            raise AgentError("无效的 topic_relation 或 route_reason")
        return cls(respond.strip() if respond else None, action, reason, relation)


@dataclass(frozen=True)
class ActionExecutionResult:
    accepted: bool
    action_type: str
    task_id: str | None
    status: str | None
    reason_code: str


@dataclass(frozen=True)
class Analysis:
    conclusion: str
    reasons: list[str]
    assumptions: list[str]
    uncertainties: list[str]
    follow_up_question: str

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Analysis":
        lists = {}
        for key in ("reasons", "assumptions", "uncertainties"):
            value = data.get(key)
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise AgentError(f"模型输出 {key} 必须为非空字符串组成的列表，可为空列表")
            lists[key] = [x.strip() for x in value]
        return cls(text(data, "conclusion", required=True), **lists,
                   follow_up_question=text(data, "follow_up_question"))


@dataclass(frozen=True)
class ChatReply:
    answer: str
    channel: str
    route_reason: str
    elapsed_seconds: float
