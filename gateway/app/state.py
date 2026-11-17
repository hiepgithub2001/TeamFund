"""Short-lived conversation state (open menus, "send me the name" prompts).

Lost on restart, which is fine: the user just sends the command again."""
import time

_store: dict[str, tuple[float, object]] = {}


def set_state(key: str, value, ttl: float = 30 * 60) -> None:
    now = time.monotonic()
    if len(_store) > 5000:
        for k in [k for k, (exp, _) in _store.items() if exp < now]:
            del _store[k]
    _store[key] = (now + ttl, value)


def get_state(key: str):
    item = _store.get(key)
    if not item:
        return None
    exp, value = item
    if exp < time.monotonic():
        del _store[key]
        return None
    return value


def del_state(key: str) -> None:
    _store.pop(key, None)
