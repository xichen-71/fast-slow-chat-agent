"""慢通道：目前仅深入分析；后续可在这里接搜索。"""

import json
from pathlib import Path

from .llm import LLM
from .schemas import Analysis


class SlowChannel:
    def __init__(self, llm: LLM):
        self.llm = llm
        self.prompt = (Path(__file__).parent / "prompts/slow.md").read_text(encoding="utf-8")

    def analyze(self, message: str, history: list[dict[str, str]], task: str) -> Analysis:
        content = json.dumps({"user_message": message, "analysis_task": task}, ensure_ascii=False)
        data = self.llm.generate(stage="slow", messages=[
            {"role": "system", "content": self.prompt},
            *history,
            {"role": "user", "content": content},
        ])
        return Analysis.parse(data)
