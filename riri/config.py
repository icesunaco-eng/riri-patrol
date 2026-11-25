"""Configuration is data: .env is never sourced or executed."""
from dataclasses import dataclass, field
from pathlib import Path
import json
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


# A MINI_* line that cannot be read becomes this value, so only the みに lane stops
# (re-review 💚20261125-8 S6); any other unreadable line still stops the patrol.
UNREADABLE_MINI = "\x00unreadable"


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
        mini = re.match(r"(MINI_[A-Z0-9_]+)\s*(?:=|$|\s)", line)
        key, sep, value = line.partition("=")
        try:
            if not sep or not key.strip().replace("_", "").isalnum():
                raise ValueError(".env に読み取れない行があります（値は表示しません）")
            result[key.strip()] = _env_value(value)
        except ValueError:
            if not mini:
                raise
            result[mini.group(1)] = UNREADABLE_MINI
    return result


def integer(env, key, default, minimum, maximum):
    try:
        value = int(env.get(key, default))
    except (ValueError, TypeError):
        return default
    return min(maximum, max(minimum, value))


def requester_mentions(value):
    """Optional literal JSON; never evaluate config or expose its contents."""
    if not value:
        return {}
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError()
            result[key] = item
        return result
    try:
        mapping = json.loads(value, object_pairs_hook=unique_object)
        if not isinstance(mapping, dict):
            raise ValueError()
        from .models import RECIPIENTS
        if any(name not in RECIPIENTS or not isinstance(user_id, str)
                or not re.fullmatch(r'U[A-Z0-9]+', user_id) for name, user_id in mapping.items()):
            raise ValueError()
        return mapping
    except (ValueError, TypeError):
        raise AccountMismatch('SLACK_REQUESTER_MENTION_IDS の設定が不正です（値は表示しません）') from None


def agent_tools(value):
    """Toolboxes the まむ/みき runners may use; unknown names are ignored, read is always on."""
    from .agent_lane import TOOLS
    names = [v.strip() for v in value.split(",") if v.strip()]
    return tuple(t for t in TOOLS if t == "read" or t in names)


MINI_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def mini_author_map(value):
    """MINI_AUTHOR_MAP: literal JSON {Notion user ID: member name}; one ID -> exactly one member (P2)."""
    if not value:
        return {}
    try:
        pairs = json.loads(value, object_pairs_hook=lambda items: items)
        from .models import RECIPIENTS
        names = set(RECIPIENTS) | {"たかむね"}
        if not isinstance(pairs, list):
            raise ValueError()
        result = {}
        for key, name in pairs:
            norm = key.replace("-", "").lower() if isinstance(key, str) else ""
            if (not re.fullmatch(r"[0-9a-f]{32}", norm) or norm in result
                    or not isinstance(name, str) or name not in names):
                raise ValueError()
            result[norm] = name
        return result
    except (ValueError, TypeError):
        raise AccountMismatch("MINI_AUTHOR_MAP の設定が不正です（値は表示しません）") from None


def path_list(value):
    """Colon-separated absolute paths (MINI_WRITE_ROOTS).  A relative entry is an error."""
    items = tuple(p for p in (v.strip() for v in value.split(":")) if p)
    if any(not p.startswith("/") for p in items):
        raise ValueError("MINI_WRITE_ROOTS")
    return items


# 2026-11-25 roadmap 2: the Codex CLI and macOS the outer sandbox and the final-answer
# signal were measured with.  Another combination keeps the lane from launching until a
# person re-initializes, re-runs the boundary tests and sets MINI_VERIFIED_* (review R7).
MINI_VERIFIED_CODEX = "0.169.0"
MINI_VERIFIED_MACOS = "27.2.1"
# The model the final-answer signal was measured with (2026-11-25); the launch pins it with -m
# and the dedicated home must name it (re-review 💚20261125-16, same kind as T1).
MINI_VERIFIED_MODEL = "gpt-6.2-sol"
# The official ledger (進行中プロジェクト): database and its data source.
MINI_LEDGER_IDS = ("a17a22bab5f9407c82bcc1161bf977de", "a55c89fde2314dfab739d520b8c5e31e")


def _hex_ids(value, key):
    ids = []
    for token in value.replace(",", " ").split():
        norm = token.replace("-", "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", norm):
            raise ValueError(key)
        ids.append(norm)
    return tuple(ids)


