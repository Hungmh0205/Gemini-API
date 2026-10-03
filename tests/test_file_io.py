"""Test cho luồng file hai chiều: nhận file tùy ý và trả media.

Bốn nhóm:
  1. `_iter_files` đọc được các kiểu part mà client khác nhau gửi.
  2. `_safe_filename` chặn traversal / ký tự điều khiển / tên reserved.
  3. `_prepare_files` giữ đúng tên gốc (lý do: `parse_file_name()` của thư viện
     lấy `Path.name`, mất tên thì Gemini không đọc được file).
  4. Endpoint `/v1/files/{id}` bắt buộc API key và chặn path traversal.

Lưu ý cho người đọc sau: `/v1/files/` phục vụ ảnh do Gemini sinh, tức dữ liệu
của tài khoản Google thật — nên không được bỏ `Depends(require_key)`.
"""

import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-files")

import server  # noqa: E402


def _data_url(payload: bytes, mime: str = "application/pdf") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode()


class TestIterFiles(unittest.TestCase):
    def test_image_url_dict_and_str(self):
        a = list(server._iter_files([{"image_url": {"url": "data:image/png;base64,AA"}}]))
        b = list(server._iter_files([{"image_url": "data:image/png;base64,AA"}]))
        c = list(server._iter_files([{"image_url": "https://x/y.png"}]))
        for got in (a, b, c):
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0]["is_url"])
        self.assertEqual(a[0]["src"], b[0]["src"], "dict và string phải cho cùng src")
        self.assertEqual(c[0]["src"], "https://x/y.png")

    def test_file_data_base64_marked_not_url(self):
        got = list(server._iter_files([
            {"type": "file", "file_data": "QUJD", "filename": "báo cáo.xlsx"},
        ]))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["is_url"])
        self.assertEqual(got[0]["src"], "QUJD")
        self.assertEqual(got[0]["filename"], "báo cáo.xlsx")

    def test_input_file_with_url_is_url(self):
        got = list(server._iter_files([
            {"type": "input_file", "file_url": "https://h/a.pdf"},
        ]))
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["is_url"])

    def test_input_file_with_data_url_is_url(self):
        got = list(server._iter_files([{"type": "input_file", "file_data": _data_url(b"x")}]))
        self.assertTrue(got[0]["is_url"])

    def test_text_part_ignored(self):
        self.assertEqual(list(server._iter_files([{"type": "text", "text": "hi"}])), [])
        self.assertEqual(list(server._iter_files("khong phai list")), [])

    def test_unknown_file_id_skipped_not_guessed(self):
        """`file_id` cần API riêng để tra; đoán bừa sẽ upload file rỗng."""
        self.assertEqual(list(server._iter_files([{"type": "input_file", "file_id": "f_1"}])), [])

    def test_iter_images_alias_still_yields_urls(self):
        """Code cũ và test cũ dùng `_iter_images`; phải không vỡ."""
        got = list(server._iter_images([{"image_url": {"url": "https://x/a.png"}}]))
        self.assertEqual(got, ["https://x/a.png"])


class TestSafeFilename(unittest.TestCase):
    def test_keeps_plain_name(self):
        self.assertEqual(server._safe_filename("report.xlsx", ".bin"), "report.xlsx")

    def test_strips_directory_traversal(self):
        for bad in ("../../etc/passwd", "..\\..\\windows\\system32\\cfg",
                    "/etc/shadow", "C:\\Users\\me\\secret.txt"):
            got = server._safe_filename(bad, ".bin")
            self.assertNotIn("/", got)
            self.assertNotIn("\\", got)
            self.assertNotIn("..", got)

    def test_strips_control_chars(self):
        got = server._safe_filename("re\nport\r\t.xlsx", ".bin")
        self.assertNotIn("\n", got)
        self.assertNotIn("\r", got)
        self.assertNotIn("\t", got)

    def test_windows_reserved_gets_prefix(self):
        for bad in ("con", "NUL", "COM1", "lpt9"):
            got = server._safe_filename(bad, ".txt")
            self.assertTrue(got.startswith("_"), got)

    def test_falls_back_to_extension_when_no_name(self):
        self.assertEqual(server._safe_filename(None, ".pdf"), "file.pdf")
        self.assertEqual(server._safe_filename("", ".zip"), "file.zip")

    def test_extension_cap_when_missing(self):
        got = server._safe_filename("khong co duoi", ".png")
        self.assertTrue(got.endswith(".png"))


