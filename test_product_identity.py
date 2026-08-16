import unittest

from product_identity import ProductIdentityError, ProductIdentityResolver


class ProductIdentityResolverTests(unittest.TestCase):
    def test_taobao_promotional_parameters_share_one_key(self) -> None:
        resolver = ProductIdentityResolver()

        first = resolver.resolve("https://item.taobao.com/item.htm?id=123&spm=a1&skuId=9")
        second = resolver.resolve("https://item.taobao.com/item.htm?pvid=x&id=123")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertEqual(first.product_key, "taobao-123")
        self.assertEqual(second.product_key, "taobao-123")
        self.assertEqual(first.canonical_url, "https://item.taobao.com/item.htm?id=123")
        self.assertEqual(second.canonical_url, "https://item.taobao.com/item.htm?id=123")

    def test_tmall_uses_a_platform_specific_key(self) -> None:
        identity = ProductIdentityResolver().resolve(
            "https://detail.tmall.com/item.htm?id=456&abbucket=1"
        )

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.platform, "tmall")
        self.assertEqual(identity.product_id, "456")
        self.assertEqual(identity.product_key, "tmall-456")
        self.assertEqual(identity.canonical_url, "https://detail.tmall.com/item.htm?id=456")

    def test_tmall_global_uses_the_tmall_product_key(self) -> None:
        identity = ProductIdentityResolver().resolve(
            "https://detail.tmall.hk/hk/item.htm?id=457&skuId=99"
        )

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.platform, "tmall")
        self.assertEqual(identity.product_key, "tmall-457")

    def test_taobao_short_link_uses_injected_redirect_resolver(self) -> None:
        calls: list[tuple[str, float]] = []

        def resolve_redirect(value: str, timeout: float) -> str:
            calls.append((value, timeout))
            return "https://item.taobao.com/item.htm?id=789&spm=redirected"

        identity = ProductIdentityResolver(resolve_redirect).resolve(
            "https://m.tb.cn/h.test",
            timeout=3.5,
        )

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(calls, [("https://m.tb.cn/h.test", 3.5)])
        self.assertEqual(identity.product_key, "taobao-789")
        self.assertEqual(identity.source_url, "https://m.tb.cn/h.test")
        self.assertEqual(identity.canonical_url, "https://item.taobao.com/item.htm?id=789")

    def test_e_tb_share_link_uses_injected_redirect_resolver(self) -> None:
        calls: list[tuple[str, float]] = []

        def resolve_redirect(value: str, timeout: float) -> str:
            calls.append((value, timeout))
            return "https://item.taobao.com/item.htm?id=790"

        identity = ProductIdentityResolver(resolve_redirect).resolve(
            "https://e.tb.cn/h.test",
            timeout=4.0,
        )

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(calls, [("https://e.tb.cn/h.test", 4.0)])
        self.assertEqual(identity.product_key, "taobao-790")

    def test_non_shared_platform_returns_none(self) -> None:
        resolver = ProductIdentityResolver()

        self.assertIsNone(resolver.resolve("https://item.jd.com/123.html"))
        self.assertIsNone(resolver.resolve("https://v.douyin.com/example"))

    def test_taobao_short_link_must_resolve_to_a_shared_product(self) -> None:
        resolver = ProductIdentityResolver(
            lambda _value, _timeout: "https://example.com/not-a-product"
        )

        with self.assertRaisesRegex(ProductIdentityError, "无法建立共享商品标识"):
            resolver.resolve("https://m.tb.cn/h.invalid")

    def test_shared_link_without_numeric_product_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProductIdentityError, "稳定商品 ID"):
            ProductIdentityResolver().resolve(
                "https://detail.tmall.com/item.htm?id=not-a-number"
            )


if __name__ == "__main__":
    unittest.main()