def mini_settings(env):
    """Every MINI_* value, read strictly (review M5 / R6).

    A wrong value never stops the whole patrol and is never silently corrected: it
    is listed in ``mini_config_errors`` (names only, never values) and the みに lane
    stays off, so requests for みに keep today's path.
    """
    errors = []
    out = {}

    def take(key, parse, default):
        raw = env.get(key, "")
        if raw == UNREADABLE_MINI:
            errors.append(key)
            return default
        try:
            return parse(raw.strip()) if raw.strip() else default
        except (ValueError, TypeError, AccountMismatch):
            errors.append(key)
            return default

    def transport(v):
        if v not in ("off", "local"):
            raise ValueError(v)
        return v

    def stage(v):
        if v not in ("0", "1", "2"):
            raise ValueError(v)
        return int(v)

    def effort(v):
        if v not in MINI_EFFORTS:
            raise ValueError(v)
        return v

    def minutes(v):
        if not re.fullmatch(r"[0-9]{1,3}", v) or not 1 <= int(v) <= 240:
            raise ValueError(v)
        return int(v)

    def version(v):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", v):
            raise ValueError(v)
        return v

    def model(v):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", v):
            raise ValueError(v)
        return v

    out["mini_transport"] = take("MINI_TRANSPORT", transport, "off")
    out["mini_stage"] = take("MINI_STAGE", stage, 0)
    out["mini_reasoning_effort"] = take("MINI_REASONING_EFFORT", effort, "max")
    out["mini_timeout_min"] = take("MINI_TIMEOUT_MIN", minutes, 90)
    out["mini_write_roots"] = take("MINI_WRITE_ROOTS", path_list, ())
    out["mini_author_map"] = take("MINI_AUTHOR_MAP", mini_author_map, {})
    out["mini_riri_notion_ids"] = take("MINI_RIRI_NOTION_IDS", lambda v: _hex_ids(v, "MINI_RIRI_NOTION_IDS"), ())
    out["mini_ledger_ids"] = take("MINI_LEDGER_IDS", lambda v: _hex_ids(v, "MINI_LEDGER_IDS"), MINI_LEDGER_IDS)
    out["mini_verified_codex"] = take("MINI_VERIFIED_CODEX", version, MINI_VERIFIED_CODEX)
    out["mini_verified_macos"] = take("MINI_VERIFIED_MACOS", version, MINI_VERIFIED_MACOS)
    out["mini_verified_model"] = take("MINI_VERIFIED_MODEL", model, MINI_VERIFIED_MODEL)
    for key in ("MINI_LOCAL_DIR", "MINI_RUN_DIR", "MINI_CODEX_BIN", "MINI_CODEX_HOME"):
        value = env.get(key, "").strip()
        if value == UNREADABLE_MINI.strip() or (value and not value.startswith("/")):
            errors.append(key)
            value = ""
        out[key.lower()] = value
    out["mini_config_errors"] = tuple(errors)
    return out


