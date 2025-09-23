# packages/core/settings.py
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Optional, Union

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    app_env: str = "dev"
    app_name: str = "Local Responses Hub"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    log_level: str = "INFO"
    db_url: str = "sqlite:///data/app.db"

    # Provider endpoints
    lmstudio_base_url: Optional[Union[AnyUrl, str]] = Field(
        default="http://192.168.0.111:1234", validation_alias="LMSTUDIO_BASE_URL"
    )

    # Context manager
    ctx_max_input_tokens: int = Field(default=2048, validation_alias="CTX_MAX_INPUT_TOKENS")
    ctx_summary_max_age_sec: int = Field(default=3600, validation_alias="CTX_SUMMARY_MAX_AGE_SEC")

    # Context/model info & budgets
    ctx_model_info_ttl_sec: int = Field(default=300, validation_alias="CTX_MODEL_INFO_TTL_SEC")
    ctx_safety_pct: float = Field(default=0.10, validation_alias="CTX_SAFETY_PCT")
    ctx_rsys_pct: float = Field(default=0.05, validation_alias="CTX_RSYS_PCT")
    ctx_rsys_min: int = Field(default=256, validation_alias="CTX_RSYS_MIN")
    ctx_rout_pct: float = Field(default=0.25, validation_alias="CTX_ROUT_PCT")
    ctx_rout_default: int = Field(default=512, validation_alias="CTX_ROUT_DEFAULT")
    ctx_default_context_length: int = Field(default=4096, validation_alias="CTX_DEFAULT_CONTEXT_LENGTH")
    ctx_core_sys_pad_tok: int = Field(default=100, validation_alias="CTX_CORE_SYS_PAD_TOK")
    context_min_core_skeleton_tok: int = Field(default=60, validation_alias="CONTEXT_MIN_CORE_SKELETON_TOK")

    # Memory L1/L2/L3 settings
    mem_l1_share: float = Field(default=0.60, validation_alias="MEM_L1_SHARE")
    mem_l2_share: float = Field(default=0.30, validation_alias="MEM_L2_SHARE")
    mem_l3_share: float = Field(default=0.10, validation_alias="MEM_L3_SHARE")
    mem_tools_max_share: float = Field(default=0.15, validation_alias="MEM_TOOLS_MAX_SHARE")
    mem_free_threshold: float = Field(default=0.05, validation_alias="MEM_FREE_THRESHOLD")
    mem_promotion_batch_size: int = Field(default=4, validation_alias="MEM_PROMOTION_BATCH_SIZE")
    cap_tok_user: int = Field(default=120, validation_alias="CAP_TOK_USER")
    cap_tok_assistant: int = Field(default=80, validation_alias="CAP_TOK_ASSISTANT")
    lang_follows_last_user: bool = Field(default=True, validation_alias="LANG_FOLLOWS_LAST_USER")

    # Summarizer
    summary_trigger_tokens: int = Field(default=100, validation_alias="SUMMARY_TRIGGER_TOKENS")
    summary_system_prompt: str = Field(
        default="Суммируй диалог кратко, по фактам, без рассуждений. "
                "Выдай 1–3 абзаца или компактные пункты. Не цитируй логи инструментов. "
                "Сохраняй язык пользователя.",
        validation_alias="SUMMARY_SYSTEM_PROMPT",
    )
    default_summary_model: str = Field(default="qwen2.5-instruct", validation_alias="DEFAULT_SUMMARY_MODEL")
    summary_max_chars: int = Field(default=900, validation_alias="SUMMARY_MAX_CHARS")
    summary_debounce_sec: int = Field(default=300, validation_alias="SUMMARY_DEBOUNCE_SEC")

    # Pricing overrides
    price_per_1k_default: float = Field(default=0.0, validation_alias="PRICE_PER_1K_DEFAULT")
    price_overrides: Dict[str, float] = Field(default_factory=dict)

    # Tool runs settings
    TOOL_RUNS_CACHE_TTL_SEC: int = 86400  # сутки
    TOOL_ARGS_HASH_ALGO: str = "sha256"

    # Retry settings
    RETRY_ON_LENGTH_ENABLED: bool = True
    RETRY_ON_LENGTH_MAX: int = 1
    RETRY_PART_MAX_TOK: int = 1500
    RETRY_THINK_MAX_PCT: float = 0.10

    @property
    def db_dialect(self) -> str:
        # Extract dialect part from SQLAlchemy URL (e.g., sqlite)
        return self.db_url.split(":", 1)[0] if ":" in self.db_url else self.db_url


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    # Load dynamic price overrides from env like PRICE__lmstudio__default=0.0
    import os

    overrides: Dict[str, float] = {}
    for k, v in os.environ.items():
        if not k.startswith("PRICE__"):
            continue
        parts = k.split("__", 2)
        if len(parts) == 3:
            prov = parts[1].lower()
            mdl = parts[2].lower()
            key = f"{prov}:{mdl}"
            try:
                overrides[key] = float(v)
            except ValueError:
                continue
    s = AppSettings()
    s.price_overrides = overrides
    return s
