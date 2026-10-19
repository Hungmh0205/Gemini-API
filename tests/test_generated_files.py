"""Test cho khả năng nhận TÀI LIỆU Gemini sinh ra (xlsx, docx, pdf, csv, mã nguồn).

Trước khi có phần này, server chỉ gom ảnh/video/audio. Bảy nhóm test:

  1. Scanner tìm URL tài liệu mà không cần biết field number.
  2. Scanner đặt đúng tên file Gemini khai (`sandbox:/mnt/data/...`).
  3. Scanner KHÔNG báo nhầm ảnh/video đã có parser riêng.
  4. `GeneratedFile` suy đúng MIME từ đuôi file.
  5. `_store_output` nhận MIME tài liệu thay vì loại bỏ.
  6. `_collect_attachments` gắn `kind="file"` và truyền tên xuống `_store_output`.
  7. Response có trường `files` bên cạnh `attachments`/`images`.

Lý do thiết kế quan trọng nhất: scanner cố tình dò theo HÌNH DẠNG URL thay vì
theo số thứ tự field JSPB. Số thứ tự đó không được Google công bố và đã từng dịch
chuyển, nên hard-code sẽ hỏng ngay lần build frontend kế tiếp.
"""

import asyncio
import inspect
import json
import os
import sys
import unittest
from pathlib import Path

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)
os.environ.setdefault("GEMINI_API_KEY", "test-key-for-docs")

import server  # noqa: E402
from gemini_webapi.types import GeneratedFile  # noqa: E402
from gemini_webapi.utils import (  # noqa: E402
    collect_generated_files,
    collect_sandbox_names,
)

# URL tải thật của Gemini thường KHÔNG có đuôi file — đây là dạng phổ biến nhất.
OPAQUE_URL = "https://lh3.googleusercontent.com/gg-dl/AbCdEf1234567890XYZ=="
DOC_URL = "https://lh3.googleusercontent.com/gg-dl/report.xlsx"


class TestScannerFindsDocuments(unittest.TestCase):
    def test_finds_url_naming_a_document(self):
        got = collect_generated_files([[None, [DOC_URL]]])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, DOC_URL)
        self.assertEqual(got[0].name, "report.xlsx")

    def test_finds_url_hidden_deep_in_the_payload(self):
        """Field number chưa biết → phải quét toàn bộ cây, không đoán vị trí."""
        payload = [None, [[[[[[DOC_URL]]]]]]]
        got = collect_generated_files(payload)
        self.assertEqual([g.url for g in got], [DOC_URL])

    def test_finds_url_inside_dict_keys_and_values(self):
        got = collect_generated_files([{"a": {"b": [None, {"url": DOC_URL}]}}])
        self.assertEqual(len(got), 1)

    def test_sandbox_placeholder_alone_still_reported(self):
        """Gemini nói có file; ta chưa tìm được URL thì vẫn phải báo tên file."""
        got = collect_generated_files([], "Xem sandbox:/mnt/data/bao-cao.xlsx nhé")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "bao-cao.xlsx")
        self.assertEqual(got[0].url, "", "chưa có URL thì để trống, không bịa URL")

    def test_extension_from_query_string(self):
        got = collect_generated_files([[None, ["https://x/dl/f?fn=report.docx"]]])
        self.assertEqual(got[0].name, "report.docx")

    def test_sandbox_and_data_url_are_left_alone(self):
        """`sandbox:` không phải URL tải được — tuyệt đối không đem ra ngoài."""
        got = collect_generated_files([], "sandbox:/mnt/data/x.xlsx")
        self.assertTrue(all(not g.url.startswith("sandbox:") for g in got))


