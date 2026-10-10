"""外部发卡 API 调用(Issue #5 Phase 6, §18/M20/M22)。

执行链固定为:出站守卫 → SecretVault 取值 → 渲染模板 → 发请求 → 结果分类。

分类标准是"远端有没有可能已经产生副作用":
- 出站被拦截 / 取密钥失败 / 建连失败:明确没发出去,可以重试;
- 4xx:对方明确拒绝,没发卡;
- 2xx 但没有可发送内容:远端可能已经扣了一次卡,按结果不明处理;
- 5xx / 读超时 / 请求中断:远端可能已经发卡,同样按结果不明处理。

错误信息里只出现状态码与异常类型:请求头、请求体、密钥都不会进入错误串。
"""

from __future__ import annotations

import json
import logging
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from xianyu_agent.application.ports.secret_vault import SecretRef, SecretVault
from xianyu_agent.domain.delivery.api_config import (
    ApiConfigError,
    ApiDeliveryConfig,
    extract_content,
    render_body,
    render_headers,
)
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.infrastructure.security.outbound_guard import check_url

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 256 * 1024


class _ResponseTooLarge(ValueError):
    """原始正文或某一解压层超过接收预算。"""


class _InvalidResponseEncoding(ValueError):
    """无法在接收预算内完整解码响应。"""


def _inflate(payload: bytes, *, wbits: int) -> bytes:
    """限制单层输出;flush 的 length 不是上限,因此只接受完整流。"""
    decoder = zlib.decompressobj(wbits)
    output = decoder.decompress(payload, MAX_RESPONSE_BYTES + 1)
    if len(output) > MAX_RESPONSE_BYTES:
        raise _ResponseTooLarge
    if not decoder.eof or decoder.unused_data:
        raise _InvalidResponseEncoding
    return output


def _inflate_gzip(payload: bytes) -> bytes:
    """gzip 可包含多个 member,所有 member 共用同一输出预算。"""
    output = bytearray()
    while payload:
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
        member = decoder.decompress(payload, MAX_RESPONSE_BYTES - len(output) + 1)
        if len(output) + len(member) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge
        if not decoder.eof:
            raise _InvalidResponseEncoding
        output.extend(member)
        # gzip 文件末尾允许零填充;不能忽略后续 member 的解压量。
        payload = decoder.unused_data.lstrip(b"\x00")
    return bytes(output)


def _decode_response(payload: bytes, response: httpx.Response) -> bytes:
    encodings = response.headers.get_list("content-encoding", split_commas=True)
    for value in reversed(encodings):
        encoding = value.strip().lower()
        if encoding in {"", "identity"}:
            continue
        if encoding == "gzip":
            payload = _inflate_gzip(payload)
        elif encoding == "deflate":
            try:
                payload = _inflate(payload, wbits=zlib.MAX_WBITS)
            except zlib.error:
                # 保持 HTTPX 对 zlib 封装与原始 deflate 两种格式的兼容。
                payload = _inflate(payload, wbits=-zlib.MAX_WBITS)
        else:
            # 项目依赖未安装 br/zstd;不能退回 HTTPX 无界解压。
            raise _InvalidResponseEncoding
    return payload


