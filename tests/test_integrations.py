import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as hermes


class HermesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = hermes.app.test_client()

    def test_remote_ps5_path_normalizes_and_requires_absolute_path(self):
        self.assertEqual(hermes.remote_ps5_path(r"/data/homebrew"), "/data/homebrew")
        self.assertEqual(hermes.remote_ps5_path(r"/data/homebrew//"), "/data/homebrew")
        with self.assertRaises(ValueError):
            hermes.remote_ps5_path("/data/../etc/passwd")
        with self.assertRaises(ValueError):
            hermes.remote_ps5_path("data/homebrew")

    def test_engine_must_remain_loopback(self):
        self.assertEqual(hermes.validate_engine_url("http://127.0.0.1:19113"), "http://127.0.0.1:19113")
        with self.assertRaises(ValueError):
            hermes.validate_engine_url("http://192.168.1.25:19113")
        with self.assertRaises(ValueError):
            hermes.validate_engine_url("https://127.0.0.1:19113/path")

    def test_public_ip_is_rejected_before_connection_checks(self):
        response = self.client.post(
            "/api/integrations/check",
            json={"ps5_ip": "8.8.8.8", "ps5upload_engine_url": "http://127.0.0.1:19113"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("private LAN", response.get_json()["error"])

    def test_idm_rejects_embedded_credentials_and_non_http_urls(self):
        for url in ("https://user:pass@example.com/file.pkg", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(url=url):
                response = self.client.post("/api/downloads/idm", json={"url": url})
                self.assertEqual(response.status_code, 400)

    def test_transfer_appends_source_filename_to_destination_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "test.elf"
            source.write_bytes(b"demo payload")
            with patch.dict(hermes.CONFIG, {
                "download_root": temp,
                "game_root": temp,
                "inbox_root": temp,
                "ftp_local_root": temp,
            }), patch.object(hermes, "update_config", side_effect=lambda values: hermes.CONFIG.update(values)), patch.object(hermes, "activity"):
                engine_response = MagicMock()
                engine_response.ok = True
                engine_response.json.return_value = {"job_id": "job-transfer-123"}
                with patch.object(hermes, "ps5upload_request", return_value=engine_response) as request:
                    response = self.client.post("/api/ps5upload/transfer", json={
                        "local_path": str(source),
                        "remote_path": "/data/homebrew",
                        "ps5_ip": "192.168.1.40",
                        "engine_url": "http://127.0.0.1:19113",
                    })
            self.assertEqual(response.status_code, 202, response.get_json())
            self.assertEqual(response.get_json()["remote_path"], "/data/homebrew/test.elf")
            self.assertEqual(request.call_args.kwargs["body"]["dest"], "/data/homebrew/test.elf")

    def test_pkg_install_inspects_metadata_and_keeps_safe_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "demo.pkg"
            package.write_bytes(b"fake test data")
            with patch.dict(hermes.CONFIG, {
                "download_root": temp,
                "game_root": temp,
                "inbox_root": temp,
                "ftp_local_root": temp,
            }), patch.object(hermes, "update_config", side_effect=lambda values: hermes.CONFIG.update(values)), patch.object(hermes, "activity"):
                parsed = MagicMock()
                parsed.ok = True
                parsed.status_code = 200
                parsed.json.return_value = {
                    "content_id": "UP0001-PPSA12345_00-DEMO000000000000",
                    "title_id": "PPSA12345",
                    "category": "gd",
                    "app_ver": "01.00",
                    "title": "Example Game",
                    "size": 14,
                    "platform": "ps5",
                    "kind": "Ps5Finalized",
                }
                accepted = MagicMock()
                accepted.ok = True
                accepted.status_code = 200
                accepted.json.return_value = {"ok": True, "job": "install-job-1"}
                with patch.object(hermes, "ps5upload_request", side_effect=[parsed, accepted]) as request:
                    response = self.client.post("/api/ps5upload/pkg/install", json={
                        "local_path": str(package),
                        "ps5_ip": "192.168.1.40",
                        "engine_url": "http://127.0.0.1:19113",
                    })
            self.assertEqual(response.status_code, 202, response.get_json())
            self.assertEqual(response.get_json()["metadata"]["title_id"], "PPSA12345")
            body = request.call_args.kwargs["body"]
            self.assertFalse(body["options"]["allow_destructive_reinstall"])
            self.assertFalse(body["options"]["delete_source_copy_after"])

    def test_pkg_install_refuses_np_xs_system_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "system.pkg"
            package.write_bytes(b"not-a-real-pkg")
            with patch.dict(hermes.CONFIG, {
                "download_root": temp,
                "game_root": temp,
                "inbox_root": temp,
                "ftp_local_root": temp,
            }), patch.object(hermes, "update_config", side_effect=lambda values: hermes.CONFIG.update(values)):
                parsed = MagicMock()
                parsed.ok = True
                parsed.status_code = 200
                parsed.json.return_value = {
                    "content_id": "UP0001-NPXS00001_00-SYSTEM00000000000",
                    "title_id": "NPXS00001",
                    "category": "gd",
                    "app_ver": "01.00",
                    "title": "System app",
                }
                with patch.object(hermes, "ps5upload_request", return_value=parsed) as request:
                    response = self.client.post("/api/ps5upload/pkg/install", json={
                        "local_path": str(package),
                        "ps5_ip": "192.168.1.40",
                        "engine_url": "http://127.0.0.1:19113",
                    })
            self.assertEqual(response.status_code, 400)
            self.assertIn("System packages", response.get_json()["error"])
            self.assertEqual(request.call_count, 1, "Should reject before invoking the install endpoint")

    def test_ftp_path_rejects_traversal(self):
        with self.assertRaises(ValueError):
            hermes.ftp_remote_path("/data/homebrew/../../system")
        self.assertEqual(hermes.ftp_remote_path("/data/homebrew/"), "/data/homebrew")


if __name__ == "__main__":
    unittest.main()
