import hashlib, secrets
from .config import APP_SECRET

def new_token():
    return secrets.token_urlsafe(32)

def token_hash(token: str) -> str:
    # Hash invite tokens before storing them. The raw token is only shown at room creation.
    return hashlib.sha256((APP_SECRET + token).encode("utf-8")).hexdigest()

# ---- name + password login -------------------------------------------------------------------
# A franchise's bearer token is DERIVED from a per-record random nonce (HMAC with APP_SECRET), so a
# correct login can re-issue the very same token on any device without the raw token being stored.
# The bid hot path is unchanged: it still authenticates by token_hash(token) exactly as before.
import hmac, base64

_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L: easy to read out over a call
_PBKDF2_ROUNDS = 120_000

def new_nonce() -> str:
    return secrets.token_hex(16)

def derive_token(kind: str, nonce: str) -> str:
    mac = hmac.new(APP_SECRET.encode("utf-8"), f"{kind}:{nonce}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

def new_password() -> str:
    raw = "".join(secrets.choice(_PW_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]

def _norm_pw(password: str) -> str:
    return "".join(str(password).upper().split()).replace("-", "")

def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", _norm_pw(password).encode(), salt.encode(), _PBKDF2_ROUNDS)
    return f"{salt}${dk.hex()}"

def check_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        # still burn comparable time so a missing password is not distinguishable by timing
        hashlib.pbkdf2_hmac("sha256", b"x", b"x", _PBKDF2_ROUNDS)
        return False
    salt, want = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", _norm_pw(password).encode(), salt.encode(), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), want)
