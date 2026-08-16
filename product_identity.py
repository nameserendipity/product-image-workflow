from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import parse_qs, urlparse

from platform_urls import TAOBAO_SHORT_HOSTS, is_taobao_host, is_tmall_host


class ProductIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    platform: Literal["taobao", "tmall"]
    product_id: str
    product_key: str
    source_url: str
    canonical_url: str


RedirectResolver = Callable[[str, float], str]


class ProductIdentityResolver:
    def __init__(self, redirect_resolver: RedirectResolver | None = None) -> None:
        self.redirect_resolver = redirect_resolver

    def resolve(self, value: str, timeout: float = 15.0) -> ProductIdentity | None:
        source_url = value.strip()
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return None

        was_short_link = host in TAOBAO_SHORT_HOSTS
        if was_short_link:
            if self.redirect_resolver is None:
                raise ProductIdentityError("淘宝短链接暂时无法建立共享商品标识")
            try:
                resolved_url = self.redirect_resolver(source_url, timeout).strip()
            except Exception as error:
                raise ProductIdentityError("淘宝短链接解析失败，无法建立共享商品标识") from error
            parsed = urlparse(resolved_url)
            host = (parsed.hostname or "").lower()

        platform = _shared_platform(host)
        if platform is None:
            if was_short_link:
                raise ProductIdentityError("淘宝短链接未解析到有效商品，无法建立共享商品标识")
            return None
        product_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if not product_id.isdigit():
            raise ProductIdentityError("淘宝或天猫链接缺少稳定商品 ID")
        canonical_url = _canonical_url(platform, product_id)
        return ProductIdentity(
            platform=platform,
            product_id=product_id,
            product_key=f"{platform}-{product_id}",
            source_url=source_url,
            canonical_url=canonical_url,
        )


def _shared_platform(host: str) -> Literal["taobao", "tmall"] | None:
    if is_taobao_host(host):
        return "taobao"
    if is_tmall_host(host):
        return "tmall"
    return None


def _canonical_url(platform: Literal["taobao", "tmall"], product_id: str) -> str:
    if platform == "taobao":
        return f"https://item.taobao.com/item.htm?id={product_id}"
    return f"https://detail.tmall.com/item.htm?id={product_id}"
