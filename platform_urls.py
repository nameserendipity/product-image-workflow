from __future__ import annotations


TAOBAO_SHORT_HOSTS = frozenset({"m.tb.cn", "e.tb.cn"})


def host_matches_domain(host: str, domain: str) -> bool:
    normalized = host.lower().strip(".")
    return normalized == domain or normalized.endswith(f".{domain}")


def is_taobao_host(host: str) -> bool:
    return host_matches_domain(host, "taobao.com")


def is_tmall_host(host: str) -> bool:
    return any(host_matches_domain(host, domain) for domain in ("tmall.com", "tmall.hk"))


def is_taobao_or_tmall_host(host: str) -> bool:
    return is_taobao_host(host) or is_tmall_host(host)


def is_douyin_product_host(host: str) -> bool:
    return host_matches_domain(host, "jinritemai.com")
