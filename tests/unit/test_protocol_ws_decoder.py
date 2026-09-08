"""Regression tests for the first M5 WebSocket client responsibility split."""

from xianyu_agent.protocol import client
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


def test_ws_client_uses_canonical_decoder_module() -> None:
    assert client.ws_decoder is decoder
