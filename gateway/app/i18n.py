"""Bot text in English and Vietnamese, from locales/*.json. Values passed in must already be escaped."""
import json
import re
from pathlib import Path

_DIR = Path(__file__).parent / "locales"
DICTS: dict[str, dict[str, str]] = {lang: json.loads((_DIR / f"{lang}.json").read_text()) for lang in ("en", "vi")}


def t(lang: str, key: str, /, **params) -> str:
    s = DICTS.get(lang, {}).get(key) or DICTS["en"].get(key) or key
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), "")), s)


def has(key: str) -> bool:
    return key in DICTS["en"]
