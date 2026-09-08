"""Regression tests for the M5 WebSocket decoder responsibility split."""

import base64
import json

from xianyu_agent.protocol import client, parser
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import decoder


def test_decode_dict_frame() -> None:
    decoded = decoder.decode_frame('{"code":200,"headers":{"mid":"m-1"}}')
    assert decoded is not None
    assert decoded.payload == {"code": 200, "headers": {"mid": "m-1"}}
    assert decoded.frame.code == 200
    assert decoded.frame.headers == {"mid": "m-1"}


def test_decode_non_dict_json_preserves_raw_body() -> None:
    decoded = decoder.decode_frame('["a",1]')
    assert decoded is not None
    assert decoded.payload == ["a", 1]
    assert decoded.frame.body == '["a",1]'


def test_decode_binary_uses_replace_policy() -> None:
    decoded = decoder.decode_frame(b'{"body":"\xff"}')
    assert decoded is not None
    assert decoded.raw_text == '{"body":"\ufffd"}'
    assert decoded.frame.body == "\ufffd"


def test_decode_non_json_returns_none() -> None:
    assert decoder.decode_frame("not-json") is None
    assert decoder.decode_frame(b"not-json") is None


def test_unpack_sync_payloads_preserves_existing_json_policy() -> None:
    payload = {"1": {"10": {"reminderContent": "hello"}}}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    frame = WsFrame(body={"syncPushPackage": {"data": [{"data": encoded}]}})

    assert decoder.unpack_sync_payloads(frame) == [payload]


def test_unpack_sync_payloads_skips_invalid_entries() -> None:
    frame = WsFrame(
        body={
            "syncPushPackage": {
                "data": [
                    None,
                    {},
                    {"data": 123},
                    {"data": "not-base64"},
                ]
            }
        }
    )

    assert decoder.unpack_sync_payloads(frame) == []


def test_parser_sync_unpack_preserves_legacy_decoder_hooks(monkeypatch) -> None:
    monkeypatch.setattr(
        parser,
        "_decode_body",
        lambda body: {"syncPushPackage": {"data": [{"data": f"encoded:{body}"}]}},
    )
    monkeypatch.setattr(parser, "_decode_sync_data", lambda value: {"decoded": value})

    assert parser.unpack_sync_payloads(WsFrame(body="legacy-body")) == [
        {"decoded": "encoded:legacy-body"}
    ]
    assert parser.ws_decoder is decoder


def test_ws_client_uses_canonical_decoder_module() -> None:
    assert client.ws_decoder is decoder
