"""Fast Agent：所有消息和系统事件使用同一个协议，不含固定聊天话术。"""

import json
from pathlib import Path

from .llm import LLM
from .schemas import FastResponse


class FastAgent:
    def __init__(self, llm: LLM):
        self.llm = llm
        self.prompt = (Path(__file__).parent / "prompts/fast.md").read_text(encoding="utf-8")

    def respond(self, context: dict) -> FastResponse:
        # 整个输入作为数据；event 明确区分用户消息和程序事件。
        data = self.llm.generate(stage="fast", messages=[
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ])
        return FastResponse.parse(data, allow_silent=context["event"] != "user_message")
