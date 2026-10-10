"""外部发卡 API 的配置模型与模板渲染(Issue #5 Phase 6, §18/M21/M22)。

配置里只有结构与 secret_ref:密钥值放在 SecretVault 里,通过模板里的 {{secret:引用名}} 占位。
业务变量用 {{order_id}} 这类白名单占位符,渲染是一次性字符串替换,不执行任何表达式。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

# GET 的发卡接口同样常见(参考项目 super-butler 支持 GET 与 POST),因此 body 字段按方法
# 解释:GET 作为查询参数,POST / PUT 作为 JSON 请求体。
ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT")
BUSINESS_VARIABLES: tuple[str, ...] = (
    "order_id",
    "buyer_name",
    "item_title",
    "sku_name",
    "quantity",
)
SECRET_PREFIX = "secret:"
MIN_TIMEOUT_SECONDS = 0.5
MAX_TIMEOUT_SECONDS = 60.0

_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")
_RESPONSE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ApiConfigError(ValueError):
    """发货 API 配置不合法。"""


def _placeholders(text: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in _PLACEHOLDER.finditer(text))


@dataclass(frozen=True)
class ApiDeliveryConfig:
    """One external card-issuing API definition (structure only, no secrets)."""

    name: str
    url: str
    method: str = "POST"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = "{}"
    response_path: str = "card"
    timeout_seconds: float = 10.0
    enabled: bool = True
    description: str | None = None
    local_id: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "API 配置必须有名称"
            raise ApiConfigError(msg)
        parts = urlsplit(self.url.strip())
        if parts.scheme != "https":
            msg = "发货 API 只允许 https 地址"
            raise ApiConfigError(msg)
        if not parts.hostname:
            msg = "发货 API 地址缺少主机名"
            raise ApiConfigError(msg)
        method = self.method.strip().upper()
        if method not in ALLOWED_METHODS:
            msg = f"只允许 {'/'.join(ALLOWED_METHODS)} 方法,当前是 {method or '空'}"
            raise ApiConfigError(msg)
        object.__setattr__(self, "method", method)
        if not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            msg = f"超时必须在 {MIN_TIMEOUT_SECONDS} 到 {MAX_TIMEOUT_SECONDS} 秒之间"
            raise ApiConfigError(msg)
        if not _RESPONSE_PATH.fullmatch(self.response_path.strip()):
            msg = f"响应取值路径只能是点分字段名: {self.response_path!r}"
            raise ApiConfigError(msg)
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "response_path", self.response_path.strip())
        headers = {str(key).strip(): str(value) for key, value in self.headers.items()}
        if any(not key for key in headers):
            msg = "请求头名称不能为空"
            raise ApiConfigError(msg)
        object.__setattr__(self, "headers", MappingProxyType(headers))
        self._validate_placeholders()
        self._validate_body_json()

    def _validate_placeholders(self) -> None:
        seen: list[str] = []
        for text in self._template_texts():
            for name in _placeholders(text):
                if name.startswith(SECRET_PREFIX):
                    ref = name[len(SECRET_PREFIX) :].strip()
                    if not ref:
                        msg = "secret 占位符必须带引用名,例如 {{secret:card_api_key}}"
                        raise ApiConfigError(msg)
                    continue
                if name not in BUSINESS_VARIABLES:
                    msg = (
                        f"模板变量 {name!r} 不在白名单内,只允许 "
                        f"{'、'.join(BUSINESS_VARIABLES)} 或以 secret: 开头的引用"
                    )
                    raise ApiConfigError(msg)
                seen.append(name)

    def _validate_body_json(self) -> None:
        probe = _substitute(
            self.body,
            dict.fromkeys(BUSINESS_VARIABLES, "x"),
            dict.fromkeys(self.secret_refs, "x"),
        )
        try:
            parsed = json.loads(probe)
        except json.JSONDecodeError as exc:
            msg = f"请求体必须是合法 JSON(占位符替换后): {exc.msg}"
            raise ApiConfigError(msg) from exc
        if not isinstance(parsed, dict):
            msg = "请求体必须是 JSON 对象"
            raise ApiConfigError(msg)

    @property
    def body_is_query(self) -> bool:
        """GET 把同一个 JSON 对象当查询参数,其余方法当 JSON 请求体。"""
        return self.method == "GET"

    def _template_texts(self) -> tuple[str, ...]:
        return (self.body, *self.headers.keys(), *self.headers.values())

    @property
    def secret_refs(self) -> tuple[str, ...]:
        """Secret references this config needs, in order of first appearance."""
        refs: list[str] = []
        for text in self._template_texts():
            for name in _placeholders(text):
                if not name.startswith(SECRET_PREFIX):
                    continue
                ref = name[len(SECRET_PREFIX) :].strip()
                if ref not in refs:
                    refs.append(ref)
        return tuple(refs)

    @property
    def variables(self) -> tuple[str, ...]:
        """Business variables this config uses, in order of first appearance."""
        used: list[str] = []
        for text in self._template_texts():
            for name in _placeholders(text):
                if name in BUSINESS_VARIABLES and name not in used:
                    used.append(name)
        return tuple(used)

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"{self.name}(#{self.local_id or '-'} {self.method} {self.url} {state})"


def _substitute(template: str, variables: Mapping[str, str], secrets: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name.startswith(SECRET_PREFIX):
            ref = name[len(SECRET_PREFIX) :].strip()
            if ref not in secrets:
                msg = f"缺少密钥引用 {ref}"
                raise ApiConfigError(msg)
            return secrets[ref]
        return str(variables.get(name, ""))

    return _PLACEHOLDER.sub(replace, template)


def render_headers(
    config: ApiDeliveryConfig, variables: Mapping[str, str], secrets: Mapping[str, str]
) -> dict[str, str]:
    return {
        _substitute(key, variables, secrets): _substitute(value, variables, secrets)
        for key, value in config.headers.items()
    }


def render_body(
    config: ApiDeliveryConfig, variables: Mapping[str, str], secrets: Mapping[str, str]
) -> dict[str, object]:
    text = _substitute(config.body, variables, secrets)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"渲染后的请求体不是合法 JSON: {exc.msg}"
        raise ApiConfigError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "渲染后的请求体必须是 JSON 对象"
        raise ApiConfigError(msg)
    return parsed


def extract_content(payload: object, path: str) -> str | None:
    """Read the card text out of a JSON response along a dotted path."""
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    if current is None:
        return None
    text = str(current).strip()
    return text or None
