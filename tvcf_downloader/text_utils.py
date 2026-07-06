import locale
import os
from typing import Any


def decode_output(data: Any, preferred: str | None = None) -> str:
    if isinstance(data, str):
        return data
    if data is None:
        return ""

    try:
        raw = bytes(data)
    except (TypeError, ValueError):
        return str(data)

    encodings: list[str] = []
    if preferred:
        encodings.append(preferred)
    encodings.extend(["utf-8-sig", "utf-8", "cp949", "euc-kr", locale.getpreferredencoding(False)])

    seen: set[str] = set()
    for encoding in encodings:
        key = encoding.lower()
        if not encoding or key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    return raw.decode("utf-8", errors="replace")


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env
