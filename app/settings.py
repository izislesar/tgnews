"""Единая конфигурация приложения.

Правило проекта: `os.getenv` больше нигде не вызывается — только здесь.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def _read_env_file(path: Path) -> dict[str, str]:
    """Минимальный парсер .env (нужен для локального CLI, не для контейнера)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def _get(env: dict[str, str], name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, env.get(name, default))


def _int(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(_get(env, name, str(default)) or default)
    except ValueError:
        return default


def _float(env: dict[str, str], name: str, default: float) -> float:
    try:
        return float(_get(env, name, str(default)) or default)
    except ValueError:
        return default


DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"  # Telegram Desktop


@dataclass(frozen=True, slots=True)
class Settings:
    # --- Telegram ---
    bot_token: str = ""
    admin_ids: str = ""
    session_path: str = "/data/session"
    bot_username: str = "lalsdjasdljkbot"
    api_id: int = DEFAULT_API_ID
    api_hash: str = DEFAULT_API_HASH
    approval_chat_id: str = ""

    # --- LLM ---
    llm_base: str = "https://opencode.ai/inference/go/v1"
    llm_key: str = ""
    model: str = "muse-spark-1.3-contributor"
    fallback_model: str = "glm-5.3-flash"
    llm_timeout: float = 90.0
    llm_max_output: int = 3000

    # --- выдача ---
    get_n: int = 5
    get_max: int = 20
    get_candidates: int = 15
    media_wait_s: float = 120.0
    link_gap: int = 5

    # --- хранилище ---
    db_path: str = "/data/queue.db"

    # --- поиск ---
    searx_url: str = "http://searxng:8080"
    searx_timeout: float = 20.0
    search_concurrency: int = 2
    search_results: int = 12
    searx_fallback_engines: str = "bing"

    # --- загрузка страниц ---
    fetch_timeout: float = 12.0
    fetch_max_bytes: int = 2_000_000
    fetch_concurrency: int = 4
    fetch_min_interval: float = 1.0  # вежливость: не чаще 1 запроса/сек на хост
    user_agent: str = DEFAULT_USER_AGENT

    # --- фактчекинг ---
    fc_mode: str = "off"  # off | shadow | on
    fc_cycles: int = 2
    fc_max_claims: int = 6
    fc_deadline_s: float = 90.0
    fc_calls_per_post: int = 12
    fc_judge_concurrency: int = 4
    fc_top_hits: int = 4
    fc_snippets_per_claim: int = 3
    fc_min_text: int = 200
    fc_local_timeout: float = 8.0
    fc_claim_model: str = ""  # пусто -> fallback_model (дешёвая модель)
    fc_judge_model: str = ""  # пусто -> fallback_model

    # --- промпты ---
    prompts_dir: str = ""

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> "Settings":
        env = _read_env_file(Path(env_file)) if env_file else {}
        defaults = cls()  # slots-датакласс: значения по умолчанию берём из экземпляра
        return cls(
            bot_token=_get(env, "BOT_TOKEN", defaults.bot_token) or defaults.bot_token,
            admin_ids=_get(env, "ADMIN_IDS", defaults.admin_ids) or defaults.admin_ids,
            session_path=_get(env, "SESSION_PATH", defaults.session_path)
            or defaults.session_path,
            bot_username=_get(env, "BOT_USERNAME", defaults.bot_username)
            or defaults.bot_username,
            api_id=_int(env, "API_ID", defaults.api_id),
            api_hash=_get(env, "API_HASH", defaults.api_hash) or defaults.api_hash,
            approval_chat_id=_get(env, "APPROVAL_CHAT_ID", defaults.approval_chat_id)
            or defaults.approval_chat_id,
            llm_base=_get(env, "OPENCODE_BASE", defaults.llm_base) or defaults.llm_base,
            llm_key=_get(env, "OPENCODE_API_KEY", defaults.llm_key) or defaults.llm_key,
            model=_get(env, "OPENCODE_MODEL", defaults.model) or defaults.model,
            fallback_model=_get(env, "OPENCODE_MODEL_FALLBACK", defaults.fallback_model)
            or defaults.fallback_model,
            llm_timeout=_float(env, "LLM_TIMEOUT", defaults.llm_timeout),
            llm_max_output=_int(env, "LLM_MAX_OUTPUT", defaults.llm_max_output),
            get_n=_int(env, "GET_N", defaults.get_n),
            get_max=_int(env, "GET_MAX", defaults.get_max),
            get_candidates=_int(env, "GET_CANDIDATES", defaults.get_candidates),
            media_wait_s=_float(env, "MEDIA_WAIT_S", defaults.media_wait_s),
            link_gap=_int(env, "LINK_GAP", defaults.link_gap),
            db_path=_get(env, "DB_PATH", defaults.db_path) or defaults.db_path,
            searx_url=_get(env, "SEARX_URL", defaults.searx_url) or defaults.searx_url,
            searx_timeout=_float(env, "SEARX_TIMEOUT", defaults.searx_timeout),
            search_concurrency=_int(env, "SEARCH_CONCURRENCY", defaults.search_concurrency),
            search_results=_int(env, "SEARCH_RESULTS", defaults.search_results),
            searx_fallback_engines=_get(env, "SEARX_FALLBACK_ENGINES",
                                        defaults.searx_fallback_engines)
            or defaults.searx_fallback_engines,
            fetch_timeout=_float(env, "FETCH_TIMEOUT", defaults.fetch_timeout),
            fetch_max_bytes=_int(env, "FETCH_MAX_BYTES", defaults.fetch_max_bytes),
            fetch_concurrency=_int(env, "FETCH_CONCURRENCY", defaults.fetch_concurrency),
            fetch_min_interval=_float(env, "FETCH_MIN_INTERVAL", defaults.fetch_min_interval),
            user_agent=_get(env, "USER_AGENT", defaults.user_agent) or defaults.user_agent,
            fc_mode=(_get(env, "FC_ENABLED", defaults.fc_mode) or defaults.fc_mode).lower(),
            fc_cycles=_int(env, "FC_CYCLES", defaults.fc_cycles),
            fc_max_claims=_int(env, "FC_MAX_CLAIMS", defaults.fc_max_claims),
            fc_deadline_s=_float(env, "FC_DEADLINE_S", defaults.fc_deadline_s),
            fc_calls_per_post=_int(env, "FC_CALLS_PER_POST", defaults.fc_calls_per_post),
            fc_judge_concurrency=_int(env, "FC_JUDGE_CONCURRENCY", defaults.fc_judge_concurrency),
            fc_top_hits=_int(env, "FC_TOP_HITS", defaults.fc_top_hits),
            fc_snippets_per_claim=_int(env, "FC_SNIPPETS", defaults.fc_snippets_per_claim),
            fc_min_text=_int(env, "FC_MIN_TEXT", defaults.fc_min_text),
            fc_local_timeout=_float(env, "FC_LOCAL_TIMEOUT", defaults.fc_local_timeout),
            fc_claim_model=_get(env, "FC_CLAIM_MODEL", defaults.fc_claim_model)
            or defaults.fc_claim_model,
            fc_judge_model=_get(env, "FC_JUDGE_MODEL", defaults.fc_judge_model)
            or defaults.fc_judge_model,
            prompts_dir=_get(env, "PROMPTS_DIR", defaults.prompts_dir) or defaults.prompts_dir,
        )

    @property
    def claim_model(self) -> str:
        return self.fc_claim_model or self.fallback_model

    @property
    def judge_model(self) -> str:
        return self.fc_judge_model or self.fallback_model
