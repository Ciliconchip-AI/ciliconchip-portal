"""Passwords for the Ciliconchip Portal: salted PBKDF2-SHA256 hashes, password rules, temporary passwords."""

import hashlib
import hmac
import re
import secrets

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
PBKDF2_ROUNDS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = (stored or "").split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    """'' when the password is acceptable, otherwise what is wrong with it."""
    if len(password or "") < 8:
        return "Use at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Use both letters and numbers."
    return ""


def temporary_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(10))
        if not password_problem(pw):
            return pw
