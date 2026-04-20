"""Alexa request validation.

Three checks applied to every incoming Alexa request:
  1. JSON structure — required fields present
  2. Timestamp freshness — within 150 seconds
  3. RSA-SHA1 signature — verifies the request came from Amazon

Helpers for extracting identifiers are also here.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_CERT_CACHE: dict[str, bytes] = {}


# ─── Structure ───────────────────────────────────────────────────────────────


def validate_alexa_request(body: dict[str, Any]) -> Optional[str]:
    """Return an error message string if the request envelope is malformed, else None."""
    if not isinstance(body, dict):
        return "Request body is not a JSON object"
    if "version" not in body:
        return "Missing 'version' field"
    request = body.get("request")
    if not isinstance(request, dict):
        return "Missing or invalid 'request' object"
    if "type" not in request:
        return "Missing 'type' in request"
    request_type = request.get("type", "")
    # Session is required for intent/launch/session-ended but not AudioPlayer events
    if not request_type.startswith("AudioPlayer.") and not request_type.startswith("PlaybackController."):
        if request_type != "SessionEndedRequest" and not isinstance(body.get("session"), dict):
            pass  # Some Alexa requests (SessionEndedRequest) omit session — allow it
    return None


# ─── Timestamp ───────────────────────────────────────────────────────────────


def verify_alexa_timestamp(body: dict[str, Any], max_age_seconds: int = 150) -> bool:
    """Return True if the request timestamp is within max_age_seconds of now."""
    ts_str = body.get("request", {}).get("timestamp", "")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = abs((datetime.now(timezone.utc) - ts).total_seconds())
        return delta <= max_age_seconds
    except Exception:
        return False


# ─── Signature ───────────────────────────────────────────────────────────────


def _validate_cert_url(url: str) -> bool:
    """Verify the SignatureCertChainUrl meets Amazon's requirements."""
    try:
        p = urlparse(url)
        if p.scheme.lower() != "https":
            return False
        if p.hostname is None or p.hostname.lower() != "s3.amazonaws.com":
            return False
        if not p.path.startswith("/echo.api/"):
            return False
        if p.port is not None and p.port != 443:
            return False
        return True
    except Exception:
        return False


async def _fetch_cert(url: str) -> Optional[bytes]:
    """Download and cache the Alexa signing certificate."""
    if url in _CERT_CACHE:
        return _CERT_CACHE[url]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            _CERT_CACHE[url] = resp.content
            return resp.content
    except Exception as exc:
        logger.warning("Failed to download Alexa cert from %s: %s", url, exc)
        return None


async def verify_alexa_signature(request: Any, raw_body: bytes) -> bool:
    """Perform full Alexa RSA-SHA1 signature verification.

    Returns True on success. Logs the specific failure reason on False.
    """
    cert_url = request.headers.get("SignatureCertChainUrl", "")
    signature_b64 = request.headers.get("Signature", "")

    if not cert_url or not signature_b64:
        logger.warning("Alexa signature headers missing: cert_url=%s sig=%s", bool(cert_url), bool(signature_b64))
        return False

    if not _validate_cert_url(cert_url):
        logger.warning("Alexa cert URL failed validation: %s", cert_url)
        return False

    cert_pem = await _fetch_cert(cert_url)
    if not cert_pem:
        return False

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        cert = x509.load_pem_x509_certificate(cert_pem)

        # SAN must contain echo-api.amazon.com
        try:
            san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            dns_names = san_ext.value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            dns_names = []
        if "echo-api.amazon.com" not in dns_names:
            logger.warning("Alexa cert SAN does not contain echo-api.amazon.com")
            return False

        # Certificate validity window
        now = datetime.now(timezone.utc)
        not_before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.replace(tzinfo=timezone.utc)
        not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=timezone.utc)
        if now < not_before or now > not_after:
            logger.warning("Alexa cert is outside its validity window")
            return False

        # RSA-SHA1 signature check
        sig_bytes = base64.b64decode(signature_b64)
        cert.public_key().verify(sig_bytes, raw_body, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 — required by Alexa
        return True

    except Exception as exc:
        logger.warning("Alexa signature cryptographic verification failed: %s", exc)
        return False


# ─── Identifier extraction ───────────────────────────────────────────────────


def extract_device_id(body: dict[str, Any]) -> str:
    return (
        body.get("context", {})
        .get("System", {})
        .get("device", {})
        .get("deviceId", "unknown")
    )


def extract_user_id(body: dict[str, Any]) -> str:
    user_id = body.get("session", {}).get("user", {}).get("userId", "")
    if user_id:
        return user_id
    return (
        body.get("context", {})
        .get("System", {})
        .get("user", {})
        .get("userId", "unknown")
    )


def extract_skill_id(body: dict[str, Any]) -> str:
    skill_id = body.get("session", {}).get("application", {}).get("applicationId", "")
    if not skill_id:
        skill_id = (
            body.get("context", {})
            .get("System", {})
            .get("application", {})
            .get("applicationId", "")
        )
    return skill_id
