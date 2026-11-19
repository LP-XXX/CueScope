from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deal_watch.config import AppConfig, ConfigStore, validate_webhook_url
from deal_watch.models import Rect, RegionConfig


class ConfigTests(unittest.TestCase):
    def test_v2_config_moves_legacy_default_to_30_seconds_and_drops_learning(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.path.write_text(
                '{"version":2,"poll_interval_ms":60000,"learning_enabled":true}',
                encoding="utf-8",
            )
            config = store.load()
            self.assertEqual(config.version, 4)
            self.assertEqual(config.poll_interval_ms, 30000)
            self.assertEqual(config.recognition_record_limit, 20000)
            self.assertNotIn("learning_enabled", config.__dataclass_fields__)
            store.save(config)
            saved = store.path.read_text(encoding="utf-8")
            self.assertNotIn("learning_enabled", saved)
            self.assertIn('"version": 4', saved)

    def test_v2_custom_poll_interval_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.path.write_text(
                '{"version":2,"poll_interval_ms":45000}', encoding="utf-8"
            )
            self.assertEqual(store.load().poll_interval_ms, 45000)

    def test_webhook_allows_only_feishu_https_or_explicit_loopback(self):
        for url in (
            "https://open.feishu.cn/open-apis/bot/v2/hook/test-only",
            "http://127.0.0.1:18080/webhook",
            "http://localhost:18080/webhook",
            "http://[::1]:18080/webhook",
        ):
            validate_webhook_url(url)
        for url in (
            "http://open.feishu.cn/open-apis/bot/v2/hook/test-only",
            "https://open.feishu.cn.attacker.invalid/private-token",
            "https://user:password@open.feishu.cn/open-apis/bot/v2/hook/private-token",
            "https://open.feishu.cn/open-apis/bot/v2/hook/private-token?secret=1",
            "https://example.invalid/private-token",
            "http://127.0.0.1.example.invalid/private-token",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                validate_webhook_url(url)
            self.assertNotIn("private-token", str(raised.exception))

    def test_credentials_are_never_written_to_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            config = AppConfig(
                webhook="https://open.feishu.cn/secret",
                keywords=["手机"],
                regions=[RegionConfig(alias="一群", window_title="微信", rect=Rect(0, 0, 100, 100))],
            )
            store.save(config)
            raw = store.path.read_text(encoding="utf-8")
            self.assertNotIn("open.feishu.cn", raw)
            loaded = store.load()
            self.assertEqual(loaded.webhook, "")
            self.assertEqual(loaded.keywords, ["手机"])
            self.assertEqual(loaded.regions[0].rect.width, 100)


if __name__ == "__main__":
    unittest.main()
