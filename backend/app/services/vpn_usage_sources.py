"""Device-scoped source pseudonyms; raw addresses never reach persistence."""

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass, replace

from app.services.vpn_node_usage import UsageReceipt


def _invalid_source() -> None:
    error = ValueError("vpn_usage_source_invalid")
    try:
        raise error from None
    finally:
        error.__context__ = None


def source_hash(secret: str, device_id: int, raw: str) -> str:
    try:
        if (
            type(device_id) is not int
            or device_id <= 0
            or type(raw) is not str
            or "%" in raw
        ):
            _invalid_source()
        value = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
        address = ipaddress.ip_address(value)
        if value != raw and not isinstance(address, ipaddress.IPv6Address):
            _invalid_source()
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            address = address.ipv4_mapped
        normalized = (
            f"6:{ipaddress.ip_network((address, 64), strict=False)}"
            if isinstance(address, ipaddress.IPv6Address)
            else f"4:{address}"
        )
        message = f"vpn-abuse-network-source-v1\0{device_id}\0{normalized}".encode(
            "ascii"
        )
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    except (ValueError, TypeError, UnicodeError):
        _invalid_source()


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    state: str
    key_epoch: str | None = None
    source_hashes: tuple[str, ...] | None = None


def prepare_source_samples(
    receipt: UsageReceipt, device_ids: dict[int, int | None], secret: str | None
) -> tuple[UsageReceipt, dict[int, SourceEvidence]]:
    try:
        secret_valid = (
            isinstance(secret, str)
            and len(secret) >= 32
            and secret != "change-me-session-secret"
        )
        encoded = secret.encode("utf-8", "strict") if secret_valid else None
    except UnicodeError:
        encoded = None
    epoch = (
        hmac.new(
            encoded, b"vpn-abuse-network-source-key-v1", hashlib.sha256
        ).hexdigest()[:16]
        if encoded is not None
        else None
    )
    samples = {}
    cleaned = []
    for key in receipt.keys:
        evidence = SourceEvidence(key.source_state)
        if key.source_state == "ok":
            device_id = device_ids.get(key.access_key_id)
            if encoded is None or type(device_id) is not int or device_id <= 0:
                evidence = SourceEvidence("unavailable")
            else:
                try:
                    evidence = SourceEvidence(
                        "ok",
                        epoch,
                        tuple(
                            sorted(
                                {
                                    source_hash(secret, device_id, raw)
                                    for raw in key.sources
                                }
                            )
                        ),
                    )
                except ValueError:
                    evidence = SourceEvidence("unavailable")
        samples[key.access_key_id] = evidence
        cleaned.append(replace(key, source_state=evidence.state, sources=()))
    return replace(receipt, keys=tuple(cleaned)), samples
