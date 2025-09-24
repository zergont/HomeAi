from packages.utils.i18n import pick_lang

def make_retry_suffix(lang: str, part_max: int, think_pct: float) -> str:
    if lang == "ru":
        return (f"Предыдущий ответ оборвался по длине. Снова ответь кратко и по делу. "
                f"Можно использовать <think>, но очень коротко (не более ~{int(think_pct*100)}%). "
                f"Если ответ длинный — разбей на части до ~{part_max} токенов и в конце части спроси: «Продолжить? (да/нет)». "
                f"Не повторяй черновик. Отвечай как цельный новый ответ.")
    else:
        return (f"The previous reply was cut by length. Answer again, concise. "
                f"You may use <think>, but keep it very short (~{int(think_pct*100)}%). "
                f"If long, split into parts up to ~{part_max} tokens and end with 'Continue? (yes/no)'. "
                f"Do not repeat the previous draft. Respond as a clean new answer.")

def should_retry_length(attempt: int, enabled: bool, max_attempts: int) -> bool:
    return enabled and (attempt < max_attempts)