@dataclass(frozen=True)
class Config:
    root: Path
    mode: str = "practice"
    stop_file: Path | None = None
    slack_token: str = ""
    slack_report_user_token: str = field(default="", repr=False)
    expected_bot_id: str = ""
    expected_user_id: str = ""
    ice_mention_user_id: str = ""
    requester_mention_ids: dict = field(default_factory=dict)
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
    noa_wake_enabled: bool = False
    noa_fire_url: str = field(default="", repr=False)
    noa_fire_token: str = field(default="", repr=False)
    noa_transport: str = "routine"
    noa_local_dir: str = ""
    noa_claude_bin: str = ""
    noa_local_permission_mode: str = ""
    log_size_notice_bytes: int = 50 * 1024 * 1024
    log_flush_min: int = 180
    error_notify_min: int = 60
    max_posts: int = 4
    max_instructions: int = 1
    run_timeout: int = 240
    local_agent_mamu: bool = False
    local_agent_miki: bool = False
    agent_tools: tuple = ("read",)
    agent_hourly: int = 20
    agent_daily: int = 150
    # みにを起こす仕組み（設計 v1.0 の 7・特記事項）。既定はオフ。
    mini_transport: str = "off"
    mini_stage: int = 0
    mini_local_dir: str = ""
    mini_run_dir: str = ""
    mini_codex_bin: str = ""
    mini_codex_home: str = ""
    mini_reasoning_effort: str = "max"
    mini_timeout_min: int = 90
    mini_write_roots: tuple = ()
    mini_author_map: dict = field(default_factory=dict)
    mini_riri_notion_ids: tuple = ()
    mini_ledger_ids: tuple = MINI_LEDGER_IDS
    mini_verified_codex: str = MINI_VERIFIED_CODEX
    mini_verified_macos: str = MINI_VERIFIED_MACOS
    mini_verified_model: str = MINI_VERIFIED_MODEL
    mini_config_errors: tuple = ()     # names of wrong MINI_* settings (values are never kept)

    @property
    def secrets(self):
        return tuple(v for v in (self.slack_token, self.slack_report_user_token, self.notion_token, self.noa_fire_token, self.noa_fire_url) if v)

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
        from .mentions import validate_mention_id
        validate_mention_id(self.ice_mention_user_id, self.expected_user_id)
        if self.instruction_channel == self.report_channel:
            raise AccountMismatch("指示先と報告先は別のSlackチャンネルである必要があります")

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
        for name, suffix in {"あいす": "AISU", "のあ": "NOA", "みに": "MINI", "かれん": "KAREN", "うさ": "USA", "まむ": "MAMU", "みき": "MIKI", "みゅう": "MYU"}.items():
            value = env.get("NOTION_REPORT_PAGE_" + suffix)
            if value:
                pages[name] = "https://app.notion.com/p/" + value
        return cls(root=root, mode=mode, stop_file=stop,
                   slack_token=env.get("SLACK_BOT_TOKEN", ""),
                   slack_report_user_token=env.get("SLACK_REPORT_USER_TOKEN", ""),
                   expected_bot_id=expected_bot_id,
                   expected_user_id=expected_user_id,
                   ice_mention_user_id=env.get("ICE_MENTION_USER_ID", ""),
                   requester_mention_ids=requester_mentions(env.get("SLACK_REQUESTER_MENTION_IDS", "")),
                   instruction_channel=env.get("SLACK_INSTRUCTION_CHANNEL", "C0CFM2HRA5V"),
                   report_channel=env.get("SLACK_REPORT_CHANNEL", "C0CFL6A8QJH"),
                   notion_token=env.get("NOTION_TOKEN", ""), notion_version=env.get("NOTION_VERSION", "2026-03-11"),
                   requests_page=env.get("NOTION_REQUESTS_PAGE_ID", "413f1dff94dc81d2beebdf1b69f5c439"),
                   log_page=env.get("NOTION_LOG_PAGE_ID", "413f1dff94dc816097fbfe4d4d7706b8"), report_pages=pages,
                   ollama_url=env.get("OLLAMA_URL", "http://127.0.0.1:11434"),
                   ollama_model=env.get("OLLAMA_MODEL", "qwen3.6:35b-a3b"),
                   ollama_num_ctx=integer(env, "OLLAMA_NUM_CTX", 32768, 1024, 32768),
                   llm_enabled=env.get("LLM_ENABLED", "false").lower() == "true",
                   noa_wake_enabled=env.get("NOA_WAKE_ENABLED", "false").lower() == "true",
                   noa_fire_url=env.get("NOA_FIRE_URL", ""),
                   noa_fire_token=env.get("NOA_FIRE_TOKEN", ""),
                   noa_transport="local" if env.get("NOA_TRANSPORT", "routine").strip() == "local" else "routine",
                   noa_local_dir=env.get("NOA_LOCAL_DIR", "").strip(),
                   noa_claude_bin=env.get("NOA_CLAUDE_BIN", "").strip(),
                   noa_local_permission_mode=env.get("NOA_LOCAL_PERMISSION_MODE", "").strip(),
                   log_size_notice_bytes=integer(env, "LOG_SIZE_NOTICE_BYTES", 50 * 1024 * 1024, 1, 1024 ** 4),
                   log_flush_min=integer(env, "LOG_FLUSH_EVERY_MIN", 180, 180, 10080),
                   error_notify_min=integer(env, "ERROR_NOTIFY_EVERY_MIN", 60, 60, 10080),
                   max_posts=integer(env, "MAX_POSTS_PER_RUN", 4, 1, 4),
                   max_instructions=integer(env, "MAX_INSTRUCTION_POSTS_PER_RUN", 1, 1, 1),
                   run_timeout=integer(env, "RUN_TIMEOUT_SEC", 240, 1, 240),
                   local_agent_mamu=env.get("LOCAL_AGENT_MAMU", "false").lower() == "true",
                   local_agent_miki=env.get("LOCAL_AGENT_MIKI", "false").lower() == "true",
                   agent_tools=agent_tools(env.get("LOCAL_AGENT_TOOLS", "read")),
                   agent_hourly=integer(env, "LOCAL_AGENT_HOURLY", 20, 1, 60),
                   agent_daily=integer(env, "LOCAL_AGENT_DAILY", 150, 1, 500),
                   **mini_settings(env))

