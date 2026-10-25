from dataclasses import replace

import pytest

from app.services.vpn_node_usage import KeyUsage, UsageReceipt
from app.services.vpn_usage_sources import prepare_source_samples, source_hash

SECRET = "synthetic-persistent-source-key-32-characters"


def test_sources_are_device_scoped_and_ipv6_is_grouped_by_network():
    first = source_hash(SECRET, 1, "192.0.2.1")
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")
    assert first == source_hash(SECRET, 1, "::ffff:192.0.2.1")
    assert first == source_hash(SECRET, 1, "[::ffff:c000:201]")
    assert first != source_hash(SECRET, 2, "192.0.2.1")
    assert first != source_hash(SECRET + "rotated", 1, "192.0.2.1")
    assert source_hash(SECRET, 1, "[2001:db8:1:2::1]") == source_hash(
        SECRET, 1, "2001:db8:1:2:ffff::2"
    )
    assert source_hash(SECRET, 1, "2001:db8:1:2::1") != source_hash(
        SECRET, 1, "2001:db8:1:3::1"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "192.0.2.1:443",
        "[192.0.2.1]",
        "[2001:db8::1]:443",
        "fe80::1%en0",
        "[fe80::1%en0]",
        "not-an-ip",
        "",
        " 192.0.2.1",
        None,
        7,
    ],
)
def test_invalid_source_does_not_leak_into_error(raw):
    with pytest.raises(ValueError, match="^vpn_usage_source_invalid$") as caught:
        source_hash(SECRET, 1, raw)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def receipt():
    return UsageReceipt(
        1,
        "11111111-2222-4333-8444-555555555555",
        60_000,
        60_010,
        "a" * 64,
        (
            KeyUsage(1, "ok", 1, 0, 10, 20, "ok", ("192.0.2.1", "::ffff:192.0.2.1")),
            KeyUsage(2, "ok", 2, 0, 10, 20, "ok", ()),
            KeyUsage(3, "ok", 3, 0, 10, 20, "unsupported", ()),
            KeyUsage(4, "ok", 4, 0, 10, 20, "unavailable", ()),
        ),
    )


def test_preparation_discards_raw_sources_and_preserves_measured_empty():
    cleaned, samples = prepare_source_samples(
        receipt(), {1: 8, 2: 8, 3: 8, 4: 8}, SECRET
    )
    assert all(key.sources == () for key in cleaned.keys)
    assert samples[1].state == "ok" and len(samples[1].source_hashes) == 1
    assert samples[1].source_hashes == (source_hash(SECRET, 8, "192.0.2.1"),)
    assert samples[2].state == "ok" and samples[2].source_hashes == ()
    assert samples[1].key_epoch == samples[2].key_epoch
    assert len(samples[1].key_epoch) == 16
    assert samples[3].state == "unsupported" and samples[3].source_hashes is None
    assert samples[4].state == "unavailable" and samples[4].source_hashes is None
    assert "192.0.2.1" not in repr(samples) + repr(cleaned)
    assert SECRET not in repr(samples) + repr(cleaned)


@pytest.mark.parametrize(
    "secret", [None, "", "change-me-session-secret", "x" * 31, "\ud800" * 32]
)
def test_invalid_persistent_secret_disables_only_source_evidence(secret):
    cleaned, samples = prepare_source_samples(receipt(), {1: 8, 2: 8, 3: 8}, secret)
    assert samples[1].state == samples[2].state == "unavailable"
    assert samples[1].source_hashes is samples[1].key_epoch is None
    assert samples[3].state == "unsupported"
    assert cleaned.keys[0].traffic_state == "ok" and cleaned.keys[0].upload_bytes == 10


def test_unbound_key_cannot_create_cross_customer_source_identity():
    _, samples = prepare_source_samples(receipt(), {2: None, 3: 8}, SECRET)
    assert samples[1].state == samples[2].state == "unavailable"
    assert samples[1].source_hashes is None


def test_rotation_changes_epoch_and_invalid_source_is_unavailable_not_empty():
    original = receipt()
    _, first = prepare_source_samples(original, {1: 8}, SECRET)
    _, rotated = prepare_source_samples(original, {1: 8}, SECRET + "rotated")
    assert first[1].key_epoch != rotated[1].key_epoch
    malformed = replace(
        original, keys=(replace(original.keys[0], sources=("private-invalid",)),)
    )
    cleaned, samples = prepare_source_samples(malformed, {1: 8}, SECRET)
    assert samples[1].state == "unavailable" and samples[1].source_hashes is None
    assert cleaned.keys[0].sources == ()