class TestPrepareFiles(unittest.TestCase):
    def setUp(self):
        self._old = server._TMP_DIR
        import tempfile
        server._TMP_DIR = Path(tempfile.mkdtemp(prefix="testfiles_"))

    def tearDown(self):
        server._TMP_DIR = self._old

    def test_preserves_original_name_for_gemini(self):
        """Tên file phải giữ nguyên: thư viện lấy `Path.name` làm filename gửi lên."""
        spec = [{
            "src": base64.b64encode(b"%PDF-1.4 fake").decode(),
            "filename": "báo cáo bán hàng.xlsx",
            "is_url": False,
        }]
        files, tmps = asyncio.run(server._prepare_files(spec))
        try:
            name = Path(files[0]).name
            self.assertTrue(name.endswith("báo cáo bán hàng.xlsx"), name)
            self.assertNotEqual(name, f"{Path(name).stem}.img")
            self.assertEqual(Path(files[0]).read_bytes(), b"%PDF-1.4 fake")
        finally:
            server._cleanup_tmpfiles(tmps)

    def test_temp_file_name_is_unique(self):
        """Tiền tố uuid chống đè lên nhau khi trùng tên gốc."""
        spec = [
            {"src": base64.b64encode(b"aaa").decode(), "filename": "a.txt", "is_url": False},
            {"src": base64.b64encode(b"bbb").decode(), "filename": "a.txt", "is_url": False},
        ]
        files, tmps = asyncio.run(server._prepare_files(spec))
        try:
            self.assertEqual(len(set(files)), 2)
        finally:
            server._cleanup_tmpfiles(tmps)

    def test_rejects_too_many_files(self):
        spec = [
            {"src": base64.b64encode(b"x").decode(), "filename": f"a{i}.txt", "is_url": False}
            for i in range(server.MAX_UPLOAD_FILES + 1)
        ]
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files(spec))

    def test_rejects_oversize_single_file(self):
        big = b"0" * (server.MAX_FILE_BYTES + 1024)
        spec = [{"src": base64.b64encode(big).decode(), "filename": "a.bin", "is_url": False}]
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files(spec))

    def test_rejects_oversize_total(self):
        """Tổng vượt trần dù mỗi file nhỏ — nếu không sẽ OOM ở phía Google."""
        chunk = b"0" * (4 * 1024 * 1024)
        n = server.MAX_TOTAL_UPLOAD_BYTES // len(chunk) + 2
        spec = [
            {"src": base64.b64encode(chunk).decode(), "filename": f"a{i}.bin", "is_url": False}
            for i in range(n)
        ]
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files(spec))

    def test_invalid_base64_rejected(self):
        spec = [{"src": "!!!khong phai base64!!!", "filename": "a.txt", "is_url": False}]
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files(spec))

    def test_cleans_up_when_later_file_fails(self):
        """Đã ghi file tạm cho file trước mà file sau lỗi thì phải dọn ngay."""
        before = set(server.tmp_dir().iterdir())
        spec = [
            {"src": base64.b64encode(b"ok").decode(), "filename": "a.txt", "is_url": False},
            {"src": "!!!!", "filename": "b.txt", "is_url": False},
        ]
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files(spec))
        self.assertEqual(before, set(server.tmp_dir().iterdir()))

    def test_image_vs_document_size_limits(self):
        """Ảnh bị giới hạn MAX_IMAGE_BYTES (10MB), tài liệu được tới MAX_FILE_BYTES (20MB)."""
        # 12MB: vượt trần ảnh 10MB nhưng dưới trần tài liệu 20MB
        size = 12 * 1024 * 1024
        payload = base64.b64encode(b"x" * size).decode()

        # Ảnh 12MB phải bị từ chối
        with self.assertRaises(ValueError):
            asyncio.run(server._prepare_files([{"src": payload, "filename": "photo.png", "is_url": False}]))

        # Tài liệu 12MB phải được chấp nhận
        files, tmps = asyncio.run(server._prepare_files([{"src": payload, "filename": "report.pdf", "is_url": False}]))
        try:
            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0]).name, "report.pdf")
        finally:
            server._cleanup_tmpfiles(tmps)

    def test_sniff_ext_recognizes_pdf_and_zip(self):
        self.assertEqual(server._sniff_ext(b"%PDF-1.7..."), ".pdf")
        self.assertEqual(server._sniff_ext(b"PK\x03\x04..."), ".zip")
        self.assertEqual(server._sniff_ext(b"\x89PNG\r\n\x1a\n"), ".png")
        self.assertEqual(server._sniff_ext(b"\xff\xd8\xff"), ".jpg")
        self.assertEqual(server._sniff_ext(b"unknown data"), ".bin")


