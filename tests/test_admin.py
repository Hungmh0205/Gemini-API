import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

import admin_api
import config_store
import logstore


class TestConfigStore(unittest.TestCase):
    """Mọi test dùng config tạm qua tham số `path`, không đụng config.json thật."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self._old_env = {
            k: os.environ.pop(k, None)
            for k in list(config_store._SCALAR_ENV.values()) + list(config_store._NESTED_ENV.values())
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

    def _restore_env(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_load_fills_defaults_for_missing_keys(self):
        self.write({"port": 9001})
        cfg = config_store.load(self.path)
        self.assertEqual(cfg.port, 9001)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.browser.mode, "agent")
        # Không ghi file khi chỉ đọc.
        self.assertEqual(set(json.loads(self.path.read_text(encoding='utf-8'))), {"port"})

    def test_invalid_type_raises_config_error(self):
        self.write({"port": "khong-phai-so"})
        with self.assertRaises(config_store.ConfigError):
            config_store.load(self.path)

    def test_bad_json_does_not_get_silently_replaced(self):
        self.path.write_text("{khong phai json", encoding="utf-8")
        with self.assertRaises(config_store.ConfigError):
            config_store.load(self.path)

    def test_env_overrides_file_and_marks_field_locked(self):
        self.write({"port": 8000, "model": "gemini-flash"})
        os.environ["PORT"] = "9100"
        cfg = config_store.load(self.path)
        self.assertEqual(cfg.port, 9100)
        self.assertIn("port", config_store.locked_fields())
        # Giá trị trong file không bị đổi theo env.
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))["port"], 8000)

    def test_invalid_env_value_is_ignored(self):
        self.write({"port": 8000})
        os.environ["PORT"] = "abc"
        self.assertEqual(config_store.load(self.path).port, 8000)

    def test_env_locked_field_cannot_be_written(self):
        os.environ["GEMINI_MODEL"] = "gemini-pro"
        with self.assertRaises(config_store.ConfigError):
            config_store.update({"model": "gemini-ultra"}, self.path)

    def test_nested_env_override_is_reported_with_dotted_name(self):
        self.write({})
        os.environ["GEMINI_CLOSE_TIMEOUT"] = "40"
        self.assertEqual(config_store.load(self.path).browser.close_timeout, 40)
        self.assertIn("browser.close_timeout", config_store.locked_fields())

    def test_update_merges_nested_and_keeps_other_keys(self):
        self.write({"port": 8000, "browser": {"enabled": True, "keep_alive": True}})
        config_store.update({"browser": {"keep_alive": False}}, self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(data["browser"]["enabled"], True)
        self.assertIs(data["browser"]["keep_alive"], False)
        self.assertEqual(data["port"], 8000)

    def test_update_rejects_unknown_key(self):
        self.write({})
        with self.assertRaises(config_store.ConfigError):
            config_store.update({"khong_ton_tai": 1}, self.path)

    def test_update_rejects_path_escape(self):
        self.write({})
        with self.assertRaises(config_store.ConfigError):
            config_store.update({"auth_file": "../../windows/system32/x"}, self.path)

    def test_update_rejects_chrome_default_user_data_dir(self):
        # Ghi đè user_data_dir mặc định là cách nhanh nhất để tắt nhầm Chrome cá nhân.
        self.write({})
        with self.assertRaises(config_store.ConfigError):
            config_store.update(
                {"browser": {"user_data_dir": str(chrome_default_root())}}, self.path
            )

    def test_redact_masks_key_but_keeps_shape(self):
        out = config_store.redact({"api_key": "sk-lumi-0123456789abcdef",
                                   "admin": {"token": "0123456789abcdef0123"}})
        blob = json.dumps(out)
        self.assertNotIn("0123456789abcdef", blob)
        self.assertTrue(out["api_key"])
        self.assertTrue(out["admin"]["token"])

    def test_redact_masks_short_secret_entirely(self):
        # Giữ 4 ký tự cuối với bí mật 3 ký tự gần như không che gì.
        out = config_store.redact({"api_key": "abc", "admin": {"token": "xyz"}})
        self.assertNotIn("abc", json.dumps(out))
        self.assertNotIn("xyz", json.dumps(out))

    def test_ensure_secrets_generates_once(self):
        self.write({})
        raw, changed = config_store.ensure_secrets(self.path)
        self.assertTrue(changed)
        self.assertTrue(raw["api_key"])
        self.assertTrue(raw["admin"]["token"])
        self.assertNotEqual(raw["api_key"], raw["admin"]["token"])
        again, changed2 = config_store.ensure_secrets(self.path)
        self.assertFalse(changed2)
        self.assertEqual(again["api_key"], raw["api_key"])

    def test_save_is_atomic_no_temp_left(self):
        config_store.save({"port": 8001}, self.path)
        self.assertEqual(config_store.load(self.path).port, 8001)
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])

    def test_save_rejects_default_chrome_root(self):
        with self.assertRaises(config_store.ConfigError):
            config_store.save({"browser": {"user_data_dir": str(chrome_default_root())}}, self.path)

    def test_save_rejects_chrome_root_subdirectory(self):
        # Trỏ vào "User Data\Default" nguy hiểm không kém trỏ vào chính "User Data":
        # browser_reset sẽ xoá đúng thư mục profile đó.
        with self.assertRaises(config_store.ConfigError):
            config_store.save(
                {"browser": {"user_data_dir": str(chrome_default_root() / "Default")}}, self.path
            )

    def test_save_rejects_folder_containing_chrome_root(self):
        # Chiều ngược lại: User Data cá nhân nằm trong thư mục được trỏ tới.
        with self.assertRaises(config_store.ConfigError):
            config_store.save(
                {"browser": {"user_data_dir": str(chrome_default_root().parent)}}, self.path
            )

    def test_save_allows_agent_dir_and_tolerates_unexpanded_var(self):
        # config.json thật lưu "%LOCALAPPDATA%\Lumi-Agent\Chrome" chưa expand; guard
        # phải so sánh trên dạng đã expand, và dạng này phải đi qua.
        with tempfile.TemporaryDirectory() as tmp:
            config_store.save({"browser": {"user_data_dir": str(Path(tmp) / "udd")}}, self.path)
            self.assertTrue(self.path.is_file())

    def test_touches_detects_nesting_in_both_directions(self):
        root = chrome_default_root()
        self.assertTrue(config_store.touches(str(root), str(root)))
        self.assertTrue(config_store.touches(str(root / "Profile 1"), str(root)))
        self.assertTrue(config_store.touches(str(root), str(root / "Profile 1")))
        self.assertFalse(config_store.touches(r"C:\other\udd", str(root)))
        self.assertFalse(config_store.touches("", str(root)))


def chrome_default_root() -> Path:
    # Nhập chrome_launcher trong hàm để test đổi được môi trường trước khi gọi.
    import chrome_launcher

    return chrome_launcher.user_data_root()


class _FakeRequest:
    """Đủ để route đọc body JSON; portable không cài `httpx` nên không dùng TestClient."""

    def __init__(self, body: bytes = b"{}"):
        self._body = body

    async def json(self):
        return json.loads(self._body.decode("utf-8") or "{}")


class TestLogstore(unittest.TestCase):
    def test_redact_masks_google_cookies(self):
        line = "cookie __Secure-1PSID=abc123; SAPISID=zz; sid=qq"
        out = logstore.redact(line)
        for secret in ("abc123", "zz", "qq"):
            self.assertNotIn(secret, out)
        self.assertIn("__Secure-1PSID=", out)

    def test_redact_does_not_leak_token_as_prefix_only(self):
        out = logstore.redact("Authorization: Bearer sk-lumi-ABCDEFGH1234")
        self.assertNotIn("ABCDEFGH1234", out)
        self.assertIn("Bearer", out)

    def test_redact_keeps_long_cookie_name_intact(self):
        # `SID` là hậu tố của `__Secure-1PSID`; regex phải khớp tên dài trước.
        out = logstore.redact("__Secure-1PSID=secretvalue")
        self.assertIn("__Secure-1PSID=***", out)

    def test_redact_empty(self):
        self.assertEqual(logstore.redact(""), "")

    def test_tail_masks_file_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lumi.log"
            p.write_text("ok\n__Secure-1PSID=leakme\n", encoding="utf-8")
            logstore._log_path = p
            self.addCleanup(setattr, logstore, "_log_path", None)
            lines = logstore.tail(10)
        self.assertNotIn("leakme", "\n".join(lines))
        self.assertIn("ok", lines)

    def test_tail_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            logstore._log_path = Path(tmp) / "khong-co.log"
            self.addCleanup(setattr, logstore, "_log_path", None)
            self.assertEqual(logstore.tail(10), [])


class TestLogRotationOwner(unittest.TestCase):
    """Chỉ handler stdlib được giữ file; loguru phải đi qua handler đó.

    Trước đây loguru tự mở cùng file và xoay vòng bằng `os.rename`. Trên Windows
    handle của stdlib chặn lại, nên mỗi dòng loguru sau ngưỡng đều bị **bỏ rơi**
    kèm traceback PermissionError. Ba test dưới ghim lại hành vi đúng.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "lumi.log"
        logstore.install(str(self.log), max_bytes=8_000, backups=2)
        self.addCleanup(logstore.install, str(self.tmp / "cleanup.log"))
        try:
            from gemini_webapi.utils.logger import logger as glog
        except Exception:  # pragma: no cover - thư viện chưa cài
            self.skipTest("gemini_webapi chưa cài")
        self.glog = glog

    def _files(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("lumi"))

    def test_loguru_records_reach_the_file(self):
        self.glog.info("dong-tu-loguru-{} {}", "A" * 200, "B" * 200)
        logstore._handler.flush()
        text = self.log.read_text(encoding="utf-8", errors="replace")
        self.assertIn("dong-tu-loguru", text)
        self.assertNotIn("Logging error", text)

    def test_rotation_keeps_recent_backups(self):
        for i in range(80):
            self.glog.info("vong {} {}", i, "x" * 300)
        logstore._handler.flush()
        names = self._files()
        self.assertIn("lumi.log.1", names)
        self.assertIn("lumi.log", names)
        self.assertLessEqual(len(names), 3, f"quá nhiều file log: {names}")
        # File mới nhất phải là lumi.log, còn bản cũ đã được đổi tên.
        self.assertIn("vong 79", self.log.read_text(encoding="utf-8", errors="replace"))

    def test_reopen_resets_position_after_clear(self):
        for i in range(40):
            self.glog.info("truoc-clear {} {}", i, "y" * 200)
        logstore._handler.flush()
        with self.log.open("w", encoding="utf-8"):
            pass
        logstore.reopen()
        self.glog.info("sau-clear")
        logstore._handler.flush()
        raw = self.log.read_bytes()
        self.assertNotIn(b"\x00", raw, "file có byte NUL: chưa mở lại handler sau khi cắt")
        self.assertIn("sau-clear", raw.decode("utf-8", errors="replace"))


