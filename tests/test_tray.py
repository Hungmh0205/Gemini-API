"""Test cho supervisor trong tray.py.

Toàn bộ dùng HTTP server giả trên cổng cục bộ và socket tạm - không spawn
server.py thật, không đụng Chrome, không đụng config thật. Những thứ cần spawn
tiến trình thật (vòng restart 75, menu) nằm ở script smoke riêng.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

import tray  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """HTTP server giả trả /health kiểu Lumi, tự tắt được."""

    def __init__(self, payload: dict | None = None, status: int = 200):
        self.payload = payload if payload is not None else {"status": "ok", "model": "gemini-flash"}
        self.status = status
        self.seen_token: str | None = None
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self):
                outer.seen_token = self.headers.get("X-Lumi-Admin")
                outer.hits.append(self.path)
                body = json.dumps(outer.payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _reply
            do_POST = _reply

            def log_message(self, *a):
                pass

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def settings(self) -> dict:
        return {
            "host": "127.0.0.1", "port": self.port,
            "base_url": f"http://127.0.0.1:{self.port}",
            "token": "", "path": Path("config.json"),
        }

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def write_config(path: Path, token: str = "tok-abc-1234567890", port: int = 0) -> None:
    path.write_text(json.dumps({
        "host": "127.0.0.1", "port": port or free_port(),
        "admin": {"enabled": True, "token": token},
    }), encoding="utf-8")


class TestReadSettings(unittest.TestCase):
    def test_reads_config_from_lumi_config_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            write_config(cfg, token="tok-tu-file", port=8321)
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                os.environ.pop("HOST", None)
                os.environ.pop("PORT", None)
                s = tray.read_settings()
            self.assertEqual(s["port"], 8321)
            self.assertEqual(s["token"], "tok-tu-file")
            self.assertEqual(s["path"], cfg)

    def test_env_host_port_win_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            write_config(cfg, port=8322)
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg), "HOST": "0.0.0.0",
                                              "PORT": "9999"}, clear=False):
                s = tray.read_settings()
            self.assertEqual((s["host"], s["port"]), ("0.0.0.0", 9999))

    def test_survives_broken_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{ khong phai json", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                os.environ.pop("HOST", None)
                os.environ.pop("PORT", None)
                s = tray.read_settings()
            self.assertEqual(s["host"], "127.0.0.1")
            self.assertEqual(s["port"], 8000)

    def test_survives_non_numeric_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"host": "127.0.0.1", "port": "sai"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                os.environ.pop("PORT", None)
                self.assertEqual(tray.read_settings()["port"], 8000)


class TestRestartPolicy(unittest.TestCase):
    def setUp(self):
        self.s = tray.ServerProcess({"host": "127.0.0.1", "port": 1, "base_url": "x",
                                     "token": "", "path": Path("config.json")})

    def test_stopping_means_do_not_restart(self):
        self.s.stopping = True
        self.assertFalse(self.s.should_restart(1))

    def test_rate_limit_stops_crash_loop(self):
        n = tray.MAX_FAST_RESTARTS
        for _ in range(n):
            self.assertTrue(self.s.should_restart(1))
        self.assertFalse(self.s.should_restart(1), "phải dừng sau khi vượt giới hạn")
        self.assertIn("thoát", self.s.last_error)

    def test_old_restarts_age_out_of_window(self):
        for _ in range(tray.MAX_FAST_RESTARTS):
            self.s.should_restart(1)
        self.assertFalse(self.s.should_restart(1))
        # Đẩy lịch sử ra ngoài cửa sổ -> được phép thử lại.
        self.s.restarts = [t - tray.FAST_RESTART_WINDOW - 1 for t in self.s.restarts]
        self.assertTrue(self.s.should_restart(1))


class TestAdoptExistingServer(unittest.TestCase):
    def test_adopt_accepts_our_server(self):
        fake = FakeServer()
        self.addCleanup(fake.stop)
        s = tray.ServerProcess(fake.settings)
        self.assertTrue(s.adopt())
        self.assertTrue(s.external)
        self.assertTrue(s.alive(), "server giả đang chạy thì phải báo sống")

    def test_adopt_refuses_foreign_server(self):
        fake = FakeServer(payload={"mot_chuong_trinh_khac": 1})
        self.addCleanup(fake.stop)
        self.assertFalse(tray.ServerProcess(fake.settings).adopt())

    def test_adopt_refuses_dead_port(self):
        s = tray.ServerProcess({"host": "127.0.0.1", "port": free_port(), "base_url": "x",
                                "token": "", "path": Path("config.json")})
        self.assertFalse(s.adopt())
        self.assertFalse(s.external)

    def test_wait_returns_when_adopted_server_disappears(self):
        fake = FakeServer()
        s = tray.ServerProcess(fake.settings)
        self.assertTrue(s.adopt())
        threading.Timer(0.3, fake.stop).start()
        self.assertEqual(s.wait(), 0)

    def test_start_clears_adopt_state(self):
        fake = FakeServer()
        self.addCleanup(fake.stop)
        s = tray.ServerProcess(fake.settings)
        s.adopt()
        s.idle = True
        s.stopping = True
        # start() sẽ spawn thật -> chặn lại Popen để test không mở tiến trình.
        with mock.patch.object(tray.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            s.start()
        self.assertFalse(s.external)
        self.assertFalse(s.idle)
        self.assertFalse(s.stopping)


class TestStopViaAdmin(unittest.TestCase):
    def test_uses_token_rotated_on_disk(self):
        """Token cũ trong RAM thì endpoint từ chối -> kill cưỡng bức Chrome.

        Sau khi người dùng xoay token trên panel, tray phải đọc token mới từ
        config, nếu không mọi lần dừng server đều rơi về kill thẳng.
        """
        fake = FakeServer()
        self.addCleanup(fake.stop)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            write_config(cfg, token="token-moi-abcdef", port=fake.port)
            stale = dict(fake.settings, token="token-cu-abcdef", path=cfg)
            s = tray.ServerProcess(stale)
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                self.assertTrue(s._ask_stop_via_admin())
            self.assertEqual(fake.seen_token, "token-moi-abcdef")

    def test_returns_false_when_no_server_there(self):
        s = tray.ServerProcess({"host": "127.0.0.1", "port": free_port(),
                                "base_url": f"http://127.0.0.1:{free_port()}",
                                "token": "", "path": Path("config.json")})
        self.assertFalse(s._ask_stop_via_admin())


class TestOpenPanelToken(unittest.TestCase):
    def test_panel_url_uses_token_rotated_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            write_config(cfg, token="token-moi-abcdef", port=8000)
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                with mock.patch.object(tray.webbrowser, "open") as opened:
                    tray.open_panel({"base_url": "http://127.0.0.1:8000", "token": "cu", "path": cfg})
            self.assertEqual(opened.call_args[0][0], "http://127.0.0.1:8000/admin#t=token-moi-abcdef")

    def test_panel_url_without_token_has_no_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            write_config(cfg, token="")
            with mock.patch.dict(os.environ, {"LUMI_CONFIG": str(cfg)}, clear=False):
                with mock.patch.object(tray.webbrowser, "open") as opened:
                    tray.open_panel({"base_url": "http://127.0.0.1:8000", "token": "", "path": cfg})
            self.assertEqual(opened.call_args[0][0], "http://127.0.0.1:8000/admin")


class TestLoggingSurvivesEveryConsole(unittest.TestCase):
    """Supervisor không được chết vì muốn ghi một dòng log.

    Đã xảy ra thật khi chạy tách rời: `tray.py` in "icon tray đã sẵn sàng" ra
    console cp1252, UnicodeEncodeError làm cả tiến trình chết *trước khi* bật
    server - và pythonw nuốt mất traceback nên nhìn từ ngoài chỉ thấy "cổng
    không phản hồi". Hai test dưới ghim đúng điều đó.
    """

    def test_log_never_raises_on_ascii_only_stream(self):
        class Legacy:  # file object kiểu cũ: không có reconfigure, và chỉ nhận ASCII
            def __init__(self):
                self.parts = []

            def write(self, text):
                text.encode("ascii")  # giống console cp1252: ký tự lạ là nổ
                self.parts.append(text)

            def flush(self):
                pass

        out, err = Legacy(), Legacy()
        with mock.patch.object(tray.sys, "stdout", out), \
                mock.patch.object(tray.sys, "stderr", err):
            tray.harden_streams()
            tray._log("server con da thoat")  # ASCII: vẫn phải ghi được
            tray._log("icon tray đã sẵn sàng")  # tiếng Việt: không được ném lỗi ra
        self.assertTrue(any("server con da thoat" in p for p in out.parts))

    def test_log_never_raises_when_streams_are_none(self):
        # pythonw.exe: sys.stdout và sys.stderr đều là None.
        with mock.patch.object(tray.sys, "stdout", None), \
                mock.patch.object(tray.sys, "stderr", None):
            tray.harden_streams()
            tray._log("icon tray đã sẵn sàng")
        self.assertIsNotNone(tray.sys.stdout)
        self.assertIsNotNone(tray.sys.stderr)

    def test_log_swallows_broken_stream(self):
        class Broken:
            def write(self, _):
                raise OSError("pipe đã đóng")

            def flush(self):
                pass

        with mock.patch.object(tray.sys, "stdout", Broken()), \
                mock.patch.object(tray.sys, "stderr", Broken()):
            tray._log("vẫn phải sống")


class TestTrayMenu(unittest.TestCase):
    def test_menu_cannot_stop_and_quit_at_once(self):
        # "Dừng server" giữ icon, còn "Thoát" mới tắt icon. Nếu chúng gọi chung
        # một hàm thì "Dừng server" sẽ giết luôn tray.
        self.assertIsNot(tray.stop_from_menu, tray.quit_everything)

    def test_stop_from_menu_leaves_quitting_false(self):
        fake = FakeServer()
        self.addCleanup(fake.stop)
        s = tray.ServerProcess(fake.settings)
        s.adopt()
        tray.stop_from_menu(s)
        self.assertTrue(s.idle, "dừng server thì phải rảnh, chờ bật lại")
        self.assertFalse(s.quitting, "'Dừng server' không được thoát tray")

    def test_quit_sets_quitting_and_idle_false(self):
        fake = FakeServer()
        self.addCleanup(fake.stop)
        s = tray.ServerProcess(fake.settings)
        s.adopt()
        icon = mock.Mock()
        tray.quit_everything(s, icon)
        self.assertTrue(s.quitting)
        icon.stop.assert_called_once()

    def test_restart_from_menu_clears_rate_limit(self):
        s = tray.ServerProcess({"host": "127.0.0.1", "port": 1, "base_url": "x",
                                "token": "", "path": Path("config.json")})
        for _ in range(tray.MAX_FAST_RESTARTS):
            s.should_restart(1)
        with mock.patch.object(s, "start") as start, mock.patch.object(s, "terminate"):
            tray.restart_server(s)
        start.assert_called_once()
        self.assertEqual(s.restarts, [], "restart từ menu phải xoá bộ đếm crash")
        self.assertFalse(s.idle)

    @unittest.skipUnless(tray.HAVE_TRAY, "chưa cài pystray")
    def test_run_tray_wires_all_menu_actions(self):
        import pystray

        captured: dict = {}

        class FakeIcon:
            def __init__(self, name, icon, title, menu=None):
                captured["menu"] = menu

            def run(self):
                pass  # không tạo icon thật trong test

            def stop(self):
                pass

        s = tray.ServerProcess({"host": "127.0.0.1", "port": 1, "base_url": "x",
                                "token": "", "path": Path("config.json")})
        with mock.patch.object(tray, "Icon", FakeIcon), \
             mock.patch.object(tray, "supervise", return_value=0):
            tray.run_tray(s, {"base_url": "http://127.0.0.1:1", "token": ""})
        labels = [i.text for i in captured["menu"].items if isinstance(i, pystray.MenuItem)]
        for want in ("Mở bảng điều khiển", "Khởi động server",
                     "Khởi động lại server", "Dừng server", "Thoát"):
            self.assertIn(want, labels)


if __name__ == "__main__":
    unittest.main()
