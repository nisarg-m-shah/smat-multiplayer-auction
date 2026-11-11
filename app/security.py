import hashlib, secrets
from .config import APP_SECRET

# Passwords handed to franchises/host: easy to read out and type on a phone (no 0/O, 1/I/L).
_PW_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

def new_token():
    return secrets.token_urlsafe(32)

def new_password() -> str:
    """10 characters (~49 bits), shown as XXXXX-XXXXX. Used as the bearer credential."""
    raw = "".join(secrets.choice(_PW_ALPHABET) for _ in range(10))
    return raw[:5] + "-" + raw[5:]

def normalize_credential(value: str) -> str:
    """Typed passwords are forgiving about case, spaces and the dash. Legacy long invite tokens
    (case-sensitive, 43 chars) are returned untouched."""
    value = (value or "").strip()
    if len(value) <= 20:
        return value.replace("-", "").replace(" ", "").upper()
    return value

def token_hash(token: str) -> str:
    # Hash credentials before storing them. The raw value is only shown at room creation.
    return hashlib.sha256((APP_SECRET + normalize_credential(token)).encode("utf-8")).hexdigest()
