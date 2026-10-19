"""Regression cho các đường thoát mà server/tray từng treo.

Nhóm test này không kiểm tra tính năng mà là kiểm tra *server còn phản hồi sau khi
hỏng thứ khác*: mỗi endpoint gọi một helper có thể treo vô hạn (PowerShell/WMI,
Chrome kẹt), và nếu endpoint giữ `_browser_lock` trong lúc chờ thì các endpoint
khác xếp hàng vô tận — panel trông như treo.
"""

import asyncio
import contextlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

__import__("os").environ.setdefault("GEMINI_API_KEY", "test-key-for-tests")

import admin_api  # noqa: E402
import chrome_launcher  # noqa: E402
import server  # noqa: E402
import tray  # noqa: E402

_BCFG = {"close_timeout": 25.0}


class TestCloseChromeBounded(unittest.IsolatedAsyncioTestCase):
    """`_close_chrome_bounded` phải LUÔN trả về, không bao giờ treo endpoint."""

    async def test_normal_close_reports_ok(self):
        with mock.patch.object(chrome_launcher, "close_agent_chrome", return_value=True):
            ok, note = await admin_api._close_chrome_bounded(_BCFG, r"C:\udd\Default")
        self.assertTrue(ok)
        self.assertEqual(note, "")

    async def test_soft_failure_reports_not_ok_without_note(self):
        """`close_agent_chrome` trả False = Chrome còn sống, không phải lỗi hệ thống."""
        with mock.patch.object(chrome_launcher, "close_agent_chrome", return_value=False):
            ok, note = await admin_api._close_chrome_bounded(_BCFG, r"C:\udd\Default")
        self.assertFalse(ok)
        self.assertEqual(note, "")

    async def test_hang_becomes_timeout_instead_of_hanging_the_endpoint(self):
        """Giả lập `close_agent_chrome` treo mãi (WMI kẹt)."""
        started = time.monotonic()

        def hang(*_a, **_k):
            time.sleep(30)
            return True

        with mock.patch.object(chrome_launcher, "close_agent_chrome", hang), \
             mock.patch.object(asyncio, "wait_for", _raise_timeout):
            ok, note = await admin_api._close_chrome_bounded({"close_timeout": 0.01}, r"C:\udd\Default")

        self.assertFalse(ok)
        self.assertIn("không phản hồi", note.lower())
        self.assertIn("chrome_launcher.py", note, "phải chỉ ra lệnh để tự xử lý")
        self.assertLess(time.monotonic() - started, 5.0, "phải trả nhanh, không chờ hết 30s")

    async def test_subprocess_timeout_is_reported_not_raised(self):
        import subprocess

        def wmi_timeout(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=15)

        with mock.patch.object(chrome_launcher, "close_agent_chrome", wmi_timeout):
            ok, note = await admin_api._close_chrome_bounded(_BCFG, r"C:\udd\Default")
        self.assertFalse(ok)
        self.assertTrue(note)

    async def test_unexpected_exception_is_caught(self):
        def boom(*_a, **_k):
            raise RuntimeError("hỏng bất ngờ")

        with mock.patch.object(chrome_launcher, "close_agent_chrome", boom):
            ok, note = await admin_api._close_chrome_bounded(_BCFG, r"C:\udd\Default")
        self.assertFalse(ok)
        self.assertIn("hỏng bất ngờ", note)

    async def test_budget_covers_the_close_timeout_plus_process_spawns(self):
        """Ngân sách phải tính cả `close_timeout` lẫn các lần spawn PowerShell.

        `close_agent_chrome` gọi `browser_pids()` (mỗi lần ~0.5s) trước, trong
        vòng chờ và sau khi kill. Trần phải rộng hơn `close_timeout` thì hết giờ
        chính nó sẽ giết một lần đóng đang làm đúng việc.
        """
        seen = {}

        def capture(coro, timeout):
            seen["timeout"] = timeout
            coro.close()
            raise TimeoutError

        with mock.patch.object(chrome_launcher, "close_agent_chrome", return_value=True), \
             mock.patch.object(asyncio, "wait_for", capture):
            await admin_api._close_chrome_bounded({"close_timeout": 25}, "x")

        self.assertGreater(seen["timeout"], 25.0, "phải lớn hơn close_timeout")
        self.assertLessEqual(seen["timeout"], 25.0 + 60.0, "vẫn phải có trần")


def _raise_timeout(coro, timeout):
    """Thay thế `asyncio.wait_for` để mô phỏng hết giờ tức thì."""
    coro.close()
    raise TimeoutError


class TestPidAlive(unittest.TestCase):
    """`_pid_alive` là nền của việc bỏ ~25 lần spawn PowerShell mỗi lần đóng Chrome."""

    def test_current_process_is_alive(self):
        self.assertTrue(chrome_launcher._pid_alive(__import__("os").getpid()))

    def test_nonexistent_pid_is_not_alive(self):
        self.assertFalse(chrome_launcher._pid_alive(999_999))

    def test_invalid_pid_does_not_raise(self):
        for bad in (0, -1, 2**31):
            with self.subTest(pid=bad):
                self.assertFalse(chrome_launcher._pid_alive(bad))


