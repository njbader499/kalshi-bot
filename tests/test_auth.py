from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi_maker.auth import (
    HDR_KEY,
    HDR_SIG,
    HDR_TS,
    AuthError,
    Signer,
    load_private_key,
    redact_headers,
    signing_path,
)
from kalshi_maker.logging_setup import JsonFormatter


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verify_rsa(key: rsa.RSAPrivateKey, sig_b64: str, msg: str) -> None:
    key.public_key().verify(
        base64.b64decode(sig_b64),
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_rsa_pss_signature_over_ts_method_path(rsa_key: rsa.RSAPrivateKey) -> None:
    s = Signer("kid", rsa_key)
    h = s.headers("get", "https://x.kalshi.co/trade-api/v2/portfolio/balance", now_ms=1700000000123)
    assert h[HDR_KEY] == "kid"
    assert h[HDR_TS] == "1700000000123"  # milliseconds
    _verify_rsa(rsa_key, h[HDR_SIG], "1700000000123GET/trade-api/v2/portfolio/balance")


def test_query_string_not_signed(rsa_key: rsa.RSAPrivateKey) -> None:
    s = Signer("kid", rsa_key)
    h = s.headers("GET", "/trade-api/v2/portfolio/orders?limit=5&cursor=abc", now_ms=1)
    _verify_rsa(rsa_key, h[HDR_SIG], "1GET/trade-api/v2/portfolio/orders")


def test_ws_signs_ws_path(rsa_key: rsa.RSAPrivateKey) -> None:
    s = Signer("kid", rsa_key)
    h = s.ws_headers("wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2", now_ms=5)
    _verify_rsa(rsa_key, h[HDR_SIG], "5GET/trade-api/ws/v2")


def test_path_must_include_prefix() -> None:
    with pytest.raises(AuthError):
        signing_path("/portfolio/balance")
    assert signing_path("https://h/trade-api/v2/markets?x=1") == "/trade-api/v2/markets"


def test_ed25519_supported() -> None:
    k = ed25519.Ed25519PrivateKey.generate()
    h = Signer("kid", k).headers("POST", "/trade-api/v2/portfolio/orders", now_ms=9)
    k.public_key().verify(base64.b64decode(h[HDR_SIG]), b"9POST/trade-api/v2/portfolio/orders")


def test_load_key_from_pem(tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> None:
    p = tmp_path / "k.pem"
    p.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    assert isinstance(load_private_key(p), rsa.RSAPrivateKey)
    p.write_text("garbage")
    with pytest.raises(AuthError):
        load_private_key(p)


def test_redaction(rsa_key: rsa.RSAPrivateKey) -> None:
    h = Signer("kid-123", rsa_key).headers("GET", "/trade-api/v2/x", now_ms=1)
    red = redact_headers(h)
    assert red[HDR_SIG] == "<redacted>" and red[HDR_KEY] == "<redacted>"
    assert red[HDR_TS] == "1"
    assert "kid" not in repr(Signer("kid-123", rsa_key))


def test_json_log_redacts_signature() -> None:
    rec = logging.makeLogRecord(
        {
            "msg": "headers {'KALSHI-ACCESS-SIGNATURE': 'abcd=='}",
            "levelname": "INFO",
            "signature": "zzz",
        }
    )
    out = JsonFormatter().format(rec)
    assert "abcd==" not in out and "zzz" not in out
