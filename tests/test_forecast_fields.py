import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pv_history_forecast" / "forecast_fields.py"
SPEC = importlib.util.spec_from_file_location("forecast_fields_test_module", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
resolve_forecast_field_value = MODULE.resolve_forecast_field_value


class ForecastFieldsTest(unittest.TestCase):
    def test_resolve_forecast_field_value_supports_provider_aliases(self) -> None:
        payload = {
            "cloud_cover": 42,
            "uv": 5.2,
            "temp": 21.7,
            "precipitation_probability": 73,
        }

        self.assertEqual(resolve_forecast_field_value(payload, "cloud_coverage"), 42.0)
        self.assertEqual(resolve_forecast_field_value(payload, "uv_index"), 5.2)
        self.assertEqual(resolve_forecast_field_value(payload, "temperature"), 21.7)
        self.assertEqual(resolve_forecast_field_value(payload, "precipitation"), 73.0)

    def test_resolve_forecast_field_value_prefers_non_zero_aliases(self) -> None:
        payload = {
            "uv_index": 0.0,
            "uv": 2.7,
        }

        self.assertEqual(resolve_forecast_field_value(payload, "uv_index"), 2.7)


if __name__ == "__main__":
    unittest.main()