class TestStartupStateRoundTrip(unittest.TestCase):
    """File trạng thái khởi động phải giao tiếp được với chính nó và với tray."""

    def setUp(self) -> None:
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "server-startup.json"
        self._old = server.STARTUP_STATE_FILE
        server.STARTUP_STATE_FILE = self.path
        self.addCleanup(lambda: setattr(server, "STARTUP_STATE_FILE", self._old))

    def test_absent_when_nothing_written(self):
        self.assertIsNone(server.read_startup_state())

    def test_write_then_read(self):
        server._write_startup_state()
        st = server.read_startup_state()
        self.assertIsNotNone(st)
        self.assertEqual(st["pid"], __import__("os").getpid())
        self.assertIn("started_at", st)

    def test_clear_removes_file(self):
        server._write_startup_state()
        self.assertTrue(self.path.is_file())
        server._clear_startup_state()
        self.assertFalse(self.path.exists())
        self.assertIsNone(server.read_startup_state())

    def test_clear_is_safe_when_missing(self):
        server._clear_startup_state()  # không có file -> không được ném

    def test_stale_entry_is_rejected(self):
        self.path.write_text(
            json.dumps({"pid": __import__("os").getpid(), "started_at": time.time() - 10_000}),
            encoding="utf-8",
        )
        self.assertIsNone(server.read_startup_state(), "quá cũ thì coi như rác")

    def test_partial_write_never_leaves_broken_file(self):
        """Ghi bằng file tạm rồi `replace` — tray không bao giờ đọc trúng nửa file."""
        server._write_startup_state()
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"còn sót file tạm: {leftovers}")

    def test_write_failure_does_not_raise(self):
        """Mất file đánh dấu chỉ mất tiện ích adopt, không được giết server."""
        with mock.patch.object(server, "STARTUP_STATE_FILE", self.path.parent / "het\\quyen.md"):
            server._write_startup_state()  # phải nuốt lỗi


class TestConfigValidation(unittest.TestCase):
    """Config sai kiểu phải được báo, chứ không chạy âm thầm bằng giá trị rác."""

    def test_valid_config_has_no_error(self):
        self.assertIsNone(server._validate_config({"port": 8000}))

    def test_wrong_type_is_reported_with_field_name(self):
        err = server._validate_config({"max_concurrent": "khong phai so"})
        self.assertIsNotNone(err)
        self.assertIn("max_concurrent", err)

    def test_non_positive_number_is_reported(self):
        self.assertIsNotNone(server._validate_config({"queue_timeout": 0}))

    def test_init_timeout_validated(self):
        self.assertIsNone(server._validate_config({"init_timeout": 90}))
        self.assertIsNotNone(server._validate_config({"init_timeout": -1}))

    def test_unknown_keys_do_not_break_startup(self):
        """Key lạ phải bị bỏ qua, không chặn server khởi động."""
        self.assertIsNone(server._validate_config({"khoa_bia_ca_nhan": 1}))


class TestBackgroundTasksSurviveFirstLoop(unittest.IsolatedAsyncioTestCase):
    """Task nền phải sống sót qua vòng lặp đầu tiên.

    `auth_watchdog()` từng gán `_last_auth_mtime` mà không khai báo `global`, nên
    Python coi đó là biến local và lần lặp đầu tiên ném `UnboundLocalError`. Task
    chết lặng lẽ trong `lifespan` — không có ai nhận, không có traceback nào trên
    console — và việc phát hiện `gemini-auth.json` đổi trên đĩa không bao giờ
    chạy. Test gọi thẳng hàm với timeout ngắn để bắt đúng việc đó.
    """

    async def test_auth_watchdog_does_not_crash_on_first_iteration(self):
        task = asyncio.create_task(server.auth_watchdog())
        try:
            await asyncio.sleep(2.5)  # > 2.0s của sleep đầu tiên trong vòng lặp
            self.assertFalse(task.done(), "watchdog chết ở vòng lặp đầu")
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_auth_watchdog_reads_the_module_level_mtime(self):
        """Đọc phải ra giá trị module, không phải biến local chưa gán."""
        original = server._last_auth_mtime
        server._last_auth_mtime = -1.0
        seen: list[float] = []

        async def fake_reload() -> None:
            seen.append(server._last_auth_mtime)

        try:
            with mock.patch.object(server, "_request_reload", fake_reload):
                task = asyncio.create_task(server.auth_watchdog())
                await asyncio.sleep(2.5)
                self.assertFalse(task.done())
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.assertEqual(len(seen), 1, "phải phát hiện được lần đổi đầu tiên")
            # Watchdog gán global TRƯỚC khi gọi reload, nên giá trị ở đây là mtime
            # thật của file, không phải giá trị cũ -1.0. Việc cần chứng minh là
            # nó đọc/gán được biến module (trước đây UnboundLocalError).
            self.assertAlmostEqual(seen[0], server.AUTH_FILE.stat().st_mtime, delta=1)
            self.assertNotEqual(seen[0], -1.0)
        finally:
            server._last_auth_mtime = original


if __name__ == "__main__":
    unittest.main()