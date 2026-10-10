"""外部发卡响应必须在读取和解压边界有界,不能截断成成功。"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest

from xianyu_agent.domain.delivery import ApiDeliveryConfig
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider
from xianyu_agent.infrastructure.delivery.api_provider import MAX_RESPONSE_BYTES

JSON_CARD = b'{"card":"CODE-1"}'
BLOCK_SIZE = 64 * 1024


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, *, read_error: bool = False) -> None:
        self.chunks = tuple(
            payload[index : index + BLOCK_SIZE] for index in range(0, len(payload), BLOCK_SIZE)
        )
        self.consumed = 0
        self.closed = False
        self.read_error = read_error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk
        if self.read_error:
            raise httpx.ReadError("synthetic-private-response")

    async def aclose(self) -> None:
        self.closed = True


async def _fetch(
    stream: _TrackedStream,
    *,
    headers: list[tuple[str, str]] | None = None,
    status: int = 200,
):
    provider = ApiDeliveryProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, headers=headers, stream=stream)
        ),
        resolve_dns=False,
    )
    return await provider.fetch(
        ApiDeliveryConfig(name="响应验收", url="https://cards.example.com/dispatch"), {}
    )


@pytest.mark.parametrize("length", [None, "1", "invalid"])
async def test_oversize_stream_stops_reading_and_never_accepts_json_prefix(
    length: str | None,
) -> None:
    stream = _TrackedStream(JSON_CARD + b" " * (MAX_RESPONSE_BYTES * 4))
    headers = [] if length is None else [("content-length", length)]

    result = await _fetch(stream, headers=headers)

    assert result.content is None
    assert result.dispatched is True
    assert result.status_code == 200
    assert "超过" in (result.error or "")
    assert stream.consumed == MAX_RESPONSE_BYTES // BLOCK_SIZE + 1
    assert stream.consumed < len(stream.chunks)
    assert stream.closed


async def test_large_declared_length_is_rejected_before_body_read() -> None:
    stream = _TrackedStream(JSON_CARD)
    result = await _fetch(stream, headers=[("content-length", str(MAX_RESPONSE_BYTES + 1))])

    assert result.content is None
    assert result.dispatched is True
    assert result.status_code == 200
    assert stream.consumed == 0
    assert stream.closed


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("buffered", [False, True])
async def test_exact_limit_and_limit_plus_one(extra: int, buffered: bool) -> None:
    payload = JSON_CARD + b" " * (MAX_RESPONSE_BYTES - len(JSON_CARD) + extra)
    stream = _TrackedStream(payload)
    provider = ApiDeliveryProvider(
        transport=httpx.MockTransport(
            lambda _request: (
                httpx.Response(200, content=payload)
                if buffered
                else httpx.Response(200, stream=stream)
            )
        ),
        resolve_dns=False,
    )
    result = await provider.fetch(
        ApiDeliveryConfig(name="响应验收", url="https://cards.example.com/dispatch"), {}
    )

    assert result.content == (None if extra else "CODE-1")
    assert result.dispatched is True
    assert result.status_code == 200
    if not buffered:
        assert stream.closed


def _compress(payload: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        return gzip.compress(payload)
    if encoding == "raw-deflate":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return compressor.compress(payload) + compressor.flush()
    return zlib.compress(payload)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("size", [len(JSON_CARD), MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES + 1])
async def test_compressed_responses_have_a_decoded_limit(encoding: str, size: int) -> None:
    payload = JSON_CARD + b" " * (size - len(JSON_CARD))
    stream = _TrackedStream(_compress(payload, encoding))
    result = await _fetch(stream, headers=[("content-encoding", encoding.removeprefix("raw-"))])

    assert result.content == (None if size > MAX_RESPONSE_BYTES else "CODE-1")
    assert result.dispatched is True
    assert result.status_code == 200
    assert stream.closed


@pytest.mark.parametrize("repeated_headers", [False, True])
@pytest.mark.parametrize("oversize", [False, True])
async def test_layered_case_insensitive_and_repeated_encoding_headers(
    repeated_headers: bool,
    oversize: bool,
) -> None:
    payload = JSON_CARD + b" " * (MAX_RESPONSE_BYTES if oversize else 0)
    stream = _TrackedStream(gzip.compress(zlib.compress(payload)))
    headers = (
        [("content-encoding", "DeFlAtE"), ("content-encoding", "GZiP")]
        if repeated_headers
        else [("content-encoding", " DeFlAtE, GZiP ")]
    )
    result = await _fetch(stream, headers=headers)

    assert result.content == (None if oversize else "CODE-1")
    assert result.dispatched is True
    assert stream.closed


async def test_intermediate_decoding_layer_is_also_bounded() -> None:
    # 第一层解压超过上限,最终 JSON 仍很小;不能先构造超限中间副本。
    inner = gzip.compress(JSON_CARD) + b"\x00" * MAX_RESPONSE_BYTES
    stream = _TrackedStream(zlib.compress(inner))
    result = await _fetch(stream, headers=[("content-encoding", "gzip, deflate")])

    assert result.content is None
    assert result.dispatched is True
    assert "超过" in (result.error or "")
    assert stream.closed


@pytest.mark.parametrize("encoding", ["", "gzip,", ", gzip", "gzip,,identity"])
@pytest.mark.parametrize("oversize", [False, True])
async def test_empty_encoding_list_elements_preserve_httpx_compatibility(
    encoding: str, oversize: bool
) -> None:
    payload = JSON_CARD + b" " * (MAX_RESPONSE_BYTES if oversize else 0)
    if "gzip" in encoding:
        payload = gzip.compress(payload)
    stream = _TrackedStream(payload)
    result = await _fetch(stream, headers=[("content-encoding", encoding)])

    assert result.content == (None if oversize else "CODE-1")
    assert result.dispatched is True
    assert stream.closed


@pytest.mark.parametrize("oversize", [False, True])
async def test_all_gzip_members_count_toward_the_limit(oversize: bool) -> None:
    payload = (
        gzip.compress(JSON_CARD) + gzip.compress(b" " * MAX_RESPONSE_BYTES)
        if oversize
        else gzip.compress(JSON_CARD[:8]) + gzip.compress(JSON_CARD[8:])
    )
    stream = _TrackedStream(payload)
    result = await _fetch(stream, headers=[("content-encoding", "gzip")])

    assert result.content == (None if oversize else "CODE-1")
    assert result.dispatched is True
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_truncated_compression_is_uncertain(encoding: str) -> None:
    stream = _TrackedStream(_compress(JSON_CARD, encoding)[:-4])
    result = await _fetch(stream, headers=[("content-encoding", encoding)])

    assert result.content is None
    assert result.dispatched is True
    assert result.status_code == 200
    assert stream.closed


async def test_unsupported_encoding_fails_without_automatic_decoding() -> None:
    stream = _TrackedStream(JSON_CARD)
    result = await _fetch(stream, headers=[("content-encoding", "unknown-codec")])

    assert result.content is None
    assert result.dispatched is True
    assert stream.closed


@pytest.mark.parametrize(
    ("status", "dispatched"), [(302, False), (307, True), (308, True), (400, False), (503, True)]
)
async def test_unused_non_success_bodies_are_closed_without_reading(
    status: int,
    dispatched: bool,
) -> None:
    stream = _TrackedStream(b"x" * MAX_RESPONSE_BYTES * 2)
    result = await _fetch(
        stream, status=status, headers=[("location", "https://cards.example.com/other")]
    )

    assert result.content is None
    assert result.status_code == status
    assert result.dispatched is dispatched
    assert stream.consumed == 0
    assert stream.closed


async def test_partial_read_failure_is_uncertain_and_does_not_echo_response() -> None:
    stream = _TrackedStream(JSON_CARD, read_error=True)
    result = await _fetch(stream)

    assert result.content is None
    assert result.dispatched is True
    assert result.status_code == 200
    assert "synthetic-private-response" not in (result.error or "")
    assert stream.closed