async def _read_response(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            declared_length = int(length)
        except ValueError:
            declared_length = 0
        if declared_length > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge
    if response.is_stream_consumed:
        # 注入的测试 transport 可直接返回已缓冲/解码的 Response。
        payload = response.content
        if len(payload) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge
        return payload
    payload = bytearray()
    # aiter_bytes 会先无界解压再分块;在 raw 边界计数并自行有界解压。
    async for chunk in response.aiter_raw():
        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge
        payload.extend(chunk)
    return _decode_response(bytes(payload), response)


def _stringify_query(body: Mapping[str, object]) -> dict[str, str]:
    """Query strings 只能是字符串:数字与布尔值按 JSON 习惯渲染。"""
    rendered: dict[str, str] = {}
    for key, value in body.items():
        if isinstance(value, bool):
            rendered[str(key)] = "true" if value else "false"
        elif value is None:
            rendered[str(key)] = ""
        elif isinstance(value, str):
            rendered[str(key)] = value
        else:
            rendered[str(key)] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return rendered


@dataclass(frozen=True)
class ApiFetchResult:
    """What one API delivery call produced, and whether the remote may have acted."""

    content: str | None = None
    dispatched: bool = False
    error: str | None = None
    status_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.content is not None


class ApiDeliveryProvider:
    """Call one external card-issuing API and classify the outcome."""

    def __init__(
        self,
        *,
        vault: SecretVault | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_hosts: tuple[str, ...] = (),
        resolve_dns: bool = True,
    ) -> None:
        self._vault = vault
        self._transport = transport
        self._allow_hosts = allow_hosts
        self._resolve_dns = resolve_dns

    @classmethod
    def from_environment(cls) -> ApiDeliveryProvider:
        """Build the production provider with the encrypted SQL SecretVault."""
        return cls(vault=SqlAlchemySecretVault(FernetKeyring.from_environment()))

    @classmethod
    def for_config(cls, config: ApiDeliveryConfig) -> ApiDeliveryProvider:
        """Pick the provider a config needs: 只有引用密钥时才装配机密库。"""
        return cls.from_environment() if config.secret_refs else cls()

    async def fetch(
        self, config: ApiDeliveryConfig, variables: Mapping[str, str]
    ) -> ApiFetchResult:
        if not config.enabled:
            return ApiFetchResult(error=f"API 配置 {config.name} 已停用")
        check = check_url(config.url, allow_hosts=self._allow_hosts, resolve_dns=self._resolve_dns)
        if not check.allowed:
            logger.warning("api delivery blocked by outbound guard config=%s", config.name)
            return ApiFetchResult(error=f"出站被拦截: {check.reason}")
        try:
            secrets = await self._resolve_secrets(config)
            headers = render_headers(config, variables, secrets)
            body = render_body(config, variables, secrets)
        except (ApiConfigError, KeyError) as exc:
            return ApiFetchResult(error=f"配置或密钥不可用({type(exc).__name__})")
        except Exception as exc:
            logger.warning("api delivery secret resolve failed config=%s", config.name)
            return ApiFetchResult(error=f"密钥解析失败({type(exc).__name__})")
        return await self._request(config, headers, body)

    async def _resolve_secrets(self, config: ApiDeliveryConfig) -> dict[str, str]:
        if not config.secret_refs:
            return {}
        if self._vault is None:
            msg = "未配置机密库"
            raise RuntimeError(msg)
        resolved: dict[str, str] = {}
        for ref in config.secret_refs:
            value = await self._vault.resolve(SecretRef(ref))
            resolved[ref] = value.reveal()
        return resolved

    async def _request(  # noqa: PLR0911 - 结果分类逐条判定,提前返回最清楚
        self, config: ApiDeliveryConfig, headers: Mapping[str, str], body: Mapping[str, object]
    ) -> ApiFetchResult:
        timeout = httpx.Timeout(config.timeout_seconds)
        status: int | None = None
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    config.method,
                    config.url,
                    headers=dict(headers),
                    **(
                        {"params": _stringify_query(body)}
                        if config.body_is_query
                        else {"json": dict(body)}
                    ),
                ) as response,
            ):
                status = response.status_code
                # 这些正文没有业务消费者,只分类响应头并立即关闭流。
                if 300 <= status < 400:
                    return self._handle_redirect(config, response, status)
                if 400 <= status < 500:
                    return ApiFetchResult(
                        status_code=status, error=f"对方拒绝请求(HTTP {status}),未发卡"
                    )
                if status >= 500:
                    return ApiFetchResult(
                        status_code=status,
                        dispatched=True,
                        error=f"对方服务异常(HTTP {status}),可能已经发卡",
                    )
                response_body = await _read_response(response)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.InvalidURL) as exc:
            if status is not None:
                return ApiFetchResult(
                    status_code=status,
                    dispatched=True,
                    error=f"请求中断,结果不明({type(exc).__name__})",
                )
            return ApiFetchResult(error=f"建连失败,未发出({type(exc).__name__})")
        except httpx.HTTPError as exc:
            return ApiFetchResult(
                status_code=status,
                dispatched=True,
                error=f"请求中断,结果不明({type(exc).__name__})",
            )
        except _ResponseTooLarge:
            return ApiFetchResult(
                status_code=status,
                dispatched=True,
                error="响应超过256KiB上限,无法确认发卡结果",
            )
        except (_InvalidResponseEncoding, zlib.error):
            return ApiFetchResult(
                status_code=status, dispatched=True, error="响应编码不可用,无法确认发卡结果"
            )
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return ApiFetchResult(
                status_code=status, dispatched=True, error="响应不是合法 JSON,无法确认发卡结果"
            )
        content = extract_content(payload, config.response_path)
        if content is None:
            return ApiFetchResult(
                status_code=status,
                dispatched=True,
                error=f"响应里没有可发送的内容(路径 {config.response_path})",
            )
        return ApiFetchResult(content=content, dispatched=True, status_code=status)

    def _handle_redirect(
        self, config: ApiDeliveryConfig, response: httpx.Response, status: int
    ) -> ApiFetchResult:
        """重定向后的地址必须重新过一遍出站检查(M54)。"""
        location = response.headers.get("location", "")
        if not location:
            return ApiFetchResult(
                status_code=status,
                dispatched=True,
                error=f"对方返回重定向但没给地址(HTTP {status})",
            )
        target = str(httpx.URL(config.url).join(location))
        check = check_url(target, allow_hosts=self._allow_hosts, resolve_dns=self._resolve_dns)
        if not check.allowed:
            replayed = status in {307, 308}
            return ApiFetchResult(
                status_code=status,
                dispatched=replayed,
                error=f"重定向目标被拦截({check.reason})",
            )
        # 允许的目标也不自动跟随:发卡请求不能因为一次重定向被发两遍,交给运营确认。
        return ApiFetchResult(
            status_code=status,
            dispatched=status in {307, 308},
            error=f"对方要求重定向到 {target},已停下等待人工确认",
        )
