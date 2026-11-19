"""读取环境变量；不依赖第三方包，已有环境变量优先于 .env。"""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().isidentifier():
            raise ValueError(f".env 第 {number} 行格式错误，应为 KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    base_url: str = ""
    api_key: str = ""
    fast_model: str = ""
    slow_model: str = ""
    token_field: str = "max_completion_tokens"
    json_mode: bool = False
    fast_max_tokens: int = 800
    slow_max_tokens: int = 2400
    timeout_seconds: float = 60
    history_turns: int = 8
    history_max_chars: int = 12000

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Config":
        load_env(env_file or PROJECT_ROOT / ".env")
        json_mode = os.getenv("LLM_JSON_MODE", "false").lower()
        if json_mode not in {"true", "false"}:
            raise ValueError("LLM_JSON_MODE 必须为 true 或 false")
        fast_model = os.getenv("FAST_MODEL", "").strip()
        config = cls(
            base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            fast_model=fast_model,
            slow_model=os.getenv("SLOW_MODEL", "").strip() or fast_model,
            token_field=os.getenv("LLM_TOKEN_FIELD", "max_completion_tokens"),
            json_mode=json_mode == "true",
            fast_max_tokens=int(os.getenv("FAST_MAX_TOKENS", "800")),
            slow_max_tokens=int(os.getenv("SLOW_MAX_TOKENS", "2400")),
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            history_turns=int(os.getenv("HISTORY_TURNS", "8")),
            history_max_chars=int(os.getenv("HISTORY_MAX_CHARS", "12000")),
        )
        if config.token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("LLM_TOKEN_FIELD 必须为 max_tokens 或 max_completion_tokens")
        if not math.isfinite(config.timeout_seconds) or min(
            config.fast_max_tokens, config.slow_max_tokens, config.timeout_seconds,
            config.history_turns, config.history_max_chars,
        ) <= 0:
            raise ValueError("预算、超时和历史长度必须是正数")
        return config

    def validate_live(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment or url.username:
            raise ValueError("请在 .env 配置有效的 LLM_BASE_URL，如 https://服务域名/v1")
        if not self.fast_model or not self.slow_model:
            raise ValueError("请在 .env 配置 FAST_MODEL；SLOW_MODEL 可以留空")
