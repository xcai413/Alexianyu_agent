"""Regression tests for the M5 WebSocket payload normalizer split."""

import base64
import json

import msgpack

from xianyu_agent.protocol import parser
from xianyu_agent.protocol.ws import normalizer


def test_decode_body_preserves_json_and_base64_json_contract() -> None:
    payload = {"bizType": "text", "1": "hello"}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    assert normalizer.decode_body(payload) is payload
    assert normalizer.decode_body(json.dumps(payload)) == payload
    assert normalizer.decode_body(encoded) == payload
    assert normalizer.decode_body("not-json") is None


def test_decode_sync_data_preserves_json_and_msgpack_contract() -> None:
    payload = {"1": {"2": "chat@goofish"}}
    json_value = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    msgpack_value = base64.b64encode(msgpack.packb(payload, use_bin_type=True)).decode("ascii")

    assert normalizer.decode_sync_data(json_value) == payload
    assert normalizer.decode_sync_data(msgpack_value) == payload
    assert normalizer.decode_sync_data("not-base64") is None


def test_parser_keeps_normalizer_compatibility_aliases() -> None:
    assert parser.ws_normalizer is normalizer
    assert parser._decode_body is normalizer.decode_body
    assert parser._decode_sync_data is normalizer.decode_sync_data
