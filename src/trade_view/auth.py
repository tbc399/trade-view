import hashlib
import hmac
import secrets
import time

import jwt


AUTH_COOKIE_NAME = "trade_view_session"
SESSION_TTL_SECONDS = 48 * 60 * 60

PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 600_000
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    )
    encoded_digest = _base64url_encode(digest)
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${encoded_digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected_digest = password_hash.split("$", 3)
        iterations = int(iterations_text)
    except ValueError:
        return False

    if algorithm != PASSWORD_HASH_ALGORITHM or iterations <= 0:
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return hmac.compare_digest(_base64url_encode(digest), expected_digest)


def create_session_token(username: str, signing_secret: str) -> str:
    issued_at = int(time.time())
    payload = {
        "sub": username,
        "iat": issued_at,
        "exp": issued_at + SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, signing_secret, algorithm=JWT_ALGORITHM)


def verify_session_token(
    token: str,
    *,
    expected_username: str,
    signing_secret: str,
) -> bool:
    try:
        payload = jwt.decode(
            token,
            signing_secret,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.InvalidTokenError:
        return False

    return payload.get("sub") == expected_username


def _base64url_encode(value: bytes) -> str:
    return jwt.utils.base64url_encode(value).decode("ascii")
