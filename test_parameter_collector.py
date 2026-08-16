import unittest
from unittest.mock import patch

from parameter_collector import collect_product_parameters


class ParameterCollectorTests(unittest.TestCase):
    def test_expands_panel_before_accepting_summary_parameters(self) -> None:
        page = object()
        summary = [
            {"name": "材质", "value": "棉"},
            {"name": "适用季节", "value": "四季"},
        ]
        expanded = summary + [
            {"name": "品牌", "value": "自有品牌"},
            {"name": "颜色", "value": "黑色"},
            {"name": "尺码", "value": "M"},
            {"name": "款式", "value": "基础款"},
        ]

        with (
            patch("parameter_collector.parameter_surfaces", return_value=[page]),
            patch("parameter_collector.open_parameter_panel", return_value=(page, True, True)),
            patch(
                "parameter_collector.extract_visible_parameter_rows",
                side_effect=[summary, expanded],
            ),
        ):
            result = collect_product_parameters(page, "123")

        self.assertEqual(result["parameter_status"], "complete")
        self.assertEqual(len(result["product_parameters"]), 6)

    def test_marks_small_parameter_result_as_partial(self) -> None:
        page = object()
        summary = [{"name": "材质", "value": "棉"}]
        with (
            patch("parameter_collector.parameter_surfaces", return_value=[page]),
            patch("parameter_collector.open_parameter_panel", return_value=(page, True, True)),
            patch(
                "parameter_collector.extract_visible_parameter_rows",
                side_effect=[summary, summary],
            ),
        ):
            result = collect_product_parameters(page, "123")

        self.assertEqual(result["parameter_status"], "partial")
        self.assertEqual(result["product_parameters"], summary)


if __name__ == "__main__":
    unittest.main()
