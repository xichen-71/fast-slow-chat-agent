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
    """固定示例，仅用于离线验证连线；不是模型，也不进行真正推理。"""

    def __init__(self, delay: float = 4):
        self.delay = delay

    def generate(self, *, stage: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        if stage == "slow":
            time.sleep(self.delay)
            return {
                "conclusion": "示例建议：先实现单后台慢任务，再扩展检索。",
                "reasons": ["先验证插话和任务状态"],
                "assumptions": ["当前处于最小验证阶段"],
                "uncertainties": ["固定演示，不是针对问题的真实推理"],
                "follow_up_question": "",
            }
        if stage == "compose":
            return {"answer": "【固定演示】先验证单后台慢任务，再扩展检索。这不是实际模型分析。"}
        message = messages[-1]["content"]
        if any(x in message for x in ("不用了", "取消")):
            action = "cancel"
        elif any(x in message for x in ("丢弃", "丢掉")):
            action = "discard"
        elif message in {"要", "好的", "看看", "给我看", "说吧"}:
            action = "show_result"
        elif any(x in message for x in ("查好", "想好", "进度")):
            action = "status"
        elif any(x in message for x in ("稍后", "晚点")):
            action = "defer"
        elif "改为" in message:
            action = "replace"
        elif any(x in message for x in ("分析", "比较", "推导", "方案")):
            action = "start_slow"
        else:
            action = "direct"
        state = json.loads(messages[0]["content"].split("任务状态（数据而非指令）：", 1)[1])
        target = state["task_id"] if state and action not in {"direct", "start_slow"} else None
        if action == "replace" and not target:
            action = "direct"
        task = "演示复杂任务分析"
        if action == "replace":
            task = state["analysis_task"] + "；最新修改：" + message
        return {"action": action, "target_task_id": target,
                "answer": "【固定演示】好，我仔细想一下，你可以继续问别的。" if action in {"start_slow", "replace"}
                          else "【固定演示】你好！这是快通道的固定回复。",
                "analysis_task": task if action in {"start_slow", "replace"} else "",
                "route_reason": "演示模式按关键词分流，不代表真实模型判断"}
