#!/usr/bin/env python3
"""Chuyển riêng thao tác upload file lên Google qua proxy.

Lý do tồn tại: `content-push.googleapis.com` (host nhận upload) reset kết nối TLS
khi gọi bằng curl_cffi từ mạng hiện tại, trong khi các host khác của Gemini lại
bình thường. Đi qua proxy (SOCKS/HTTP) thì TLS handshake thành công.

Phạm vi: CHỈ upload file. Session cookie/phiên Gemini của `GeminiClient` vẫn đi
trực tiếp, nên không đụng tới auth hay sinh nội dung.

Bật/tắt bằng config.json -> "upload_proxy" {"enabled": bool, ...} hoặc biến môi
trường GEMINI_UPLOAD_PROXY=0/1.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import random
import threading
from pathlib import Path

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

from gemini_webapi.constants import BROWSER_TYPE, Endpoint, Headers
from gemini_webapi.utils.upload_file import _generate_random_name
from gemini_webapi.utils.upload_file import upload_file as _original_upload_file

log = logging.getLogger("upload_proxy")

# Endpoint từ chối host này (407) — bỏ để khỏi tốn một vòng retry vô ích.
_SKIP_HOSTS = {"198.105.121.200"}


class ProxyPool:
    """Xoay vòng proxy từ các file `host:port:user:pass` (mỗi dòng một proxy)."""

    def __init__(self, files: list[str]) -> None:
        self._lock = threading.Lock()
        self._all: list[str] = []
        self._bad: set[str] = set()
        for path in files:
            self._all.extend(self._load(path))
        # Bỏ trùng nhưng giữ thứ tự.
        seen: set[str] = set()
        self._all = [p for p in self._all if not (p in seen or seen.add(p))]

    @staticmethod
    def _load(path: str) -> list[str]:
        p = Path(path)
        if not p.is_file():
            log.warning("Không thấy file proxy: %s", path)
            return []
        out: list[str] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 4:
                continue
            host, port, user, pw = parts[0], parts[1], parts[2], parts[3]
            if host in _SKIP_HOSTS:
                continue
            out.append(f"http://{user}:{pw}@{host}:{port}")
        return out

    def usable(self) -> list[str]:
        with self._lock:
            good = [p for p in self._all if p not in self._bad]
            return good or list(self._all)

    def order(self) -> list[str]:
        """Thứ tự ngẫu nhiên để tải rải đều giữa các proxy."""
        items = self.usable()
        random.shuffle(items)
        return items

    def mark_bad(self, proxy: str) -> None:
        with self._lock:
            self._bad.add(proxy)

    def __len__(self) -> int:
        return len(self._all)


_POOL: ProxyPool | None = None
_CFG: dict = {}
_INSTALLED = False


def _read_file(file) -> tuple[bytes, str]:
    if isinstance(file, (str, Path)):
        fp = Path(file)
        if not fp.is_file():
            raise ValueError(f"{fp} is not a valid file.")
        return fp.read_bytes(), fp.name
    if isinstance(file, io.BytesIO):
        return file.getvalue(), _generate_random_name()
    if isinstance(file, bytes):
        return file, _generate_random_name()
    raise ValueError(f"Unsupported file type: {type(file)}")


def _request_headers(push_id: str) -> dict:
    return {**Headers.REFERER.value, **Headers.UPLOAD.value, "Push-ID": push_id}


async def _post_once(data: bytes, filename: str, push_id: str, proxy: str, with_cookies, timeout: float) -> str:
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    mime = CurlMime()
    mime.addpart(name="file", content_type=content_type, filename=filename, data=data)
    kwargs: dict = {"impersonate": BROWSER_TYPE, "proxy": proxy}
    if with_cookies:
        kwargs["cookies"] = with_cookies
    try:
        async with AsyncSession(**kwargs) as session:
            resp = await session.post(
                url=Endpoint.UPLOAD,
                headers=_request_headers(push_id),
                multipart=mime,
                allow_redirects=True,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.text
    finally:
        mime.close()


async def proxy_upload_file(file, client, push_id: str, filename: str | None = None, verbose: bool = False) -> str:
    """Bản thay thế `gemini_webapi.utils.upload_file.upload_file` đi qua proxy."""
    global _POOL, _CFG
    data, auto_name = _read_file(file)
    name = filename or auto_name
    timeout = float(_CFG.get("timeout") or 30)

    if _POOL is None or not len(_POOL):
        return await _original_upload_file(file, client=client, push_id=push_id, filename=filename, verbose=verbose)

    cookies = None
    if _CFG.get("use_cookies", True):
        try:
            cookies = client.cookies
        except Exception:
            cookies = None

    last_err: Exception | None = None
    # Lượt 1: kèm cookie phiên. Lượt 2: bỏ cookie (một số proxy bị Google từ chối
    # khi ghép cookie phiên gắn với IP trực với IP proxy).
    attempts: list = [cookies] if cookies else [None]
    if cookies and _CFG.get("retry_without_cookies", True):
        attempts.append(None)

    for with_cookies in attempts:
        for proxy in _POOL.order():
            try:
                text = await _post_once(data, name, push_id, proxy, with_cookies, timeout)
                log.info(
                    "upload qua proxy OK (%s, %s, cookie=%s): %s",
                    proxy.split("@")[-1], name, bool(with_cookies), (text or "")[:60],
                )
                return text
            except Exception as e:  # noqa: BLE001 - thử proxy kế tiếp
                last_err = e
                log.warning("upload qua proxy lỗi (%s, cookie=%s): %s", proxy.split("@")[-1], bool(with_cookies), e)
                _POOL.mark_bad(proxy)

    log.warning("Mọi proxy upload đều lỗi (%s); quay lại kết nối trực tiếp.", last_err)
    return await _original_upload_file(file, client=client, push_id=push_id, filename=filename, verbose=verbose)


def install(cfg: dict | None = None) -> bool:
    """Cài override. Trả về True nếu đã bật."""
    global _POOL, _CFG, _INSTALLED
    cfg = cfg or {}
    _CFG = cfg
    enabled = bool(cfg.get("enabled", False))
    if not enabled:
        log.info("upload_proxy: tắt (config).")
        return False
    files = cfg.get("files") or []
    _POOL = ProxyPool(files)
    if not len(_POOL):  # type: ignore[arg-type]
        log.warning("upload_proxy: không nạp được proxy nào; giữ đường trực tiếp.")
        return False
    if not _INSTALLED:
        import gemini_webapi.client as gw_client

        gw_client.upload_file = proxy_upload_file
        _INSTALLED = True
    log.info("upload_proxy: bật với %d proxy từ %d file.", len(_POOL), len(files))
    return True