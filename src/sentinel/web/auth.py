"""User store, password hashing, and token signing — stdlib only, zero extra deps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Literal

Role = Literal["admin", "user"]

_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
_USERS_FILE = _DATA_DIR / "users.json"
_SECRET_FILE = _DATA_DIR / ".secret"
_TOKEN_TTL = 60 * 60 * 12  # 12 hours


def _ensure_dir() -> None:
    _DATA_DIR.mkdir(exist_ok=True)


def _secret() -> bytes:
    _ensure_dir()
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes()
    key = secrets.token_bytes(32)
    _SECRET_FILE.write_bytes(key)
    return key


def _load() -> dict[str, dict]:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(users: dict[str, dict]) -> None:
    _ensure_dir()
    _USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def _hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return base64.b64encode(dk).decode(), base64.b64encode(salt).decode()


def _verify(password: str, hashed: str, salt_b64: str) -> bool:
    salt = base64.b64decode(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return hmac.compare_digest(base64.b64encode(dk).decode(), hashed)


# --- public API ---------------------------------------------------------------

def users_exist() -> bool:
    return bool(_load())


def create_user(username: str, password: str, role: Role = "user") -> bool:
    """Returns False if username already exists."""
    users = _load()
    if username in users:
        return False
    hashed, salt = _hash(password)
    users[username] = {"username": username, "role": role, "hash": hashed, "salt": salt}
    _save(users)
    return True


def delete_user(username: str) -> bool:
    users = _load()
    if username not in users:
        return False
    del users[username]
    _save(users)
    return True


def authenticate(username: str, password: str) -> dict | None:
    """Returns {username, role} if credentials are valid, else None."""
    u = _load().get(username)
    if not u or not _verify(password, u["hash"], u["salt"]):
        return None
    return {"username": u["username"], "role": u["role"]}


def list_users() -> list[dict]:
    return [{"username": u["username"], "role": u["role"]} for u in _load().values()]


def create_token(username: str, role: str) -> str:
    payload = json.dumps({"username": username, "role": role, "exp": int(time.time()) + _TOKEN_TTL})
    p64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_secret(), p64.encode(), hashlib.sha256).hexdigest()
    return f"{p64}.{sig}"


def verify_token(token: str) -> dict | None:
    """Returns {username, role} if valid and not expired, else None."""
    try:
        p64, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), p64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p64 + "=="))
        if payload["exp"] < int(time.time()):
            return None
        return {"username": payload["username"], "role": payload["role"]}
    except Exception:
        return None
