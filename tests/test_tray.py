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
import time
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

    def test_slow_crash_loop_is_also_stopped(self):
        """Vòng lặp CHẬM: mỗi lần chạy 200s rồi chết, cách nhau 300s.

        Cửa sổ `FAST_RESTART_WINDOW` không bao giờ đầy trong trường hợp này, nên
        chỉ có trần tổng mới chặn được. Trước khi có nó, tray spawn vô hạn suốt
        đêm với một cấu hình hỏng.
        """
        real = time.monotonic
        clock = {"t": 1000.0}
        with mock.patch.object(tray.time, "monotonic", lambda: clock["t"]):
            stopped_at = None
            for i in range(tray.MAX_TOTAL_RESTARTS + 15):
                clock["t"] += 200 + 300  # chạy 200s, chết, nghỉ 300s
                self.s.started_at = clock["t"] - 200
                self.s.became_healthy = False
                if not self.s.should_restart(1):
                    stopped_at = i + 1
                    break
        time.monotonic = real
        self.assertIsNotNone(stopped_at, "vòng lặp chậm phải bị chặn")
        self.assertLessEqual(stopped_at, tray.MAX_TOTAL_RESTARTS + 1)
        self.assertIn("bật lại", self.s.last_error)

    def test_healthy_run_resets_backoff(self):
        """Đã từng phục vụ được rồi mới hỏng -> không tính là lỗi lặp lại."""
        self.s.consecutive = 12
        self.s.became_healthy = True
        self.s.started_at = time.monotonic() - (tray.HEALTHY_AFTER + 1)
        self.assertTrue(self.s.should_restart(1))
        self.assertEqual(self.s.consecutive, 1)

    def test_long_run_that_never_came_up_does_not_reset(self):
        """Sống 5 phút rồi chết mà chưa từng phục vụ -> vẫn phải đếm.

        Reset theo thời gian sống là một lỗi dễ mắc: server cấu hình sai vẫn
        treo `client_init()` được vài phút rồi mới chết, nên nó sẽ không bao giờ
        bị chặn.
        """
        self.s.consecutive = 12
        self.s.became_healthy = False
        self.s.started_at = time.monotonic() - 300
        self.s.restarts = []
        self.assertTrue(self.s.should_restart(1))
        self.assertEqual(self.s.consecutive, 13)

    def test_backoff_grows_and_is_capped(self):
        seen = []
        for _ in range(10):
            self.s.should_restart(1)
            self.s.restarts = []  # bỏ ràng buộc cửa sổ nhanh
            seen.append(self.s.restart_delay())
        self.assertEqual(seen[0], tray.RESTART_BACKOFF[0])
        self.assertGreater(seen[3], seen[0])
        self.assertLessEqual(max(seen), max(tray.RESTART_BACKOFF))

    def test_reset_backoff_clears_state(self):
        """Người dùng sửa xong bấm menu -> phải thử lại được ngay."""
        self.s.consecutive = tray.MAX_TOTAL_RESTARTS + 5
        self.s.restarts = [time.monotonic()] * 10
        self.assertFalse(self.s.should_restart(1), "trước khi reset thì phải bị chặn")
        self.s.reset_backoff()
        self.assertTrue(self.s.should_restart(1))


