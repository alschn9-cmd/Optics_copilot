import re

from app.models import ContentItem

MIN_LENGTH_RATIO = 0.4
MAX_LENGTH_RATIO = 2.5
LATIN_SHARE_LIMIT = 0.3

_REFUSAL = re.compile(
    r"как языковая модель|as an? (?:ai )?language model|i cannot assist|не могу помочь",
    re.IGNORECASE,
)


def find_anomalies(item: ContentItem) -> list[str]:
    reasons: list[str] = []
    title = (item.title_ru or "").strip()
    body = (item.body_ru or "").strip()
    if not title:
        reasons.append("Пустой заголовок перевода")
    if not body:
        reasons.append("Пустой текст перевода")

    original = f"{item.title_original}{item.body_original}"
    translated = f"{title}{body}"
    if original and translated:
        ratio = len(translated) / len(original)
        if ratio < MIN_LENGTH_RATIO:
            reasons.append("Перевод короче 40% оригинала")
        elif ratio > MAX_LENGTH_RATIO:
            reasons.append("Перевод длиннее 250% оригинала")

    if translated and latin_share(translated) > LATIN_SHARE_LIMIT:
        reasons.append("Слишком большая доля латиницы")

    if _REFUSAL.search(translated):
        reasons.append("Ответ модели похож на отказ")
    return reasons


def latin_share(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for char in letters if "a" <= char.lower() <= "z")
    return latin / len(letters)
