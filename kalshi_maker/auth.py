"""Request signing for Kalshi.

Verified by reading docs.kalshi.com/getting_started/api_keys directly (2026-11-04):
  * headers KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (milliseconds), KALSHI-ACCESS-SIGNATURE
  * message = timestamp + HTTP METHOD + path, path starting with /trade-api/v2
  * the path is signed WITHOUT the query string ("strip the ? and everything after it")
  * two key types: Ed25519 (recommended, the default in Kalshi's UI) signs the message
    directly; RSA (2048-bit) uses RSA-PSS, MGF1(SHA-256), salt length = digest length.
    Kalshi picks the algorithm from the registered public key.
  * the PEM header does NOT identify the type (PKCS#8 RSA also says "BEGIN PRIVATE KEY"),
    so the type is detected from the parsed key object.
  * WebSocket handshake signs GET + /trade-api/ws/v2
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PrivateKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey

HDR_KEY = "KALSHI-ACCESS-KEY"
HDR_TS = "KALSHI-ACCESS-TIMESTAMP"
HDR_SIG = "KALSHI-ACCESS-SIGNATURE"
SENSITIVE_HEADERS = frozenset({HDR_KEY.lower(), HDR_SIG.lower(), "authorization"})


class AuthError(Exception):
    pass


def load_private_key(path: Path) -> PrivateKey:
    data = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as e:
        raise AuthError(f"could not parse PEM private key at {path}: {type(e).__name__}") from e
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise AuthError("RSA key smaller than 2048 bits")
        return key
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key
    raise AuthError(f"unsupported key type {type(key).__name__}; need RSA or Ed25519")


def signing_path(url_or_path: str) -> str:
    """Return the path to sign: no scheme/host, no query string, no fragment."""
    path = urlsplit(url_or_path).path
    if not path.startswith("/trade-api/"):
        raise AuthError(f"signing path must start with /trade-api/, got {path!r}")
    return path


def sign_message(key: PrivateKey, message: str) -> str:
    msg = message.encode("utf-8")
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        sig = key.sign(msg)
    return base64.b64encode(sig).decode("ascii")


class Signer:
    def __init__(self, key_id: str, key: PrivateKey) -> None:
        if not key_id:
            raise AuthError("empty key id")
        self._key_id = key_id
        self._key = key

    @classmethod
    def from_file(cls, key_id: str, path: Path) -> Signer:
        return cls(key_id, load_private_key(path))

    def headers(self, method: str, url_or_path: str, now_ms: int | None = None) -> dict[str, str]:
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        msg = ts + method.upper() + signing_path(url_or_path)
        return {HDR_KEY: self._key_id, HDR_TS: ts, HDR_SIG: sign_message(self._key, msg)}

    def ws_headers(self, ws_url: str, now_ms: int | None = None) -> dict[str, str]:
        return self.headers("GET", ws_url, now_ms)

    def __repr__(self) -> str:  # never leak key material
        return "Signer(<redacted>)"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
