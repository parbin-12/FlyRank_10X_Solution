"""
Auth layer — Concept #3: Authentication.

Passwords are salted + hashed with PBKDF2-HMAC-SHA256 (stdlib hashlib, no
plaintext or reversible storage). Sessions are stateless JWTs. Protected
routes verify the JWT via the `require_user` FastAPI dependency — a request
with no token, a bad token, or an expired token gets a real 401, not a
silent pass-through.
"""
import hashlib
import os
import secrets
import time

import jwt
from fastapi import Header, HTTPException

JWT_SECRET = os.environ.get("PULSELOG_JWT_SECRET", "dev-secret-change-me-in-prod")
JWT_ALGO = "HS256"
JWT_TTL_SECONDS = 60 * 60 * 12  # 12 hours


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return digest, salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    digest, _ = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


def issue_token(user_id: int, email: str) -> str:
    payload = {"sub": user_id, "email": email, "exp": time.time() + JWT_TTL_SECONDS}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def require_user(authorization: str = Header(default=None)) -> dict:
    """FastAPI dependency: protects a route. Raises 401 on any bad auth state."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"id": payload["sub"], "email": payload["email"]}
