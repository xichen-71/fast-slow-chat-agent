"""会话内消息历史。无跨进程持久化、无长期画像或检索。"""

class Memory:
    def __init__(self):
        self.messages = []

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def recent(self, turns: int, max_chars: int) -> list[dict[str, str]]:
        selected = []
        size = 0
        for message in reversed(self.messages[-turns * 2:]):
            if size + len(message["content"]) > max_chars:
                break
            selected.append(dict(message))
            size += len(message["content"])
        # 不以孤立的助手回复作为上下文开头。
        selected.reverse()
        while selected and selected[0]["role"] != "user":
            selected.pop(0)
        return selected

    def clear(self) -> None:
        self.messages.clear()
