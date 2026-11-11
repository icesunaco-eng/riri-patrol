"""Configuration is data: .env is never sourced or executed."""
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from .errors import AccountMismatch
from .models import REPORT_PAGES


def _env_value(value):
    """Read literal dotenv values, with comments but no shell interpretation."""
    value = value.strip()
    if not value or value.startswith("#"):
        return ""
    if value[0] in "\"'":
        quote, escaped = value[0], False
        for index, char in enumerate(value[1:], 1):
            if char == quote and not escaped:
                tail = value[index + 1:].strip()
                if tail and not tail.startswith("#"):
                    break
                # Quotes group the value; even backslashes and $ expressions
                # remain literal data. In particular, nothing is expanded.
                return value[1:index]
            escaped = char == "\\" and not escaped
        raise ValueError(".env に読み取れない引用符があります（値は表示しません）")
    # A # within an unquoted word (for example a URL fragment) is data.
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def read_env(path: Path) -> dict[str, str]:
    result = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError(".env に読み取れない行があります（値は表示しません）")
        result[key.strip()] = _env_value(value)
    return result


def integer(env, key, default, minimum, maximum):
    try:
        value = int(env.get(key, default))
    except (ValueError, TypeError):
        return default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class Config:
    root: Path
    mode: str = "practice"
    stop_file: Path | None = None
    slack_token: str = ""
    expected_bot_id: str = ""
    expected_user_id: str = ""
    instruction_channel: str = "C0CFM2HRA5V"
    report_channel: str = "C0CFL6A8QJH"
    notion_token: str = ""
    notion_version: str = "2026-03-11"
    requests_page: str = "413f1dff94dc81d2beebdf1b69f5c439"
    log_page: str = "413f1dff94dc816097fbfe4d4d7706b8"
    report_pages: dict | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.6:35b-a3b"
    ollama_num_ctx: int = 32768
    llm_enabled: bool = False
    log_size_notice_bytes: int = 50 * 1024 * 1024
    log_flush_min: int = 180
    error_notify_min: int = 60
    max_posts: int = 4
    max_instructions: int = 1
    run_timeout: int = 240

    @property
    def secrets(self):
        return tuple(v for v in (self.slack_token, self.notion_token) if v)

    @property
    def pages(self):
        return dict(self.report_pages or REPORT_PAGES)

    @property
    def stop(self):
        return self.stop_file or self.root / "STOP"

    def validate_identity(self):
        """Validate inside Patrol's audited stop boundary, never while loading."""
        if not self.expected_bot_id.strip() or not self.expected_user_id.strip():
            raise AccountMismatch(".env に期待するSlackボットIDとユーザーIDの設定が必要です")

    @classmethod
    def load(cls, root, dry_run=False, environ: Mapping | None = None):
        root = Path(root).resolve()
        env = read_env(root / ".env")
        # Kept in the signature for callers/tests from v0.1, but deliberately
        # ignored. Neither shell variables nor caller overrides authorize live
        # mode, credentials, destinations, or any other protected setting.
        expected_bot_id = env.get("EXPECTED_SLACK_BOT_ID", "").strip()
        expected_user_id = env.get("EXPECTED_SLACK_USER_ID", "").strip()
        mode = "live" if env.get("RIRI_MODE") == "live" and not dry_run else "practice"
        stop = Path(env.get("STOP_FILE", str(root / "STOP"))).expanduser()
        if not stop.is_absolute():
            stop = root / stop
        pages = dict(REPORT_PAGES)
        for name, suffix in {"あいす": "AISU", "のあ": "NOA", "みに": "MINI", "かれん": "KAREN", "うさ": "USA"}.items():
            value = env.get("NOTION_REPORT_PAGE_" + suffix)
            if value:
                pages[name] = "https://app.notion.com/p/" + value
        return cls(root=root, mode=mode, stop_file=stop,
                   slack_token=env.get("SLACK_BOT_TOKEN", ""),
                   expected_bot_id=expected_bot_id,
                   expected_user_id=expected_user_id,
                   instruction_channel=env.get("SLACK_INSTRUCTION_CHANNEL", "C0CFM2HRA5V"),
                   report_channel=env.get("SLACK_REPORT_CHANNEL", "C0CFL6A8QJH"),
                   notion_token=env.get("NOTION_TOKEN", ""), notion_version=env.get("NOTION_VERSION", "2026-03-11"),
                   requests_page=env.get("NOTION_REQUESTS_PAGE_ID", "413f1dff94dc81d2beebdf1b69f5c439"),
                   log_page=env.get("NOTION_LOG_PAGE_ID", "413f1dff94dc816097fbfe4d4d7706b8"), report_pages=pages,
                   ollama_url=env.get("OLLAMA_URL", "http://127.0.0.1:11434"),
                   ollama_model=env.get("OLLAMA_MODEL", "qwen3.6:35b-a3b"),
                   ollama_num_ctx=integer(env, "OLLAMA_NUM_CTX", 32768, 1024, 32768),
                   llm_enabled=env.get("LLM_ENABLED", "false").lower() == "true",
                   log_size_notice_bytes=integer(env, "LOG_SIZE_NOTICE_BYTES", 50 * 1024 * 1024, 1, 1024 ** 4),
                   log_flush_min=integer(env, "LOG_FLUSH_EVERY_MIN", 180, 180, 10080),
                   error_notify_min=integer(env, "ERROR_NOTIFY_EVERY_MIN", 60, 60, 10080),
                   max_posts=integer(env, "MAX_POSTS_PER_RUN", 4, 1, 4),
                   max_instructions=integer(env, "MAX_INSTRUCTION_POSTS_PER_RUN", 1, 1, 1),
                   run_timeout=integer(env, "RUN_TIMEOUT_SEC", 240, 1, 240))