class TestLogFollower(unittest.TestCase):
    """Các tình huống làm bảng log realtime im lặng nếu xử lý sai."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "lumi.log"
        self.log.write_text("dong 1\n", encoding="utf-8")
        self.f = logstore.Follower(self.log)
        self.f.read_new()  # bám đuôi file như một client vừa mở panel

    def test_only_new_lines_are_pushed(self):
        self.assertEqual(self.f.read_new(), [])  # mở ở cuối file, không lặp lại lịch sử
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("dong moi\n")
        self.assertEqual(self.f.read_new(), ["dong moi"])

    def test_sees_lines_after_log_cleared(self):
        # Nút "Xoá log" cắt file về 0 byte; vị trí đọc còn ở offset cũ nên nếu
        # không quay lại đầu thì panel im lặng tới khi file mọc lại vài KB.
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("truoc khi xoa\n")
        self.assertEqual(self.f.read_new(), ["truoc khi xoa"])
        self.log.write_text("", encoding="utf-8")
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("sau khi xoa\n")
        self.assertEqual(self.f.read_new(), ["sau khi xoa"])

    def test_sees_lines_after_rotation_keeps_same_name(self):
        # loguru xoay vòng bằng cách đổi tên file cũ rồi tạo file mới CÙNG TÊN.
        # So tên file không phát hiện được, nên follower phải so inode.
        self.assertEqual(self.f.read_new(), [])
        self.log.rename(self.tmp / "lumi.log.1")
        self.log.write_text("sau khi xoay von\n", encoding="utf-8")
        self.assertEqual(self.f.read_new(), ["sau khi xoay von"])

    def test_redacts_lines_it_pushes(self):
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("cookie __Secure-1PSID=leakme\n")
        out = self.f.read_new()
        self.assertNotIn("leakme", "\n".join(out))
        self.assertIn("__Secure-1PSID=", "\n".join(out))

    def test_missing_file_is_not_fatal_and_recovers(self):
        log = self.tmp / "chua-co.log"
        f = logstore.Follower(log)
        self.assertEqual(f.read_new(), [])
        log.write_text("xinh chao\n", encoding="utf-8")
        self.assertEqual(f.read_new(), ["xinh chao"])  # file xuất hiện sau cũng đọc được

    def test_close_is_idempotent(self):
        self.f.read_new()
        self.f.close()
        self.f.close()
        self.assertEqual(self.f._pos, 0)

    def test_does_not_hold_file_open(self):
        # Windows: file đang mở mà không FILE_SHARE_DELETE thì loguru không
        # rename được để xoay vòng -> panel đang xem log sẽ làm hỏng logging.
        self.f.read_new()
        self.log.rename(self.tmp / "lumi.log.1")  # nổi nếu còn handle mở


class TestAdminAuth(unittest.TestCase):
    """Kiểm tra hàng rào của panel: token sai, thiếu token, Origin lạ."""

    def setUp(self):
        self._old = (admin_api._admin_token, admin_api._base_url, admin_api._config_path)
        admin_api.configure("secret-token", "http://127.0.0.1:8000")
        self.addCleanup(self._restore)

    def _restore(self):
        admin_api._admin_token, admin_api._base_url, admin_api._config_path = self._old

    class _Req:
        def __init__(self, origin: str = "") -> None:
            self.headers = {"origin": origin} if origin else {}

    def test_accepts_correct_token(self):
        admin_api.require_admin(self._Req(), "secret-token")

    def test_rejects_missing_token(self):
        with self.assertRaises(Exception) as ctx:
            admin_api.require_admin(self._Req(), None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_rejects_wrong_token(self):
        with self.assertRaises(Exception) as ctx:
            admin_api.require_admin(self._Req(), "sai")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_rejects_foreign_origin_even_with_valid_token(self):
        # Đây là hàng rào chặn trang web bất kỳ trong Chrome gọi vào localhost.
        with self.assertRaises(Exception) as ctx:
            admin_api.require_admin(self._Req("https://evil.example"), "secret-token")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_accepts_own_origin(self):
        admin_api.require_admin(self._Req("http://127.0.0.1:8000"), "secret-token")

    def test_disabled_when_token_empty(self):
        admin_api.configure("", "http://127.0.0.1:8000")
        with self.assertRaises(Exception) as ctx:
            admin_api.require_admin(self._Req(), "bat-ky")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_every_api_route_is_guarded(self):
        api_routes = [r for r in admin_api.router.routes if r.path.startswith("/admin/api/")]
        self.assertGreaterEqual(len(api_routes), 10)
        for r in api_routes:
            self.assertTrue(
                any(getattr(d, "dependency", None) is admin_api.require_admin
                    for d in r.dependencies),
                f"{r.path} không có dependency auth",
            )

    def test_config_patch_rejects_non_object(self):
        import asyncio

        class _Req:
            headers: dict = {}

            async def json(self):
                return ["khong", "phai", "object"]

        with self.assertRaises(Exception) as ctx:
            asyncio.run(admin_api.patch_config(_Req()))
        self.assertEqual(ctx.exception.status_code, 400)


class TestBrokenConfig(unittest.TestCase):
    """config.json hỏng phải trả lỗi, tuyệt đối không treo request.

    Trước đây `ConfigError` (là `ValueError`) lọt thẳng ra ngoài route: FastAPI
    không có handler cho nó, request treo vô hạn và panel im lặng. Mọi route đọc
    config phải đi qua `_load_cfg()`.
    """

    def setUp(self):
        self._old = (admin_api._config_path, logstore._log_path)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cfg = Path(tmp) / "config.json"
        admin_api._config_path = self.cfg
        logstore._log_path = Path(tmp) / "lumi.log"
        self.addCleanup(self._restore)

    def _restore(self):
        admin_api._config_path, logstore._log_path = self._old

    def test_load_cfg_turns_config_error_into_http_500(self):
        import asyncio

        from fastapi import HTTPException

        self.cfg.write_text(json.dumps({"port": "khong-phai-so"}), encoding="utf-8")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(admin_api._load_cfg())
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("không hợp lệ", ctx.exception.detail)

    def test_config_endpoint_shows_broken_file_instead_of_hanging(self):
        import asyncio

        # Nội dung rác nhưng đọc được: người dùng cần thấy file để sửa.
        self.cfg.write_text(json.dumps({"port": "khong-phai-so", "api_key": "sk-secret-1234567890"}),
                            encoding="utf-8")
        out = asyncio.run(admin_api.get_config())
        self.assertIs(out["valid"], False)
        self.assertTrue(out["error"])
        self.assertNotIn("sk-secret-1234567890", json.dumps(out))

    def test_config_endpoint_reports_valid_file(self):
        import asyncio

        self.cfg.write_text(json.dumps({"port": 8123}), encoding="utf-8")
        out = asyncio.run(admin_api.get_config())
        self.assertIs(out["valid"], True)
        self.assertIsNone(out["error"])


class TestLogConfigIsActuallyRead(unittest.TestCase):
    """`logging.file` trong config.json phải có tác dụng.

    `server.py` đọc dict thô của config.json. Trước đây nó lấy
    `CONFIG.get("log_file")` ở cấp gốc, trong khi schema đặt đường dẫn ở
    `logging.file` -> giá trị luôn rỗng, server lặng lẽ ghi `logs/lumi.log`, và
    bằng chứng là cả bộ test cũng viết nhầm vào file log thật của người dùng.
    """

    def _log_settings(self, config_dict: dict):
        import server

        return server.log_settings(config_dict)

    def test_logging_section_is_read_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ban-rieng.log"
            got = self._log_settings({"logging": {"file": str(target), "level": "DEBUG"}})
            self.assertEqual(got["file"], str(target))
            self.assertEqual(got["level"], "DEBUG")
            self.assertIs(got["enabled"], True)

    def test_rotation_settings_come_from_config(self):
        got = self._log_settings({"logging": {"max_bytes": 1234, "backups": 7}})
        self.assertEqual(got["max_bytes"], 1234)
        self.assertEqual(got["backups"], 7)

    def test_defaults_when_section_missing(self):
        got = self._log_settings({"host": "127.0.0.1"})
        self.assertEqual(got["file"], "logs/lumi.log")
        self.assertEqual(got["level"], "INFO")
        self.assertIs(got["enabled"], True)

    def test_enabled_false_is_respected(self):
        got = self._log_settings({"logging": {"enabled": False}})
        self.assertIs(got["enabled"], False)

    def test_config_store_summary_agrees_with_server(self):
        # `summary()` xuất phẳng `log_file`; tray đọc key này. Hai bên phải
        # cùng một giá trị, nếu không tray sẽ báo sai đường dẫn log.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(
                {"logging": {"file": "logs\\rieng.log", "level": "WARNING"}}), encoding="utf-8")
            cfg = config_store.load(path)
            summary = config_store.summary(cfg)
            self.assertEqual(summary["log_file"], "logs\\rieng.log")
            self.assertEqual(self._log_settings({"logging": {"level": "WARNING"}})["level"], "WARNING")


class TestBrowserRouteGuards(unittest.TestCase):
    """Cả `stop` và `reset` đều phải chặn Chrome User Data mặc định.

    Hai route này từng là con dao hai lưỡi: nếu lọt, lệnh sẽ tắt (xoá session)
    Chrome cá nhân của người dùng - thứ không thể hoàn tác.
    """

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cfg = Path(tmp) / "config.json"
        self.cfg.write_text(json.dumps(
            {"browser": {"user_data_dir": str(chrome_default_root()), "enabled": True}}
        ), encoding="utf-8")
        self._old = (admin_api._config_path, logstore._log_path)
        admin_api._config_path = self.cfg
        logstore._log_path = Path(tmp) / "lumi.log"
        self.addCleanup(lambda: (setattr(admin_api, "_config_path", self._old[0]),
                                 setattr(logstore, "_log_path", self._old[1])))

    def test_browser_stop_refuses_default_user_data_dir(self):
        import asyncio

        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(admin_api.browser_stop())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Chrome cá nhân", ctx.exception.detail)

    def test_browser_reset_refuses_default_user_data_dir(self):
        import asyncio
        import json as _json

        from fastapi import HTTPException

        body = _json.dumps({"confirm": "xoa"}).encode()
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(admin_api.browser_reset(_FakeRequest(body)))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Chrome cá nhân", ctx.exception.detail)


class TestUvicornLogging(unittest.TestCase):
    """Access log của uvicorn phải vào được file log.

    Uvicorn đặt `propagate=False` cho `uvicorn.access`/`uvicorn.error`, nên handler
    gắn ở root không bao giờ thấy access log — bảng log realtime im lặng trong
    khi file vẫn có dòng. `attach_uvicorn()` sửa đúng chỗ đó.
    """

    def test_attach_uvicorn_adds_file_handler_to_uvicorn_loggers(self):
        import logging

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        log = tmp / "lumi.log"
        root = logging.getLogger()
        old_level = root.level
        old_propagate = {n: logging.getLogger(n).propagate for n in
                         ("uvicorn", "uvicorn.error", "uvicorn.access")}
        logstore.install(str(log), level="INFO")
        self.addCleanup(logstore.install, str(tmp / "cleanup.log"))
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(lambda: [setattr(logging.getLogger(n), "propagate", p)
                                 for n, p in old_propagate.items()])
        # Mức đếm từ root: `install()` set root=INFO trước khi `attach_uvicorn()`
        # chạy, nên test phải mô phỏng đúng thứ tự đó.
        root.setLevel(logging.INFO)

        logstore.attach_uvicorn("INFO")
        shared = logstore._handler
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            self.assertTrue(
                any(getattr(h, "_lumi_file_handler", False) for h in lg.handlers),
                f"{name} chua co handler ghi file",
            )
            self.assertFalse(lg.propagate, f"{name} se ghi trung x2 qua root")
            # Phải dùng CHUNG một instance: mỗi logger giữ handle riêng thì xoay
            # vòng lại thành cuộc đấu giữ file giữa các handler trên Windows.
            self.assertIn(shared, lg.handlers, f"{name} giu handle rieng")

        logging.getLogger("uvicorn.access").info('127.0.0.1:1 - "GET /v1/models HTTP/1.1" 401')
        for h in list(logging.getLogger("uvicorn.access").handlers):
            h.flush()
        self.assertIn("GET /v1/models", log.read_text(encoding="utf-8"))

    def test_attach_uvicorn_is_idempotent(self):
        import logging

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        logstore.install(str(tmp / "lumi.log"))
        self.addCleanup(logstore.install, str(tmp / "cleanup.log"))
        logstore.attach_uvicorn("INFO")
        logstore.attach_uvicorn("INFO")
        n = sum(1 for h in logging.getLogger("uvicorn.access").handlers
                if getattr(h, "_lumi_file_handler", False))
        self.assertEqual(n, 1, "gọi hai lần phải không dồn handler")


if __name__ == "__main__":
    unittest.main()
