"""
Encryption for the most sensitive Portal data: salary figures, payslips, PAN and bank details.

The key lives only in Streamlit secrets (PORTAL_ENCRYPTION_KEY), never in the database or in GitHub, so a copy
of the database on its own reveals no salaries. Losing the key means the encrypted data cannot be read again -
keep a copy of it somewhere safe and offline.

Generate a key once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import json
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, key: Optional[str]):
        self.ready = False
        self._f = None
        if key:
            try:
                self._f = Fernet(key.strip().encode())
                self.ready = True
            except (ValueError, TypeError):
                self.ready = False

    def lock(self, value: Any) -> Optional[bytes]:
        if value is None:
            return None
        if not self.ready:
            raise RuntimeError("Encryption key is not configured")
        return self._f.encrypt(json.dumps(value, default=str).encode())

    def unlock(self, blob: Optional[bytes], default: Any = None) -> Any:
        if not blob:
            return default
        if not self.ready:
            raise RuntimeError("Encryption key is not configured")
        try:
            return json.loads(self._f.decrypt(bytes(blob)).decode())
        except InvalidToken:
            raise RuntimeError("This data was encrypted with a different key")


def mask(value: str, keep: int = 4) -> str:
    v = (value or "").strip()
    if len(v) <= keep:
        return v
    return "•" * (len(v) - keep) + v[-keep:]
