"""出站平台回执契约：身份分层、时间窗和 fail-closed 边界。"""

import hashlib

import pytest

from agent.platform_receipts import (
    PlatformReceiptError,
    build_outgoing_receipt_event_key,
    validate_platform_receipt,
)


def _receipt(**overrides):
    adapter = overrides.pop("adapter", "napcat-ws")
    request_id = overrides.pop("transport_request_id", "req-1")
    value = {
        "event_key": build_outgoing_receipt_event_key(
            adapter=adapter, transport_request_id=request_id,
        ),
        "source": "onebot_ws_api",
        "adapter": adapter,
        "transport_request_id": request_id,
        "outbox_id": "act-1",
        "self_id": "bot-1",
        "channel": "group",
        "target_id": "123",
        "payload_sha256": hashlib.sha256(b"hello").hexdigest(),
        "message_ids": [42, -7],
        "observed_at": 1000.0,
        "synthetic": False,
        "schema_version": 1,
    }
    value.update(overrides)
    return value


def test_valid_receipt_has_separate_outgoing_key_and_evidence():
    receipt = validate_platform_receipt(
        _receipt(), expected_self_id="bot-1", now=1001.0,
    )
    assert receipt.event_key.startswith("out:v1:")
    assert not receipt.event_key.startswith("v2:")
    assert receipt.message_ids == (42, -7)
    assert receipt.to_evidence()["transport_request_id"] == "req-1"


@pytest.mark.parametrize("bad_ids", ([0], [True], [2**31], ["42"], []))
def test_invalid_message_ids_fail_closed(bad_ids):
    with pytest.raises(PlatformReceiptError, match="message_ids"):
        validate_platform_receipt(
            _receipt(message_ids=bad_ids), expected_self_id="bot-1", now=1001.0,
        )


@pytest.mark.parametrize("bad_ids", ([42, 42], [42, "bad"], [-7, 0]))
def test_mixed_or_duplicate_message_ids_fail_closed(bad_ids):
    with pytest.raises(PlatformReceiptError, match="message_ids"):
        validate_platform_receipt(
            _receipt(message_ids=bad_ids), expected_self_id="bot-1", now=1001.0,
        )


def test_event_key_must_be_derived_from_persisted_transport_request():
    with pytest.raises(PlatformReceiptError, match="event_key"):
        validate_platform_receipt(
            _receipt(event_key="v2:group:123:1:bot:42"),
            expected_self_id="bot-1", now=1001.0,
        )

    with pytest.raises(PlatformReceiptError, match="transport_request_id"):
        validate_platform_receipt(
            _receipt(transport_request_id="", event_key=""),
            expected_self_id="bot-1", now=1001.0,
        )


def test_context_mismatch_stale_and_synthetic_are_rejected():
    with pytest.raises(PlatformReceiptError, match="self_id"):
        validate_platform_receipt(
            _receipt(), expected_self_id="other", now=1001.0,
        )
    with pytest.raises(PlatformReceiptError, match="time window"):
        validate_platform_receipt(
            _receipt(), expected_self_id="bot-1", now=1401.0,
        )
    with pytest.raises(PlatformReceiptError, match="synthetic"):
        validate_platform_receipt(
            _receipt(source="onebot_message_sent", synthetic=True),
            expected_self_id="bot-1", now=1001.0,
        )
    receipt = validate_platform_receipt(
        _receipt(source="onebot_message_sent", synthetic=True),
        expected_self_id="bot-1", now=1001.0, allow_synthetic=True,
    )
    assert receipt.synthetic is True


def test_unknown_fields_and_payload_hash_are_rejected():
    with pytest.raises(PlatformReceiptError, match="unknown"):
        validate_platform_receipt(
            _receipt(untrusted_outbox_id="act-2"),
            expected_self_id="bot-1", now=1001.0,
        )
    with pytest.raises(PlatformReceiptError, match="payload_sha256"):
        validate_platform_receipt(
            _receipt(payload_sha256="not-a-hash"),
            expected_self_id="bot-1", now=1001.0,
        )
    with pytest.raises(PlatformReceiptError, match="payload_sha256"):
        validate_platform_receipt(
            _receipt(payload_sha256=hashlib.sha256(b"hello").hexdigest().upper()),
            expected_self_id="bot-1", now=1001.0,
        )