class TestScannerNaming(unittest.TestCase):
    def test_sandbox_name_attaches_to_matching_url(self):
        got = collect_generated_files(
            [[None, [OPAQUE_URL]]], "Đây là sandbox:/mnt/data/ket-qua.xlsx của bạn"
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, OPAQUE_URL)
        self.assertEqual(
            got[0].name,
            "ket-qua.xlsx",
            "tên phải lấy từ sandbox chứ không phải từ URL (URL không có đuôi)",
        )

    def test_name_preserves_vietnamese_characters(self):
        got = collect_generated_files(
            [[None, [OPAQUE_URL]]], "sandbox:/mnt/data/báo cáo tháng.xlsx"
        )
        self.assertEqual(got[0].name, "báo cáo tháng.xlsx")

    def test_multiple_files_keep_their_own_names(self):
        got = collect_generated_files(
            [[None, [OPAQUE_URL, OPAQUE_URL + "2", OPAQUE_URL + "3"]]],
            "sandbox:/mnt/data/a.xlsx và sandbox:/mnt/data/b.docx",
        )
        self.assertEqual([g.name for g in got[:2]], ["a.xlsx", "b.docx"])

    def test_collect_sandbox_names_dedupes(self):
        got = collect_sandbox_names("sandbox:/mnt/data/a.xlsx, xem sandbox:/mnt/data/a.xlsx")
        self.assertEqual(got, ["a.xlsx"])

    def test_collect_sandbox_names_rejects_traversal(self):
        got = collect_sandbox_names("sandbox:/mnt/data/../../etc/passwd")
        self.assertEqual(got, [], "tên chứa đường dẫn phải bị loại, không nối vào đĩa")


class TestScannerDoesNotDuplicateMedia(unittest.TestCase):
    def test_skips_encrypted_thumbnail_urls(self):
        url = "https://encrypted-tbn0.gstatic.com/images?q=abc"
        self.assertEqual(collect_generated_files([[None, [url]]]), [])

    def test_skips_gg_dl_image_urls_already_claimed(self):
        """URL do parser ảnh đã lấy rồi thì không được báo lại thành tài liệu."""
        got = collect_generated_files([[None, [OPAQUE_URL]]], exclude_urls={OPAQUE_URL})
        self.assertEqual(got, [])

    def test_sandbox_naming_a_png_is_not_a_document(self):
        got = collect_generated_files([], "sandbox:/mnt/data/bieu-do.png")
        self.assertEqual(got, [], "ảnh đã có đường ảnh riêng, không nhân đôi")

    def test_plain_reply_produces_nothing(self):
        got = collect_generated_files([[None, ["Xin chào, tôi giúp được gì?"]]], "Xin chào!")
        self.assertEqual(got, [])

    def test_cyclic_payload_terminates(self):
        """Dữ liệu đến từ server lạ: cấu trúc vòng lặp không được treo process."""
        node = [None]
        node.append(node)
        self.assertEqual(collect_generated_files(node), [])