class TestCacheAndFileId(unittest.TestCase):
    def test_file_id_is_url_hash_and_stable(self):
        """Khoá theo URL nên ID ổn định qua các lần restart."""
        a = server._file_id("https://lh3.googleusercontent.com/abc")
        b = server._file_id("https://lh3.googleusercontent.com/abc")
        c = server._file_id("https://lh3.googleusercontent.com/dif")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_file_id_regex_accepts_only_hex64(self):
        for bad in ("../etc", "ABCDEF", "a" * 63, "a" * 65, "%2e%2e", "a" * 64 + "/b"):
            self.assertIsNone(server._FILE_ID_RE.fullmatch(bad), bad)
        self.assertIsNotNone(server._FILE_ID_RE.fullmatch("a" * 64))

    def test_content_disposition_inline_vs_attachment(self):
        img = server._content_disposition("a.png", inline=True)
        doc = server._content_disposition("b.xlsx", inline=False)
        self.assertTrue(img.startswith("inline;"))
        self.assertTrue(doc.startswith("attachment;"))
        self.assertIn('filename="a.png"', img)

    def test_content_disposition_strips_quotes_and_newlines(self):
        """Chặn header injection: CRLF hoặc dấu nháy trong tên file."""
        got = server._content_disposition('a"\r\nX-Evil: 1.txt', inline=False)
        self.assertNotIn("\r", got)
        self.assertNotIn("\n", got)


