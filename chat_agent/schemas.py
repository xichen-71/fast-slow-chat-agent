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
class FastResult:
    need_slow: bool
    answer: str
    analysis_task: str
    route_reason: str
    action: str
    target_task_id: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "FastResult":
        actions = {"direct", "start_slow", "status", "show_result", "cancel", "replace", "defer", "discard"}
        action = text(data, "action", required=True)
        if action not in actions:
            raise AgentError("未知的快通道 action")
        slow = action in {"start_slow", "replace"}
        task = text(data, "analysis_task", required=slow)
        if not slow and task:
            raise AgentError("非慢任务的 analysis_task 必须为空")
        target = data.get("target_task_id")
        task_actions = {"replace", "cancel", "discard", "status", "show_result", "defer"}
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise AgentError("target_task_id 必须为非空字符串或 null")
        if action == "replace" and target is None:
            raise AgentError("replace 必须提供 target_task_id")
        if action not in task_actions and target is not None:
            raise AgentError("新任务和直接回答的 target_task_id 必须为 null")
        return cls(slow, text(data, "answer", required=True),
                   task,
                   text(data, "route_reason", required=True), action, target)


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
