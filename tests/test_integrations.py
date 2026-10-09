import hashlib
import json
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

    def test_payload_local_file_is_added_and_removed_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "my_payload.elf"
            payload.write_bytes(b"known payload bytes")
            playlist_file = root / "playlist.json"
            with patch.object(hermes, "PAYLOAD_PLAYLIST_FILE", playlist_file), patch.object(hermes, "activity"):
                added = self.client.post("/api/payloads/add-local", json={
                    "path": str(payload),
                    "name": "My payload",
                })
                self.assertEqual(added.status_code, 201, added.get_json())
                item = added.get_json()["item"]
                self.assertTrue(payload.exists())
                self.assertEqual(item["name"], "My payload")
                self.assertTrue(item["exists"])
                removed = self.client.post("/api/payloads/remove", json={"id": item["id"]})
                self.assertEqual(removed.status_code, 200, removed.get_json())
                self.assertEqual(removed.get_json()["items"], [])
                self.assertTrue(payload.exists(), "Removing a playlist entry must not delete the payload file")

    def test_payload_official_download_verifies_digest_and_saves_to_playlist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            content = b"abcdef"
            digest = hashlib.sha256(content).hexdigest()
            release = {
                "repository": "etaHEN/etaHEN",
                "label": "etaHEN",
                "tag": "v1-test",
                "url": "https://github.com/etaHEN/etaHEN/releases/tag/v1-test",
                "assets": [{
                    "name": "payload.elf",
                    "size": len(content),
                    "download_count": 1,
                    "digest": "sha256:" + digest,
                }],
            }
            api_response = MagicMock()
            api_response.json.return_value = {
                "tag_name": "v1-test",
                "assets": [{
                    "name": "payload.elf",
                    "size": len(content),
                    "digest": "sha256:" + digest,
                    "browser_download_url": "https://github.com/etaHEN/etaHEN/releases/download/v1-test/payload.elf",
                }],
            }
            download_response = MagicMock()
            download_response.__enter__.return_value = download_response
            download_response.iter_content.return_value = [b"abc", b"def"]
            with (
                patch.object(hermes, "fetch_latest_payload_release", return_value=release),
                patch.object(hermes, "PAYLOAD_DOWNLOAD_DIR", root / "payloads"),
                patch.object(hermes, "PAYLOAD_PLAYLIST_FILE", root / "playlist.json"),
                patch.object(hermes.requests, "get", side_effect=[api_response, download_response]),
                patch.object(hermes, "activity"),
            ):
                response = self.client.post("/api/payloads/releases/download", json={
                    "repository": "etaHEN/etaHEN",
                    "asset_name": "payload.elf",
                })
            self.assertEqual(response.status_code, 201, response.get_json())
            item = response.get_json()["item"]
            saved_path = Path(item["path"])
            self.assertEqual(saved_path.read_bytes(), content)
            self.assertEqual(item["sha256"], digest)
            self.assertEqual(item["source"], "official")

    def test_payload_playlist_reorder_requires_all_ids_and_persists_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file_a, file_b = root / "a.elf", root / "b.elf"
            file_a.write_bytes(b"a")
            file_b.write_bytes(b"b")
            playlist_file = root / "playlist.json"
            playlist_file.write_text(json.dumps([
                {"id": "a", "name": "A", "path": str(file_a)},
                {"id": "b", "name": "B", "path": str(file_b)},
            ]), encoding="utf-8")
            with patch.object(hermes, "PAYLOAD_PLAYLIST_FILE", playlist_file):
                invalid = self.client.post("/api/payloads/reorder", json={"ids": ["b"]})
                self.assertEqual(invalid.status_code, 400)
                valid = self.client.post("/api/payloads/reorder", json={"ids": ["b", "a"]})
            self.assertEqual(valid.status_code, 200, valid.get_json())
            self.assertEqual([item["id"] for item in valid.get_json()["items"]], ["b", "a"])
            self.assertEqual([item["id"] for item in json.loads(playlist_file.read_text(encoding="utf-8"))], ["b", "a"])

    def test_payload_local_file_rejects_unexpected_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "not-a-payload.exe"
            payload.write_bytes(b"not a payload")
            with patch.object(hermes, "PAYLOAD_PLAYLIST_FILE", root / "playlist.json"):
                response = self.client.post("/api/payloads/add-local", json={"path": str(payload)})
            self.assertEqual(response.status_code, 400)
            self.assertIn(".elf, .bin or .payload", response.get_json()["error"])

    def test_payload_sender_rejects_public_ip_before_queueing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "demo.elf"
            payload.write_bytes(b"payload")
            playlist = [{"id": "payload-test", "path": str(payload), "name": "Test payload", "filename": payload.name}]
            playlist_file = root / "playlist.json"
            playlist_file.write_text(json.dumps(playlist), encoding="utf-8")
            with patch.object(hermes, "PAYLOAD_PLAYLIST_FILE", playlist_file), patch.object(hermes, "activity"):
                with patch.object(hermes.threading, "Thread") as thread:
                    response = self.client.post("/api/payloads/send", json={
                        "id": "payload-test",
                        "ip": "8.8.8.8",
                        "port": 9021,
                    })
            self.assertEqual(response.status_code, 400)
            thread.assert_not_called()

    def test_payload_sender_worker_writes_file_bytes_and_marks_transfer_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = Path(temp) / "demo.bin"
            payload.write_bytes(b"exact payload data")
            hermes.PAYLOAD_JOBS["test-job"] = {"id": "test-job", "status": "queued", "created_at": 1}
            mock_socket = MagicMock()
            mock_socket.__enter__.return_value = mock_socket
            try:
                with patch.object(hermes.socket, "create_connection", return_value=mock_socket) as connect, patch.object(hermes, "activity"):
                    hermes.payload_sender_worker("test-job", str(payload), "192.168.1.40", 9021)
                connect.assert_called_once_with(("192.168.1.40", 9021), timeout=6)
                mock_socket.sendall.assert_called_once_with(b"exact payload data")
                self.assertEqual(hermes.PAYLOAD_JOBS["test-job"]["status"], "sent")
                self.assertEqual(hermes.PAYLOAD_JOBS["test-job"]["bytes_sent"], len(b"exact payload data"))
                self.assertIn("does not confirm", hermes.PAYLOAD_JOBS["test-job"]["message"])
            finally:
                hermes.PAYLOAD_JOBS.pop("test-job", None)

    def test_payload_release_metadata_is_from_allowlisted_official_source(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v99-test",
            "name": "Test release",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/etaHEN/etaHEN/releases/tag/v99-test",
            "body": "Test notes",
            "assets": [{
                "name": "payload.elf",
                "size": 12,
                "download_count": 5,
                "digest": "sha256:abc",
                "browser_download_url": "https://github.com/etaHEN/etaHEN/releases/download/v99-test/payload.elf",
            }, {
                "name": "source.zip",
                "size": 100,
                "download_count": 2,
                "browser_download_url": "https://github.com/etaHEN/etaHEN/releases/download/v99-test/source.zip",
            }],
        }
        with patch.object(hermes.requests, "get", return_value=mock_response):
            release = hermes.fetch_latest_payload_release("etaHEN/etaHEN")
        self.assertEqual(release["tag"], "v99-test")
        self.assertEqual([asset["name"] for asset in release["assets"]], ["payload.elf"])
        with self.assertRaises(ValueError):
            hermes.fetch_latest_payload_release("untrusted-user/untrusted-repo")

    def test_storage_audit_reports_missing_and_ready_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / "idm"
            ready.mkdir()
            with patch.dict(hermes.CONFIG, {
                "download_root": str(ready),
                "rar_temp": str(root / "missing-rar"),
                "automation_root": str(root / "automation"),
                "game_root": str(root / "games"),
                "inbox_root": str(root / "inbox"),
                "ftp_local_root": str(root / "ftp"),
            }):
                response = self.client.get("/api/storage/audit", query_string={"root": str(root)})
            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()
            paths = {item["key"]: item for item in payload["paths"]}
            self.assertEqual(paths["download_root"]["status"], "ready")
            self.assertEqual(paths["rar_temp"]["status"], "missing")
            self.assertFalse(paths["rar_temp"]["exists"])
            self.assertEqual(payload["drive"]["path"], str(root.resolve()))

    def test_storage_create_missing_paths_only_creates_inside_selected_root(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            missing_inside = root / "missing" / "subfolder"
            outside_path = Path(outside) / "should-not-create"
            with patch.dict(hermes.CONFIG, {
                "download_root": str(missing_inside),
                "rar_temp": str(root / "already-there"),
                "automation_root": str(outside_path),
                "game_root": str(root),
                "inbox_root": str(root / "inbox"),
                "ftp_local_root": str(root / "ftp"),
            }), patch.object(hermes, "ensure_layout"):
                (root / "already-there").mkdir()
                response = self.client.post("/api/storage/paths/create-missing", json={"root": str(root)})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(missing_inside.is_dir())
            self.assertFalse(outside_path.exists())
            skipped = {item["label"]: item for item in response.get_json()["skipped"]}
            self.assertIn("Outside the selected drive/folder", skipped["Hermes automation and metadata"]["reason"])
            self.assertIn("entire selected drive", skipped["Extracted PS5 games"]["reason"])

    def test_storage_organization_preview_only_lists_direct_files_and_never_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = root / "notes.pdf"
            image = root / "photo.jpg"
            payload = root / "custom.elf"
            archive = root / "game.part01.rar"
            nested = root / "nested"
            nested.mkdir()
            doc.write_bytes(b"document")
            image.write_bytes(b"image")
            payload.write_bytes(b"payload")
            archive.write_bytes(b"part")
            (nested / "ignore.mp4").write_bytes(b"nested video")
            with patch.dict(hermes.CONFIG, {
                "download_root": str(root / "idm"),
                "rar_temp": str(root / "rar"),
                "automation_root": str(root / "automation"),
                "game_root": str(root / "games"),
                "inbox_root": str(root / "inbox"),
                "ftp_local_root": str(root / "ftp"),
            }):
                response = self.client.post("/api/storage/organize/preview", json={"root": str(root)})
            self.assertEqual(response.status_code, 200, response.get_json())
            items = response.get_json()["items"]
            self.assertEqual({item["filename"] for item in items}, {"notes.pdf", "photo.jpg", "custom.elf"})
            self.assertTrue(all(not item["selected_by_default"] for item in items))
            self.assertTrue(doc.exists() and image.exists() and payload.exists() and archive.exists())
            self.assertTrue((nested / "ignore.mp4").exists())

    def test_storage_move_selected_moves_only_reviewed_files_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            moved_source = root / "move-me.pdf"
            collision_source = root / "collision.pdf"
            moved_source.write_bytes(b"move me")
            collision_source.write_bytes(b"preserve original")
            collision_dir = root / "_Organized" / "Documents"
            collision_dir.mkdir(parents=True)
            collision_destination = collision_dir / "collision.pdf"
            collision_destination.write_bytes(b"existing destination")
            with patch.dict(hermes.CONFIG, {
                "download_root": str(root / "idm"),
                "rar_temp": str(root / "rar"),
                "automation_root": str(root / "automation"),
                "game_root": str(root / "games"),
                "inbox_root": str(root / "inbox"),
                "ftp_local_root": str(root / "ftp"),
            }), patch.object(hermes, "activity"):
                response = self.client.post("/api/storage/organize/move-selected", json={
                    "root": str(root),
                    "filenames": ["move-me.pdf", "collision.pdf", "unrecognized.bin"],
                })
            self.assertEqual(response.status_code, 200, response.get_json())
            result = response.get_json()
            self.assertEqual([item["filename"] for item in result["moved"]], ["move-me.pdf"])
            self.assertFalse(moved_source.exists())
            self.assertEqual((root / "_Organized" / "Documents" / "move-me.pdf").read_bytes(), b"move me")
            self.assertTrue(collision_source.exists())
            self.assertEqual(collision_destination.read_bytes(), b"existing destination")
            skipped = {item["filename"]: item["reason"] for item in result["skipped"]}
            self.assertIn("already exists", skipped["collision.pdf"].lower())
            self.assertIn("No safe organization rule", skipped["unrecognized.bin"])

    def test_ftp_path_rejects_traversal(self):
        with self.assertRaises(ValueError):
            hermes.ftp_remote_path("/data/homebrew/../../system")
        self.assertEqual(hermes.ftp_remote_path("/data/homebrew/"), "/data/homebrew")


if __name__ == "__main__":
    unittest.main()
