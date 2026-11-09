import hashlib, secrets
from .config import APP_SECRET

def new_token():
    return secrets.token_urlsafe(32)

def token_hash(token: str) -> str:
    # Hash invite tokens before storing them. The raw token is only shown at room creation.
    return hashlib.sha256((APP_SECRET + token).encode("utf-8")).hexdigest()
