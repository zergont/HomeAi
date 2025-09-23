from __future__ import annotations

STRINGS = {
    "en": {
        "instruction": "Follow the rules. Do not reveal chain-of-thought. Answer in the user's language.",
        "core_profile": "CORE PROFILE",
        "tool_results": "TOOL RESULTS",
        "recap_l3": "RECAP L3 (micro-summary)",
        "recap_l2": "RECAP L2 (summary)",
        "divider": "---",
    },
    "ru": {
        "instruction": "Следуй правилам. Не раскрывай ход рассуждений. Отвечай на языке пользователя.",
        "core_profile": "ПРОФИЛЬ (ЯДРО)",
        "tool_results": "РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ",
        "recap_l3": "ОБЗОР L3 (микро-саммари)",
        "recap_l2": "ОБЗОР L2 (саммари)",
        "divider": "---",
    },
}

def pick_lang(last_user_lang: str | None, preferred: str | None) -> str:
    lang = (last_user_lang or preferred or "en").lower()
    return "ru" if lang.startswith("ru") else "en"


def t(lang: str, key: str) -> str:
    return STRINGS.get(lang, STRINGS["en"]).get(key, key.upper())
