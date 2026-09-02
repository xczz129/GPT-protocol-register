from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import registration_proxy_pool
from proxy_util import resolve_proxy_pool


class RegistrationProxyPoolTests(unittest.TestCase):
    def test_country_pools_preserve_input_and_deduplicate_for_scheduling(self):
        jp = "jp-gate.example:1000:user:password"
        us = "http://user:password@us-gate.example:2000"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxy-pools.json"
            with patch.object(registration_proxy_pool, "PATH", path):
                saved = registration_proxy_pool.write_registration_proxy_pools(
                    [
                        {"country": "jp", "label": "日本", "text": f"{jp}\n{jp}\n"},
                        {"country": "US", "label": "美国", "text": us},
                    ]
                )
                selected = resolve_proxy_pool(
                    {
                        "enabled": True,
                        "mode": "country_pool",
                        "selected_country": "JP",
                    }
                )
                mode = path.stat().st_mode & 0o777

        self.assertEqual(saved["countries"], 2)
        self.assertEqual(saved["total_lines"], 3)
        self.assertEqual(saved["total"], 2)
        self.assertEqual(saved["total_duplicates"], 1)
        self.assertEqual(saved["items"][0]["line_count"], 2)
        self.assertEqual(saved["items"][0]["count"], 1)
        self.assertEqual(saved["items"][0]["duplicate_count"], 1)
        self.assertEqual(saved["items"][0]["text"].splitlines(), [jp, jp])
        self.assertEqual(selected, ["http://user:password@jp-gate.example:1000"])
        self.assertEqual(mode, 0o600)

    def test_duplicate_country_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxy-pools.json"
            with patch.object(registration_proxy_pool, "PATH", path):
                with self.assertRaisesRegex(ValueError, "国家代码重复"):
                    registration_proxy_pool.write_registration_proxy_pools(
                        [
                            {"country": "JP", "text": "http://one.example:80"},
                            {"country": "jp", "text": "http://two.example:80"},
                        ]
                    )

    def test_country_pool_excludes_explicit_region_mismatches(self):
        matching = "gateway.example:1000:user-region-JP-sid-one:password"
        mismatched = "gateway.example:1000:user-region-VN-sid-two:password"
        untagged = "gateway.example:1000:user-three:password"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxy-pools.json"
            with patch.object(registration_proxy_pool, "PATH", path):
                saved = registration_proxy_pool.write_registration_proxy_pools(
                    [{
                        "country": "JP",
                        "label": "日本",
                        "text": f"{matching}\n{mismatched}\n{untagged}",
                    }]
                )
                selected = registration_proxy_pool.proxies_for_country("JP")

        row = saved["items"][0]
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["country_mismatch_count"], 1)
        self.assertEqual(row["declared_countries"], ["JP", "VN"])
        self.assertEqual(
            selected,
            [
                "http://user-region-JP-sid-one:password@gateway.example:1000",
                "http://user-three:password@gateway.example:1000",
            ],
        )

    def test_proxy_api_saves_country_mode_without_changing_registration(self):
        import web_app

        cfg = {
            "proxy": {"enabled": False, "mode": "single_proxy"},
            "registration": {"threads": 10},
        }
        body = web_app.ProxyUpdate(
            enabled=True,
            mode="country_pool",
            selected_country="JP",
            country_pools=[
                {"country": "JP", "label": "日本", "text": "http://jp.example:8080"}
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxy-pools.json"
            with (
                patch.object(registration_proxy_pool, "PATH", path),
                patch.object(web_app, "_read_config", return_value=cfg),
                patch.object(web_app, "_write_config") as write_config,
            ):
                result = asyncio.run(web_app.put_proxy(body))

        self.assertTrue(result["ok"])
        self.assertEqual(cfg["proxy"]["mode"], "country_pool")
        self.assertEqual(cfg["proxy"]["selected_country"], "JP")
        self.assertEqual(cfg["registration"]["threads"], 10)
        write_config.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()
