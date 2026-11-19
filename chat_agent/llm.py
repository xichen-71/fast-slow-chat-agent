"""统一模型接口：同步 HTTP 请求，适合当前单用户命令行实验。"""

import json
import socket
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config
from .schemas import AgentError


class LLM(Protocol):
    def generate(self, *, stage: str, messages: list[dict[str, str]]) -> dict[str, Any]: ...


def parse_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.splitlines()[1:-1])
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise AgentError("模型没有返回有效 JSON；请检查模型的结构化输出能力或增加输出预算") from exc
    if not isinstance(data, dict):
        raise AgentError("模型应返回 JSON 对象")
    return data


class ChatLLM:
    def __init__(self, config: Config):
        config.validate_live()
        self.config = config

    def generate(self, *, stage: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        slow = stage == "slow"
        payload: dict[str, Any] = {
            "model": self.config.slow_model if slow else self.config.fast_model,
            "messages": messages,
            self.config.token_field: self.config.slow_max_tokens if slow else self.config.fast_max_tokens,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(self.config.base_url + "/chat/completions",
                          data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise AgentError(f"模型接口返回 HTTP {status}；请检查地址、凭据、模型及参数兼容性") from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise AgentError("模型服务连接失败或请求超时，请检查网络和服务状态") from None
        except (ValueError, UnicodeError):
            raise AgentError("模型服务返回了无法解析的响应") from None
        try:
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                raise AgentError("模型输出被截断，请增加对应通道的 token 预算")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
        except (KeyError, IndexError, TypeError, AttributeError):
            raise AgentError("模型服务未返回有效文本；可能拒绝回答或接口不兼容") from None
        return parse_json_object(content)


class DemoLLM:
    """固定示例，只测试协议/并发；关键词规则不用于真实模式。"""

    def __init__(self, delay: float = 4):
        self.delay = delay

    def generate(self, *, stage: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        if stage == "slow":
            time.sleep(self.delay)
            return {"conclusion": "固定示例：先验证单后台任务，再扩展检索。",
                    "reasons": ["先验证对话与状态"], "assumptions": ["最小实验阶段"],
                    "uncertainties": ["固定演示，不是真实推理"], "follow_up_question": ""}
        context = json.loads(messages[-1]["content"])
        state = context["task_state"]
        event, message = context["event"], context["message"] or ""
        target = state["task_id"] if state else None
        kind, task, respond, relation = "none", None, "【固定演示】你好！这是简洁快答。", "other_topic"
        if event == "action_failed":
            feedback = context["execution_result"]
            respond = "【固定演示】执行反馈：" + feedback["reason_code"] + "；以最新任务状态为准。"
            if feedback["accepted"] and feedback["action_type"] == "get_result":
                respond = "【固定演示】" + context["analysis_result"]["conclusion"]
        elif event == "slow_failed":
            respond = "【固定演示】刚才的分析失败了，可以重试。"
        elif event == "slow_completed":
            if state["topic_switched"]:
                respond = "【固定演示】刚才的分析有结果了，现在要看吗？"
            else:
                kind = "get_result"
        else:
            explicit = context["explicit_intent"]
            if explicit:
                kind = explicit if state or explicit == "get_status" else "none"
            elif any(x in message for x in ("取消", "不用了")):
                kind = "cancel_slow" if state else "none"
            elif any(x in message for x in ("丢弃", "丢掉")):
                kind = "discard_result" if state else "none"
            elif message in {"要", "看看", "给我看", "说吧"} and state:
                kind = "get_result"
            elif any(x in message for x in ("查好", "想好", "进度")):
                kind = "get_status"
            elif any(x in message for x in ("稍后", "晚点")) and state:
                kind = "defer_result"
            elif any(x in message for x in ("改为", "再加")) and state:
                kind = "update_slow"
                task = state["analysis_task"] + "；最新要求：" + message
            elif context["mode"] == "slow" or any(x in message for x in ("比较", "分析", "方案")):
                if state and (state["status"] == "running" or (state["status"] == "completed" and not state["delivered"])):
                    respond = "【固定演示】上个任务仍占用中，先处理它，不接下新任务。"
                elif context["mode"] != "fast":
                    kind, task = "start_slow", message
                else:
                    respond = "【固定演示】当前是 FAST 模式，不启动慢任务。"
        if kind in {"start_slow", "update_slow"}:
            respond = "【固定演示】好，我按完整要求处理，你可以继续聊天。"
        elif kind == "get_result":
            if context["analysis_result"]:
                respond = "【固定演示】" + context["analysis_result"]["conclusion"]
            else:
                kind, respond = "get_status", "【固定演示】结果还没完成。"
        elif kind == "get_status":
            respond = "【固定演示】任务状态：" + (state["status"] if state else "无任务")
        elif kind in {"cancel_slow", "discard_result"}:
            respond = "【固定演示】已取消，晚到结果不再采用。"
        elif kind == "defer_result":
            respond = "【固定演示】结果先保留，想看时再说。"
        return {"respond": respond, "action": {"type": kind, "task": task,
                "target_task_id": target if kind not in {"none", "start_slow"} else None},
                "topic_relation": relation, "route_reason": "固定演示，不代表真实模型判断"}
