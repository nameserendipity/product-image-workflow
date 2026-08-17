import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kuaishou_parameters import ensure_kuaishou_product_parameters


class KuaishouParameterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_visual_dossier_fills_empty_kuaishou_parameters(self) -> None:
        dossier_path = self.root / "product-dossier.json"
        dossier_path.write_text(
            json.dumps(
                {
                    "observations": [
                        {
                            "source_index": 1,
                            "colors": [
                                "white bottle",
                                "gold label",
                                "brown advertising background",
                                "black clothing on portrait",
                            ],
                        },
                        {
                            "source_index": 2,
                            "colors": ["red bottle"],
                        },
                    ],
                    "dossier": {
                        "anchor_identity": {
                            "source_index": 1,
                            "object": "one rectangular pump shampoo bottle in an advertising graphic",
                            "visible_product_labeling": [
                                "PEPTIDE KERATIN SHAMPOO",
                                "800ml",
                            ],
                            "brand_or_mark": "unclear",
                        },
                        "confirmed_components": [
                            "rectangular bottle",
                            "pump dispenser",
                        ],
                        "materials_and_textures": [
                            {
                                "component": "bottle body",
                                "confirmed_visible_material_or_texture": "smooth plastic-like surface",
                            },
                            {
                                "component": "advertising background",
                                "confirmed_visible_material_or_texture": "beige poster graphic",
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        source = {
            "product_parameters": [],
            "sku_variants": [
                {"spec_text": "800ml*1瓶", "color_text": "白色"},
                {"spec_text": "800ml*2瓶", "color_text": "白色"},
            ],
        }

        result = ensure_kuaishou_product_parameters(source, dossier_path)

        self.assertEqual(result["parameter_status"], "inferred")
        self.assertTrue(result["product_parameters"])
        self.assertTrue(
            all(
                row["source"] == "visual_analysis"
                and row["handling"] == "图片识别，待核验"
                for row in result["product_parameters"]
            )
        )
        self.assertIn(
            {
                "name": "可见规格/容量",
                "value": "800ml",
                "source": "visual_analysis",
                "handling": "图片识别，待核验",
            },
            result["product_parameters"],
        )
        self.assertIn(
            {
                "name": "商品类型",
                "value": "one rectangular pump shampoo bottle",
                "source": "visual_analysis",
                "handling": "图片识别，待核验",
            },
            result["product_parameters"],
        )
        values = "\n".join(row["value"] for row in result["product_parameters"])
        self.assertNotIn("red bottle", values)
        self.assertNotIn("unclear", values.lower())
        self.assertNotIn("advertising background", values.lower())
        self.assertNotIn("clothing", values.lower())
        self.assertNotIn("poster graphic", values.lower())
        self.assertNotIn("component:", values.lower())
        self.assertEqual(source["product_parameters"], [])

    def test_platform_parameters_are_never_replaced(self) -> None:
        platform_rows = [
            {
                "name": "净含量",
                "value": "800ml",
                "source": "platform_api",
                "handling": "快手平台原值",
            }
        ]
        source = {
            "parameter_status": "complete",
            "product_parameters": platform_rows,
        }

        result = ensure_kuaishou_product_parameters(
            source,
            self.root / "missing.json",
        )

        self.assertEqual(result["parameter_status"], "complete")
        self.assertEqual(result["product_parameters"], platform_rows)
        self.assertIsNot(result, source)

    def test_missing_dossier_requires_manual_review(self) -> None:
        result = ensure_kuaishou_product_parameters(
            {},
            self.root / "missing.json",
        )

        self.assertEqual(result["parameter_status"], "needs_review")
        self.assertEqual(
            result["product_parameters"],
            [
                {
                    "name": "参数识别状态",
                    "value": "未识别到可靠参数，需人工补充",
                    "source": "manual_required",
                    "handling": "待人工补充",
                }
            ],
        )
        self.assertEqual(
            result["product_parameters_text"],
            "参数识别状态: 未识别到可靠参数，需人工补充",
        )


if __name__ == "__main__":
    unittest.main()
