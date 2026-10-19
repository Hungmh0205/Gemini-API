"""Regression cho vòng đời slot streaming của `/v1/chat/completions`.

Bug gốc: `chat_completions` giữ semaphore suốt request để reload chỉ đóng client
cũ khi hết người dùng. Với `stream=True`, semaphore được `async with` BÊNGOAI
generator, và `_ReleasingStreamingResponse` giữ việc trả slot lúc `__call__` kết
thúc. Trước khi có lớp đó, khi client rớt kết nối đúng giữa lúc gửi
`http.response.start`, Starlette raise trước khi background task kịp chạy:
generator chưa từng được iterate nên `finally` trong generator không vào, và
background task `stream_response` cũng chưa tới lượt -> semaphore mất vĩnh viễn.
Đủ vài request là server trả 429 hỏng cho tới khi restart.

Các test dưới đây gọi thẳng response object với `send` giả, nên phản ánh đúng
thứ tự message mà ASGI server thật dùng mà không cần dựng socket.
"""

import asyncio
import sys
import unittest
from pathlib import Path

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

os_import = __import__("os")
os_import.environ.setdefault("GEMINI_API_KEY", "test-key-for-tests")

import server  # noqa: E402


def _scope() -> dict:
    return {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}


def _make_receive():
    """Trả về coroutine function giả lập vòng đời receive của một client.

    Quan trọng: `listen_for_disconnect` lặp `while True` chừng nào chưa thấy
    `http.disconnect`. Nếu `receive` cứ trả `http.request`, task đó quay vô hạn
    và task group của Starlette không bao giờ kết thúc (test treo, không fail).
    """
    state = {"n": 0}

    async def receive() -> dict:
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class _SendThatDropsClient:
    """Client rớt kết nối ngay ở message đầu tiên (`http.response.start`).

    Đây là kịch bản chết người: generator chưa được iterate lần nào.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.sent.append(message)
        raise ConnectionResetError("client đã đóng kết nối")


def _is_client_gone(exc: BaseException) -> bool:
    """anyio gom lỗi của task group thành ExceptionGroup, nên phải dò bên trong."""
    if isinstance(exc, ConnectionResetError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_client_gone(e) for e in exc.exceptions)
    return False


class TestStreamingReleasesSlot(unittest.IsolatedAsyncioTestCase):
    """Slot phải về đúng một lần, trên mọi đường thoát của response."""

    def setUp(self) -> None:
        self.guard = server._ConcurrencyGuard(max_concurrent=1, max_queue=1, queue_timeout=1.0)

    def _response(self, gen, release):
        return server._ReleasingStreamingResponse(gen, release=release)

    async def test_disconnect_before_first_byte_still_releases_slot(self):
        released = 0

        async def release() -> None:
            nonlocal released
            released += 1

        async def gen():
            yield b"data: x\n\n"  # type: ignore[misc]

        resp = self._response(gen(), release)
        await self.guard.__aenter__()  # giả lập slot chat_completions đang giữ
        self.assertEqual(self.guard._active, 1)

        with self.assertRaises(BaseException) as ctx:
            await resp(_scope(), _make_receive(), _SendThatDropsClient())
        self.assertTrue(_is_client_gone(ctx.exception))

        self.assertEqual(released, 1, "release phải chạy dù client chết ở message đầu")
        await self.guard.__aexit__(None, None, None)
        self.assertEqual(self.guard._active, 0)

    async def test_guard_slot_available_again_after_disconnect(self):
        """Hành vi quan sát được: sau khi client rớt, request sau vẫn chạy được."""
        async def release() -> None:
            return None

        async def gen():
            yield b"data: x\n\n"  # type: ignore[misc]

        await self.guard.__aenter__()
        with self.assertRaises(BaseException) as ctx:
            await self._response(gen(), release)(_scope(), _make_receive(), _SendThatDropsClient())
        self.assertTrue(_is_client_gone(ctx.exception))
        await self.guard.__aexit__(None, None, None)

        async with self.guard:  # không bị _QueueTimeout -> đã trả slot
            self.assertEqual(self.guard._active, 1)

    async def test_normal_completion_releases_once(self):
        released = 0

        async def release() -> None:
            nonlocal released
            released += 1

        async def gen():
            yield b"data: [DONE]\n\n"  # type: ignore[misc]

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        resp = self._response(gen(), release)
        await resp(_scope(), _make_receive(), send)

        self.assertEqual(released, 1)
        self.assertTrue(any(m["type"] == "http.response.start" for m in sent))
        self.assertTrue(any(m.get("body") for m in sent), "phải có body đã gửi đi")

    async def test_error_inside_generator_still_releases_slot(self):
        released = 0

        async def release() -> None:
            nonlocal released
            released += 1

        async def gen():
            raise RuntimeError("lỗi giữa chừng")
            yield b"never"  # pragma: no cover - để gen() là generator thật

        with self.assertRaises(BaseException) as ctx:
            await self._response(gen(), release)(_scope(), _make_receive(), _SendThatDropsClient())
        self.assertTrue(_is_client_gone(ctx.exception))
        self.assertEqual(released, 1)

    async def test_call_invokes_release_exactly_once(self):
        """Hợp đồng của lớp: một lần `__call__` thì đúng một lần release."""
        released = 0

        async def release() -> None:
            nonlocal released
            released += 1

        async def gen():
            yield b"data: x\n\n"  # type: ignore[misc]

        resp = self._response(gen(), release=release)
        await resp(_scope(), _make_receive(), lambda m: asyncio.sleep(0))
        self.assertEqual(released, 1)

    async def test_production_release_pattern_dedupes_two_call_sites(self):
        """Mô phỏng đúng `_release` của `chat_completions`: cờ `released` chặn
        lần gọi thứ hai từ `finally` của generator.

        Cả hai vế đều phải chạy: generator kết thúc bình thường VÀ `__call__` kết
        thúc. Nếu mất cờ này thì semaphore `release()` hai lần -> semaphore bị
        nới quá số, và `max_concurrent` không còn là trần thật.
        """
        guard = self.guard
        calls = {"cleanup": 0, "guard_exit": 0}
        already_released = False

        async def release() -> None:
            nonlocal already_released
            if already_released:
                return
            already_released = True
            calls["cleanup"] += 1
            guard._sem.release()
            calls["guard_exit"] += 1

        await guard.__aenter__()

        async def gen():
            try:
                yield b"data: x\n\n"  # type: ignore[misc]
            finally:
                await release()  # vế 1: finally trong generator

        resp = self._response(gen(), release=release)
        await resp(_scope(), _make_receive(), lambda m: asyncio.sleep(0))  # vế 2
        await resp._release()  # giả lập lần gọi dư

        self.assertEqual(calls["cleanup"], 1)
        self.assertEqual(calls["guard_exit"], 1)


class TestTempFileCleanup(unittest.TestCase):
    """File tạm phải dọn được kể cả khi Windows khoá (PermissionError)."""

    def test_cleanup_swallows_permission_error(self):
        d = server.tmp_dir()
        f = d / "a.bin"
        f.write_bytes(b"x")

        def boom(_):
            raise PermissionError(32, "đang bị khoá")

        orig = server.os.unlink
        server.os.unlink = boom
        try:
            server._cleanup_tmpfiles([d / "a.bin"])
        finally:
            server.os.unlink = orig
        self.assertTrue(f.exists(), "lỗi unlink phải bị nuốt, không ném ra ngoài")

    def test_cleanup_removes_named_files(self):
        d = server.tmp_dir()
        (d / "keep.bin").write_bytes(b"x")
        (d / "gone.bin").write_bytes(b"x")
        server._cleanup_tmpfiles([d / "gone.bin", d / "khong-ton-tai.bin"])
        self.assertFalse((d / "gone.bin").exists())
        self.assertTrue((d / "keep.bin").exists(), "không xoá nhầm file ngoài danh sách")

    def test_cleanup_accepts_path_objects(self):
        d = server.tmp_dir()
        f = d / "p.bin"
        f.write_bytes(b"x")
        server._cleanup_tmpfiles([f])
        self.assertFalse(f.exists())


class TestLoadImageLimits(unittest.IsolatedAsyncioTestCase):
    """Ảnh tải lên phải có trần cả về bộ nhớ lẫn thời gian."""

    async def test_data_uri_too_large_rejected_without_base64_decode(self):
        import base64

        payload = b"\x00" * (server.MAX_IMAGE_BYTES + 1)
        uri = "data:image/png;base64," + base64.b64encode(payload).decode()
        with self.assertRaises(ValueError) as ctx:
            await server._load_image(uri)
        self.assertIn("image", str(ctx.exception).lower())

    async def test_bad_base64_raises_value_error_not_binascii_error(self):
        uri = "data:image/png;base64,!!!khong-hop-le!!!"
        with self.assertRaises(ValueError):
            await server._load_image(uri)


if __name__ == "__main__":
    unittest.main()