class TestGeneratedFileMime(unittest.TestCase):
    def test_mime_from_extension_wins_over_declared(self):
        f = GeneratedFile(url="u", name="r.xlsx", mime_type="application/octet-stream")
        self.assertEqual(
            f.resolved_mime_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Gemini hay gắn octet-stream cho mọi tài liệu; đuôi file đáng tin hơn",
        )

    def test_docx_and_csv_mime(self):
        self.assertEqual(
            GeneratedFile(url="u", name="a.docx").resolved_mime_type,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(GeneratedFile(url="u", name="a.csv").resolved_mime_type, "text/csv")

    def test_unknown_extension_falls_back_to_declared(self):
        self.assertEqual(
            GeneratedFile(
                url="u", name="a.weird", mime_type="application/x-weird"
            ).resolved_mime_type,
            "application/x-weird",
        )

    def test_no_signal_at_all_is_opaque(self):
        self.assertEqual(GeneratedFile(url="u").resolved_mime_type, "application/octet-stream")

    def test_is_media_separates_documents_from_media(self):
        self.assertTrue(GeneratedFile(url="u", name="c.png").is_media)
        self.assertFalse(GeneratedFile(url="u", name="c.xlsx").is_media)
        self.assertFalse(GeneratedFile(url="u", name="c").is_media)


class TestCandidateCarriesFiles(unittest.TestCase):
    def test_candidate_has_generated_files_field(self):
        from gemini_webapi.types import Candidate

        c = Candidate(rcid="r", text="x")
        self.assertEqual(c.generated_files, [])
        self.assertEqual(c.files, [])

    def test_files_property_drops_media(self):
        from gemini_webapi.types import Candidate

        c = Candidate(
            rcid="r",
            text="x",
            generated_files=[
                GeneratedFile(url="u1", name="a.xlsx"),
                GeneratedFile(url="u2", name="b.png"),
            ],
        )
        self.assertEqual([f.name for f in c.files], ["a.xlsx"])


class TestStoreOutputAcceptsDocuments(unittest.TestCase):
    def _fake_response(self, ctype, body):
        class R:
            headers = {"content-type": ctype}

            def raise_for_status(self):
                pass

            @property
            def content(self):
                return body

        class S:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def get(self_inner, url, headers=None):
                return R()

        return S()

    def _run_store(self, ctype, body, **kwargs):
        old = server.AsyncSession
        server.AsyncSession = lambda **_: self._fake_response(ctype, body)
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                old_cache, server.OUTPUT_CACHE = server.OUTPUT_CACHE, Path(td) / "o"
                try:
                    return asyncio.run(server._store_output(DOC_URL, "x", **kwargs))
                finally:
                    server.OUTPUT_CACHE = old_cache
        finally:
            server.AsyncSession = old

    def test_octet_stream_xlsx_no_longer_rejected(self):
        meta = self._run_store(
            "application/octet-stream",
            b"PK\x03\x04fake-xlsx",
            expected_kind="document",
            filename="bao-cao.xlsx",
        )
        self.assertIsNotNone(meta, "trước đây octet-stream + .xlsx bị loại ở nhánh image")
        self.assertEqual(meta["filename"], "bao-cao.xlsx")
        self.assertEqual(
            meta["mime_type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_text_csv_is_kept(self):
        meta = self._run_store("text/csv", b"a,b\n1,2\n", expected_kind="document")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["mime_type"], "text/csv")

    def test_image_path_still_works(self):
        meta = self._run_store("image/png", b"\x89PNG\r\n\x1a\n", expected_kind="image")
        self.assertEqual(meta["filename"], "x.png", "ảnh không có tên riêng thì lấy tên title")


class TestCollectAttachmentsForFiles(unittest.TestCase):
    def test_file_gets_kind_and_name(self):
        seen = {}

        class DummyCandidate:
            generated_images = []
            generated_videos = []
            generated_media = []
            files = [GeneratedFile(url=DOC_URL, name="bao-cao.xlsx")]

        class DummyOutput:
            candidates = [DummyCandidate()]
            chosen = 0

        async def fake_store(
            url, title, alt="", expected_kind="image", filename="", declared_mime=""
        ):
            seen.update(url=url, kind=expected_kind, filename=filename)
            return {
                "id": "a" * 64,
                "url": "/v1/files/" + "a" * 64,
                "filename": filename,
                "mime_type": "application/octet-stream",
                "title": title,
                "size": 1,
            }

        old = server._store_output
        server._store_output = fake_store
        try:
            items = asyncio.run(server._collect_attachments(DummyOutput()))
        finally:
            server._store_output = old

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "file")
        self.assertEqual(items[0]["filename"], "bao-cao.xlsx")
        self.assertEqual(seen["filename"], "bao-cao.xlsx", "tên phải truyền xuống _store_output")
        self.assertEqual(seen["kind"], "document")

    def test_named_file_without_url_is_still_reported(self):
        class DummyCandidate:
            generated_images = []
            generated_videos = []
            generated_media = []
            files = [GeneratedFile(url="", name="ket-qua.csv")]

        class DummyOutput:
            candidates = [DummyCandidate()]
            chosen = 0

        items = asyncio.run(server._collect_attachments(DummyOutput()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "file")
        self.assertFalse(items[0]["downloadable"])
        self.assertIsNone(items[0]["url"])


class TestResponseExposesFiles(unittest.TestCase):
    def test_completion_body_has_files_alias(self):
        body = server._completion_body(
            "cid",
            0,
            "m",
            "text",
            None,
            stream=False,
            attachments=[
                {"kind": "file", "filename": "a.xlsx"},
                {"kind": "image", "filename": "b.png"},
            ],
        )
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["files"], [{"kind": "file", "filename": "a.xlsx"}])
        self.assertEqual(msg["images"], [{"kind": "image", "filename": "b.png"}])
        self.assertIsInstance(msg["content"], str, "content phải giữ là string")

    def test_no_files_key_when_no_attachments(self):
        body = server._completion_body("cid", 0, "m", "text", None, stream=False)
        self.assertNotIn("files", body["choices"][0]["message"])


class TestServeDocumentIsAttachment(unittest.TestCase):
    def test_document_disposition_is_download(self):
        self.assertTrue(
            server._content_disposition("bao-cao.xlsx", inline=False).startswith("attachment;")
        )

    def test_image_disposition_is_inline(self):
        self.assertTrue(server._content_disposition("a.png", inline=True).startswith("inline;"))

    def test_mime_for_ext_covers_types_mimetypes_misses(self):
        self.assertEqual(server._mime_for_ext(".rtf"), "application/rtf")
        self.assertEqual(server._mime_for_ext(".tex"), "application/x-tex")
        self.assertEqual(server._mime_for_ext(".ipynb"), "application/x-ipynb+json")

    def test_mime_for_ext_uses_mimetypes_for_known(self):
        self.assertEqual(
            server._mime_for_ext(".xlsx"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_document_exts_do_not_count_as_images(self):
        """Sai chỗ này thì file 20MB đi nhánh ảnh 10MB và bị chặn oan."""
        self.assertFalse(server._is_img_name("bao-cao.xlsx"))
        self.assertTrue(server._is_img_name("anh.png"))
        self.assertEqual(server._url_ext("https://x/a.docx"), ".docx")


class TestEndToEndOverHttp(unittest.TestCase):
    """Chạy hết đường đi thật: file Gemini sinh → cache → handler `/v1/files/{id}`.

    Đây là test bắt lỗi mà các test trên không thấy: tên file và MIME sai trong
    `Content-Disposition` khiến trình duyệt tải về một file không mở được, dù mọi
    bước trung gian đều "đúng".

    Gọi handler trực tiếp thay vì `TestClient`, đúng convention của repo: bản
    portable không cài `httpx` nên `TestClient` không dùng được (xem test_admin).
    """

    def _serve(self, mime, blob, filename):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "o"
            cache.mkdir(parents=True)
            fid = server._file_id(DOC_URL)
            (cache / fid).write_bytes(blob)
            (cache / f"{fid}.json").write_text(
                json.dumps(
                    {
                        "id": fid,
                        "url": f"/v1/files/{fid}",
                        "title": filename,
                        "mime_type": mime,
                        "filename": filename,
                        "size": len(blob),
                    }
                ),
                encoding="utf-8",
            )
            old_cache = server.OUTPUT_CACHE
            server.OUTPUT_CACHE = cache
            try:
                resp = asyncio.run(server.get_output_file(fid))
            finally:
                server.OUTPUT_CACHE = old_cache
            return resp

    def test_xlsx_downloads_with_usable_name_and_type(self):
        r = self._serve(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"PK\x03\x04fake-xlsx-bytes",
            "báo cáo tháng.xlsx",
        )
        self.assertEqual(r.body, b"PK\x03\x04fake-xlsx-bytes")
        cd = r.headers["content-disposition"]
        self.assertTrue(cd.startswith("attachment;"), "tài liệu phải tải về, không inline")
        # Tên có dấu phải qua được cả dạng ASCII lẫn RFC 5987.
        self.assertIn("filename*=UTF-8''", cd)
        self.assertIn("xlsx", cd)
        self.assertEqual(
            r.media_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["content-length"], str(len(b"PK\x03\x04fake-xlsx-bytes")))

    def test_csv_downloads_as_text_csv(self):
        r = self._serve("text/csv", b"a,b\n1,2\n", "ket-qua.csv")
        self.assertEqual(r.media_type, "text/csv")
        self.assertEqual(r.body, b"a,b\n1,2\n")

    def test_image_still_inline(self):
        r = self._serve("image/png", b"\x89PNG\r\n\x1a\n", "anh.png")
        self.assertTrue(r.headers["content-disposition"].startswith("inline;"))

    def test_unknown_id_is_404_not_a_leak(self):
        import tempfile

        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as td:
            old = server.OUTPUT_CACHE
            server.OUTPUT_CACHE = Path(td)
            try:
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(server.get_output_file("0" * 64))
            finally:
                server.OUTPUT_CACHE = old
        self.assertEqual(ctx.exception.status_code, 404)


class _FakeRequest:
    """Request tối giản cho handler chỉ đọc header, tránh kéo cả FastAPI vào test."""

    def __init__(self, origin="https://gemini.google.com", payload=None):
        self.headers = {"origin": origin}
        self._payload = payload

    async def json(self):
        return self._payload if self._payload is not None else {}


class TestUploadEndpoint(unittest.TestCase):
    """`/v1/files/upload` - đường extension đẩy file Gemini tạo về server."""

    def _store(self, key, body, name="", mime=""):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            old = server.OUTPUT_CACHE
            server.OUTPUT_CACHE = Path(td)
            try:
                return server._store_upload(key, body, name or key, filename=name, declared_mime=mime)
            finally:
                server.OUTPUT_CACHE = old

    def test_upload_keeps_name_and_mime_and_serves_bytes_back(self):
        blob = b"PK\x03\x04 xlsx-bytes"
        m = self._store("c1|5", blob, "bao_cao.xlsx")
        self.assertEqual(m["filename"], "bao_cao.xlsx")
        self.assertEqual(
            m["mime_type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.assertEqual(m["size"], len(blob))
        self.assertEqual(m["url"], f"/v1/files/{m['id']}")

    def test_same_key_is_idempotent(self):
        """Extension có thể bắt cùng một file nhiều lần; phải trả cùng file_id."""
        a = self._store("c1|5", b"a" * 10, "a.xlsx")
        b = self._store("c1|5", b"a" * 10, "a.xlsx")
        self.assertEqual(a["id"], b["id"])

    def test_different_key_gives_different_id(self):
        a = self._store("c1|5", b"a", "a.xlsx")
        b = self._store("c1|6", b"a", "a.xlsx")
        self.assertNotEqual(a["id"], b["id"])

    def test_csv_is_text_csv_not_excel(self):
        """Registry Windows ánh xạ `.csv` sang `application/ms-excel`; phải ghim lại."""
        self.assertEqual(server._mime_for_ext(".csv"), "text/csv")
        m = self._store("c1|7", b"a,b\n1,2\n", "ket_qua.csv")
        self.assertEqual(m["mime_type"], "text/csv")

    def test_rejects_empty_and_oversized(self):
        self.assertIsNone(self._store("k1", b"", "a.xlsx"))
        big = b"x" * (server.MAX_OUTPUT_FILE_BYTES + 1)
        self.assertIsNone(self._store("k2", big, "a.xlsx"))

    def test_sniffs_type_when_name_has_no_extension(self):
        m = self._store("c1|8", b"%PDF-1.7 ...", "")
        self.assertEqual(m["mime_type"], "application/pdf")
        self.assertTrue(m["filename"].endswith(".pdf"), m["filename"])
        # Tên rác từ khoá phải được làm sạch trước khi đưa vào Content-Disposition.
        self.assertNotIn("|", m["filename"])

    def test_endpoint_is_behind_api_key(self):
        # Path này có cả route OPTIONS (preflight, cố ý không có key) và POST.
        # Chỉ route POST mới phải bắt key.
        routes = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/upload" and "POST" in (r.methods or set())
        ]
        self.assertTrue(routes, "Phải có route POST /v1/files/upload")
        for route in routes:
            self.assertTrue(
                any(getattr(d, "dependency", None) is server.require_key for d in route.dependencies),
                "Route POST upload phải yêu cầu API key: nó nhận dữ liệu tài khoản thật",
            )

    def test_upload_sends_cors_for_the_extension_origin(self):
        """Content script chạy trong trang https nên không có CORS là bị chặn im lặng."""
        preflight = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/upload" and "OPTIONS" in (r.methods or set())
        ]
        self.assertTrue(preflight, "Thiếu route OPTIONS cho preflight CORS")
        resp = asyncio.run(preflight[0].endpoint())
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "*")
        self.assertIn("Authorization", resp.headers.get("access-control-allow-headers", ""))

    def test_ping_beacon_needs_no_key_but_sends_cors(self):
        """Beacon chứng minh content script đã inject: không có nó thì im lặng mất dấu."""
        routes = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/ping" and "GET" in (r.methods or set())
        ]
        self.assertTrue(routes, "Thiếu route GET /v1/files/ping")
        route = routes[0]
        self.assertFalse(
            [d for d in route.dependencies if getattr(d, "dependency", None) is server.require_key],
            "Ping phải gọi được không cần key, nếu không content script im lặng thất bại",
        )
        resp = asyncio.run(route.endpoint(_FakeRequest()))
        self.assertTrue(resp.headers.get("access-control-allow-origin"), "Ping cũng là request chéo origin")

    def test_debug_trail_has_post_and_preflight(self):
        """Dấu vết lỗi phải có OPTIONS: POST JSON luôn kèm preflight.

        Không có OPTIONS thì trình duyệt chặn trước khi gửi, content script nuốt lỗi
        trong `.catch(() => {})` và ta mất dấu vết đúng lúc cần nhất.
        """
        post = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/debug" and "POST" in (r.methods or set())
        ]
        options = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/debug" and "OPTIONS" in (r.methods or set())
        ]
        self.assertTrue(post, "Thiếu route POST /v1/files/debug")
        self.assertTrue(options, "Thiếu route OPTIONS /v1/files/debug — preflight sẽ bị chặn")

        resp = asyncio.run(options[0].endpoint())
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "*")
        self.assertIn("Content-Type", resp.headers.get("access-control-allow-headers", ""))
        self.assertIn("Authorization", resp.headers.get("access-control-allow-headers", ""))

    def test_debug_trail_has_post_and_preflight(self):
        """Dấu vết lỗi phải có OPTIONS: POST JSON luôn kèm preflight.

        Không có OPTIONS thì trình duyệt chặn trước khi gửi, content script nuốt lỗi
        trong `.catch(() => {})` và ta mất dấu vết đúng lúc cần nhất.
        """
        post = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/debug" and "POST" in (r.methods or set())
        ]
        options = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/files/debug" and "OPTIONS" in (r.methods or set())
        ]
        self.assertTrue(post, "Thiếu route POST /v1/files/debug")
        self.assertTrue(options, "Thiếu route OPTIONS /v1/files/debug — preflight sẽ bị chặn")

        resp = asyncio.run(options[0].endpoint())
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "*")
        self.assertIn("Content-Type", resp.headers.get("access-control-allow-headers", ""))
        self.assertIn("Authorization", resp.headers.get("access-control-allow-headers", ""))

    def test_chat_jobs_endpoint_lists_pending_job(self):
        """Extension poll `/v1/chat/jobs` để gõ prompt vào Gemini."""
        routes = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/chat/jobs" and "GET" in (r.methods or set())
        ]
        self.assertTrue(routes, "Thiếu route GET /v1/chat/jobs cho extension poll")
        job_id = server._new_chat_job("tao file excel", "gemini-flash")
        try:
            resp = asyncio.run(routes[0].endpoint(_FakeRequest()))
            body = json.loads(resp.body)
            self.assertIn(job_id, [j["job_id"] for j in body["jobs"]])
            self.assertEqual(body["jobs"][0]["prompt"], "tao file excel")
        finally:
            server._drop_chat_job(job_id)

    def test_result_endpoint_wakes_the_waiter(self):
        routes = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/chat/jobs/{job_id}/result"
            and "POST" in (r.methods or set())
        ]
        self.assertTrue(routes, "Thiếu route POST /v1/chat/jobs/{job_id}/result")
        job_id = server._new_chat_job("p", "m")
        job = server._CHAT_JOBS[job_id]

        async def post(payload):
            req = _FakeRequest(payload=payload)
            return await routes[0].endpoint(job_id, req)

        try:
            asyncio.run(post({"text": "xin chao", "files": [{"id": "f9"}]}))
            got = asyncio.run(server._await_chat_job(job, timeout=1.0))
            self.assertEqual(got["text"], "xin chao")
            self.assertEqual(got["files"][0]["id"], "f9")
        finally:
            server._drop_chat_job(job_id)

    def test_result_endpoint_reports_error_to_waiter(self):
        routes = [
            r
            for r in server.app.routes
            if getattr(r, "path", None) == "/v1/chat/jobs/{job_id}/result"
            and "POST" in (r.methods or set())
        ]
        job_id = server._new_chat_job("p", "m")
        job = server._CHAT_JOBS[job_id]
        try:
            asyncio.run(routes[0].endpoint(job_id, _FakeRequest(payload={"error": "khong tim thay o nhap"})))
            self.assertEqual(job["error"], "khong tim thay o nhap")
        finally:
            server._drop_chat_job(job_id)

    def test_uploaded_file_metadata_becomes_attachment(self):
        """Bytes đã upload phải ra đúng khuôn attachment của response."""
        out = server._file_attachments_from_uploaded(
            [{"id": "abc123", "filename": "bang_luong.xlsx", "mime_type": "x", "size": 5981}]
        )
        self.assertEqual(out[0]["kind"], "file")
        self.assertEqual(out[0]["id"], "abc123")
        self.assertEqual(out[0]["url"], "/v1/files/abc123")
        self.assertEqual(out[0]["title"], "bang_luong.xlsx")
        self.assertEqual(out[0]["size"], 5981)
        # Thiếu id thì bỏ, không sinh attachment hỏng.
        self.assertEqual(server._file_attachments_from_uploaded([{"nope": 1}]), [])

    def test_browser_generate_is_opt_in(self):
        """Mặc định phải giữ đường API cũ để nâng cấp không làm hỏng hành vi."""
        self.assertIn("browser_generate", inspect.getsource(server))


if __name__ == "__main__":
    unittest.main()