class TestHonestStatus(unittest.TestCase):
    """Menu không được mời bấm "Khởi động server" khi server đang khởi động."""

    def setUp(self):
        self.fake = FakeServer()
        self.addCleanup(self.fake.stop)
        self.s = tray.ServerProcess(self.fake.settings)

    def test_running_server_reports_ok(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        self.s.proc = proc
        with mock.patch.object(tray, "health", return_value={"status": "ok"}):
            self.assertEqual(self.s.status(), "ok")
            self.assertEqual(self.s.status_text(), "đang chạy")

    def test_dead_server_reports_stopped(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        self.s.proc = proc
        with mock.patch.object(tray, "health", return_value=None):
            self.assertEqual(self.s.status(), "stopped")
            self.assertFalse(self.s.alive())

    def test_starting_server_is_not_offered_a_duplicate_start(self):
        """Tiến trình còn sống nhưng `/health` chưa mở -> "đang khởi động".

        Nếu gộp thành "đã dừng", menu bật nút "Khởi động server"; bấm là spawn
        thêm một server, cái mới lập tức crash vì cổng đã bị chiếm.
        """
        proc = mock.Mock()
        proc.poll.return_value = None
        self.s.proc = proc
        self.s.started_at = time.monotonic()
        with mock.patch.object(tray, "health", return_value=None):
            self.assertEqual(self.s.status(), "starting")
            self.assertTrue(self.s.alive(), "phải coi là còn sống để không mời bật lại")
            self.assertIn("đang khởi động", self.s.status_text())

    def test_status_text_reports_error_instead_of_pretending_ok(self):
        self.s.last_error = "Server không giữ được trạng thái"
        with mock.patch.object(tray, "health", return_value=None):
            self.assertIn("không giữ được", self.s.status_text())


class TestStartupStateFile(unittest.TestCase):
    """Phân biệt "Lumi đang khởi động" với "chương trình khác chiếm cổng"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "server-startup.json"

    def write(self, data) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_file(self):
        self.assertIsNone(tray.startup_server_age(self.path))

    def test_live_pid_reports_age(self):
        self.write({"pid": os.getpid(), "started_at": time.time()})
        got = tray.startup_server_age(self.path)
        self.assertIsNotNone(got)
        self.assertEqual(got[1], str(os.getpid()))

    def test_dead_pid_ignored(self):
        self.write({"pid": 999999, "started_at": time.time()})
        self.assertIsNone(tray.startup_server_age(self.path),
                          "pid chết thì file chỉ là rác, không phải server đang chạy")

    def test_too_old_ignored(self):
        self.write({"pid": os.getpid(), "started_at": time.time() - 9999})
        self.assertIsNone(tray.startup_server_age(self.path))

    def test_corrupt_file_ignored(self):
        self.path.write_text("{khong phai json", encoding="utf-8")
        self.assertIsNone(tray.startup_server_age(self.path))

    def test_bogus_shapes_ignored(self):
        for bad in ({"pid": "x", "started_at": time.time()},
                    {"pid": os.getpid()},
                    {"started_at": time.time()},
                    {"pid": os.getpid(), "started_at": "x"}):
            with self.subTest(bad=bad):
                self.write(bad)
                self.assertIsNone(tray.startup_server_age(self.path))


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


class TestTrayIconFromLogo(unittest.TestCase):
    """Icon tray lấy từ logo.png, và không bao giờ làm tray chết vì ảnh.

    pystray trên Windows cần ảnh 32bit ARGB có alpha thật; logo.png là PNG
    palette nên phải ép về RGBA. Ba test dưới ghim: kích thước/mode, fallback
    khi ảnh hỏng, và quan trọng nhất là test không được ghi đè lên logo.png thật.
    """

    def setUp(self):
        self._logo = tray.LOGO_PNG
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, tray, "LOGO_PNG", self._logo)

    def test_icon_is_rgba_at_every_size_windows_uses(self):
        for size in (16, 32, 64, 256):
            with self.subTest(size=size):
                img = tray.make_icon_image(size)
                self.assertEqual(img.mode, "RGBA")
                self.assertEqual(img.size, (size, size))
                lo, hi = img.getchannel("A").getextrema()
                self.assertEqual((lo, hi), (0, 255))  # có alpha, không phải ảnh đục

    def test_real_logo_is_not_writable_by_test(self):
        """Chốt hành vi: đổi LOGO_PNG rồi xử lý ảnh hỏng cũng không đụng file thật.

        Đã xảy ra thật: một lần kiểm thử ghi "file hỏng" thẳng lên logo.png của
        người dùng và xoá mất ảnh gốc. Giờ mọi thao tác phải đi qua bản sao tạm.
        """
        copy = Path(self.tmp.name) / "logo.png"
        copy.write_bytes(self._logo.read_bytes())
        before = self._logo.read_bytes()

        tray.LOGO_PNG = copy
        copy.write_bytes(b"khong phai png")  # ảnh hỏng
        img = tray.make_icon_image(64)

        self.assertEqual((img.mode, img.size), ("RGBA", (64, 64)))
        self.assertEqual(self._logo.read_bytes(), before)

    def test_falls_back_when_logo_missing_or_broken(self):
        tray.LOGO_PNG = Path(self.tmp.name) / "khong-co.png"
        self.assertEqual(tray.make_icon_image(64).mode, "RGBA")

        broken = Path(self.tmp.name) / "logo.png"
        broken.write_bytes(b"\x89PNG\r\n\x1a\n hong")
        tray.LOGO_PNG = broken
        img = tray.make_icon_image(64)
        self.assertEqual((img.mode, img.size), ("RGBA", (64, 64)))
        self.assertGreater(img.getchannel("A").getextrema()[1], 0)  # có hình vẽ

    def _use_logo(self, img, name="logo.png"):
        path = Path(self.tmp.name) / name
        img.save(path)
        tray.LOGO_PNG = path

    def test_transparent_margin_is_trimmed_so_small_icons_stay_readable(self):
        """Logo gốc có ~10% viền trong suốt; ở 16px thì phải cắt bớt.

        Không cắt thì nội dung chỉ chiếm ~33% khung ở 16px và icon tray trông
        nhỏ như một vết. Sau khi cắt phải đầy khung hơn đáng kể.
        """
        from PIL import Image, ImageDraw

        src = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        ImageDraw.Draw(src).ellipse((300, 300, 724, 724), fill=(76, 141, 255, 255))
        self._use_logo(src)

        small = tray.make_icon_image(16)
        frac = sum(1 for px in small.get_flattened_data() if px[3] > 8) / (16 * 16)
        self.assertGreater(frac, 0.55, f"chi {frac:.0%} khung co noi dung")

    def test_icon_keeps_aspect_ratio(self):
        """Cắt vuông quanh tâm: nội dung phải giữ tỉ lệ, không bị kéo méo.

        Logo thật có vùng nội dung 824x843 (không vuông). Nếu ép thẳng về 16x16
        thì chiều ngang bị nén ~2% - ở 16px đó là vài pixel lệch. Vẽ một bầu
        dục rộng 300 cao 220 rồi đo 4 khoảng đệm: cắt vuông + canh giữa thì
        trái≈phải, trên≈dưới, và đệm ngang hẹp hơn đệm dọc.
        """
        from PIL import Image, ImageDraw

        src = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        ImageDraw.Draw(src).ellipse((50, 90, 350, 310), fill=(255, 255, 255, 255))
        self._use_logo(src)

        size = 64
        box = tray.make_icon_image(size).getchannel("A").getbbox()
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        ml, mr, mt, mb = left, size - right, top, size - bottom
        self.assertAlmostEqual(ml, mr, delta=2, msg="lệch trái-phải: cắt không vuông/cân giữa")
        self.assertAlmostEqual(mt, mb, delta=2, msg="lệch trên-dưới: cắt không vuông/cân giữa")
        self.assertLess(ml, mt, "bầu dục rộng hơn cao nên đệm ngang phải hẹp hơn")


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