class TestFilesEndpointAuth(unittest.TestCase):
    """`/v1/files/` phục vụ dữ liệu tài khoản Google thật → phải có auth."""

    def test_requires_api_key(self):
        # Kiểm tra route được bảo vệ bởi dependency require_key
        route = next((r for r in server.app.routes if getattr(r, "path", None) == "/v1/files/{file_id}"), None)
        self.assertIsNotNone(route, "Route /v1/files/{file_id} phải tồn tại")
        self.assertTrue(
            any(getattr(d, "dependency", None) is server.require_key for d in route.dependencies),
            "Route /v1/files/{file_id} phải có dependency require_key",
        )
        # require_key từ chối khi không có key hoặc key sai
        with self.assertRaises(server.HTTPException) as ctx:
            server.require_key(authorization=None, x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(server.HTTPException) as ctx:
            server.require_key(authorization="Bearer sai-key", x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_with_key_traversal_is_404_not_500(self):
        """`file_id` đi thẳng vào đường dẫn nên traversal phải ra 404, không lộ."""
        for bad in ("../../etc/passwd", "..%2f..%2fwindows", "zz"):
            with self.assertRaises(server.HTTPException) as ctx:
                asyncio.run(server.get_output_file(bad))
            self.assertIn(ctx.exception.status_code, (400, 404), bad)

    def test_missing_file_is_404(self):
        with self.assertRaises(server.HTTPException) as ctx:
            asyncio.run(server.get_output_file("0" * 64))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_serves_cached_file_with_security_headers(self):
        fid = "a" * 64
        blob, meta_path = server._cache_paths(fid)
        server.OUTPUT_CACHE.mkdir(parents=True, exist_ok=True)
        try:
            blob.write_bytes(b"fake-image-bytes")
            import json
            meta_path.write_text(json.dumps({
                "mime_type": "image/png",
                "filename": "output.png",
            }), encoding="utf-8")
            resp = asyncio.run(server.get_output_file(fid))
            self.assertEqual(resp.body, b"fake-image-bytes")
            self.assertEqual(resp.media_type, "image/png")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("inline;", resp.headers.get("Content-Disposition", ""))
        finally:
            blob.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

    def test_body_limit_rejects_oversize_request(self):
        """Body phải bị chặn trước khi FastAPI parse hết vào RAM."""
        class _FakeReq:
            def __init__(self, method="POST", path="/v1/chat/completions", cl="2000000"):
                self.method = method
                self.url = type("URL", (), {"path": path})()
                self.headers = {"content-length": str(cl)}

        async def _fake_next(req):
            return server.Response(content=b"ok")

        old = server.MAX_REQUEST_BYTES
        try:
            server.MAX_REQUEST_BYTES = 1000
            resp = asyncio.run(server._limit_request_body(_FakeReq(cl=2_000_000), _fake_next))
            self.assertEqual(resp.status_code, 413)

            resp_ok = asyncio.run(server._limit_request_body(_FakeReq(cl=500), _fake_next))
            self.assertEqual(resp_ok.body, b"ok")
        finally:
            server.MAX_REQUEST_BYTES = old


class TestCompletionBodyAttachments(unittest.TestCase):
    def test_content_stays_string_when_attachments_present(self):
        """`content` phải là string để client cũ đọc `message.content` không vỡ."""
        att = [{"kind": "image", "url": "/v1/files/" + "a" * 64,
                "title": "t", "filename": "t.png", "mime_type": "image/png"}]
        body = server._completion_body("id", 0, "m", "xin chao", None, False, att)
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["content"], "xin chao")
        self.assertIsInstance(msg["content"], str)
        self.assertEqual(len(msg["attachments"]), 1)
        self.assertEqual(len(msg["images"]), 1)

    def test_no_attachment_fields_when_empty(self):
        body = server._completion_body("id", 0, "m", "hi", None, False, None)
        msg = body["choices"][0]["message"]
        self.assertNotIn("attachments", msg)
        self.assertNotIn("images", msg)

    def test_images_alias_only_contains_images(self):
        att = [
            {"kind": "image", "url": "/a"},
            {"kind": "audio", "url": "/b"},
        ]
        body = server._completion_body("id", 0, "m", "t", None, False, att)
        msg = body["choices"][0]["message"]
        self.assertEqual(len(msg["attachments"]), 2)
        self.assertEqual(len(msg["images"]), 1)

    def test_stream_emits_attachments_delta_before_finish(self):
        att = [{"kind": "image", "url": "/v1/files/" + "b" * 64}]
        resp = server._completion_body("id", 0, "m", "x", None, True, att)

        async def collect():
            return [c async for c in resp.body_iterator]

        chunks = asyncio.run(collect())
        deltas = []
        for c in chunks:
            if c.startswith("data: ") and "[DONE]" not in c:
                import json
                d = json.loads(c[6:].strip())
                deltas.append(d["choices"][0])
        att_idx = next(i for i, d in enumerate(deltas) if d["delta"].get("attachments"))
        finish_idx = next(i for i, d in enumerate(deltas) if d["finish_reason"] == "stop")
        self.assertLess(att_idx, finish_idx, "media phai den truoc finish_reason")

    def test_stream_without_attachments_has_no_attachments_delta(self):
        resp = server._completion_body("id", 0, "m", "x", None, True, None)

        async def collect():
            return [c async for c in resp.body_iterator]

        self.assertNotIn("attachments", "".join(asyncio.run(collect())))

    def test_collect_attachments_video_and_audio(self):
        class DummyMedia:
            def __init__(self, url="", mp3_url="", title=""):
                self.url = url
                self.mp3_url = mp3_url
                self.title = title

        class DummyCandidate:
            def __init__(self):
                self.generated_images = []
                self.generated_videos = [DummyMedia(url="https://x/v.mp4", title="demo video")]
                self.generated_media = [DummyMedia(mp3_url="https://x/a.mp3", title="demo audio")]

        class DummyOutput:
            def __init__(self):
                self.candidates = [DummyCandidate()]
                self.chosen = 0

        async def fake_store_output(url, title, alt="", expected_kind="image"):
            return {
                "id": server._file_id(url),
                "url": f"/v1/files/{server._file_id(url)}",
                "title": title,
                "mime_type": "video/mp4" if expected_kind == "video" else "audio/mpeg",
                "filename": f"{title}.{'mp4' if expected_kind == 'video' else 'mp3'}",
            }

        old_store = server._store_output
        server._store_output = fake_store_output
        try:
            items = asyncio.run(server._collect_attachments(DummyOutput()))
            self.assertEqual(len(items), 2)
            kinds = {i["kind"] for i in items}
            self.assertEqual(kinds, {"video", "audio"})
        finally:
            server._store_output = old_store


if __name__ == "__main__":
    unittest.main()