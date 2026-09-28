#!/usr/bin/env python3
"""Log xoay vòng + che bí mật, phục vụ bảng điều khiển.

Ba việc:

1. **Ghi log ra file.** `run_server.bat` in ra console và không redirect, nên
   muốn xem lại log hôm qua thì không có gì để xem. Ở đây thêm một file xoay
   vòng, không thay console.
2. **Che bí mật trước khi hiện ra UI.** `gemini-auth.json` là phiên Google
   thật. Log là thứ hay bị paste lên issue/chat, nên `__Secure-1PSID`,
   `__Secure-1PSIDTS`, `SAPISID` và chuỗi `Bearer <key>` đều bị thay trước khi
   trả ra HTTP. Lưu ý: chỉ che khi *đọc lên UI*, file log trên đĩa vẫn giữ
   nguyên để phục vụ chẩn đoán - nếu không thì cũng mất tác dụng của việc ghi.
3. **Đuôi theo thời gian thực (SSE)** cho tab Log của panel.

Ghi log kiểu này thì `logging` của stdlib (server.py) và `loguru` (thư viện
gemini_webapi) phải cùng đổ một file. Chỉ handler stdlib được giữ file; loguru
chuyển tiếp record vào đó (xem `_forward_loguru_to_stdlib`) vì trên Windows hai
bên cùng mở một file là không khả thi với xoay vòng.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import time
from collections import deque
from pathlib import Path

_SENSITIVE_COOKIES = (
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "SAPISID",
    "APISID",
    "SID",
    "HSID",
    "SSID",
)
_COOKIE_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in _SENSITIVE_COOKIES) + r")=([^;\s\"']+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{8,})")
_MASK = "***"

_sink_id: int | None = None
_log_path: Path | None = None
_handler: logging.Handler | None = None  # handler duy nhất đang giữ file log


def redact(text: str) -> str:
    """Che cookie phiên và bearer token.

    Không phân biệt hoa thường: tên cookie trong header là case-sensitive nhưng
    log server lẫn lộn, và che nhầm (thành `***`) chỉ tốn vài byte còn không che
    thì lộ phiên Google thật.

    Tên ngắn như `SID` nằm cuối danh sách chỉ để dễ đọc: `\b` trong regex đã
    bảo đảm `__Secure-1PSID` được khớp trọn vẹn, không bị cắt thành `SID`.
    """
    if not text:
        return text
    out = _COOKIE_RE.sub(lambda m: f"{m.group(1)}={_MASK}", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)}{_MASK}", out)


def resolve_path(file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


def _file_handler(path: Path, level: str, max_bytes: int, backups: int) -> logging.Handler:
    """Handler DUY NHẤT được phép giữ file log.

    Dùng `RotatingFileHandler` chứ không phải `FileHandler`: nó đóng file cũ
    trước rồi mới đổi tên, nên xoay vòng chạy được trên Windows (FileHandler giữ
    handle không có FILE_SHARE_DELETE, khiến mọi lần đổi tên đều PermissionError).
    """
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=max(1024, int(max_bytes)), backupCount=max(1, int(backups)),
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    h.setLevel(level)
    h._lumi_file_handler = True  # đánh dấu handler của chính file này
    return h


def _forward_loguru_to_stdlib(message) -> None:
    """Đưa dòng log của loguru vào logger stdlib, thay vì loguru tự mở file.

    Trước đây loguru tự có sink riêng trỏ vào CÙNG file với stdlib. Hai bên cùng
    giữ một file là bất khả thi trên Windows: khi loguru đủ ngưỡng xoay vòng nó
    gọi `os.rename`, handle của stdlib chặn lại -> PermissionError, và loguru
    **bỏ rơi dòng log đó** (không ghi được, chỉ in traceback ra stderr). Đã tái
    hiện: sau ~2MB log, mọi dòng `gemini_webapi` biến mất.

    Nay chỉ handler stdlib giữ file, loguru chuyển tiếp qua đó, nên xoay vòng do
    một chủ sở hữu duy nhất lo và mọi dòng đều tới file.
    """
    rec = getattr(message, "record", None)
    level = logging.INFO
    if rec is not None:
        level = getattr(rec.get("level"), "no", logging.INFO)
    logging.getLogger("gemini_webapi").log(level, str(message).rstrip())


def attach_uvicorn(level: str = "INFO") -> None:
    """Gắn CHUNG handler file của root vào logger của uvicorn.

    Phải gọi SAU khi uvicorn tự cấu hình logging (trong `Server.run()`): cấu hình
    mặc định của uvicorn đặt `propagate: False` cho `uvicorn.error` và
    `uvicorn.access`, nên log truy cập không bao giờ nổi lên root để tới handler
    của ta. Gọi trước thì bị uvicorn ghi đè lúc khởi động.

    Dùng lại đúng instance handler của root chứ không tạo handler mới: nếu mỗi
    logger giữ một handle riêng thì xoay vòng lại trở thành cuộc đấu giữ file
    giữa các handler trên cùng Windows.

    Đặt `propagate = False` cho ba logger này sau khi gắn handler: nếu không,
    mỗi dòng log sẽ vào file hai lần (một lần từ handler của chính logger, một
    lần nữa qua root). Log ra console vẫn còn nguyên vì uvicorn tự gắn
    StreamHandler của riêng nó.
    """
    if _log_path is None:
        return
    root = logging.getLogger()
    shared = next((h for h in root.handlers if getattr(h, "_lumi_file_handler", False)), None)
    if shared is None:
        return
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(getattr(h, "_lumi_file_handler", False) for h in lg.handlers):
            lg.addHandler(shared)
        lg.propagate = False


def install(file: str, level: str = "INFO", max_bytes: int = 2_000_000, backups: int = 3) -> Path | None:
    """Đăng ký file log xoay vòng cho cả stdlib lẫn loguru. Trả về path.

    Gọi lại nhiều lần là an toàn: sink cũ được gỡ trước khi thêm sink mới, nên
    đổi `logging.file` từ panel không làm dồn log vào nhiều file.
    """
    global _sink_id, _log_path, _handler

    path = resolve_path(file)
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_lumi_file_handler", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    handler = _file_handler(path, level, max_bytes, backups)
    root.addHandler(handler)
    _handler = handler
    if root.level > logging.DEBUG or root.level == logging.NOTSET:
        root.setLevel(level)

    # set_log_level() của thư viện đã gỡ hết handler loguru, nên phải thêm lại
    # SAU khi server gọi nó - thứ tự này do server.py quyết định, xem setup_logging().
    # Sink dạng hàm (chuyển tiếp vào stdlib) chứ không phải `add(str(path))`: xem
    # _forward_loguru_to_stdlib để hiểu vì sao hai bên không được cùng giữ file.
    try:
        from gemini_webapi.utils.logger import logger as _glogger
    except Exception:  # pragma: no cover - thư viện chưa cài
        _glogger = None
    if _glogger is not None:
        if _sink_id is not None:
            try:
                _glogger.remove(_sink_id)
            except (ValueError, TypeError):
                pass
        _sink_id = _glogger.add(
            _forward_loguru_to_stdlib,
            level=level,
            format="{message}",
            filter=lambda record: record["extra"].get("name") == "gemini_webapi",
            catch=True,  # lỗi ghi log không được làm sập request
        )

    _log_path = path
    return path


def reopen() -> None:
    """Mở lại handler file từ vị trí 0, dùng sau khi cắt (nút 'Xoá log').

    Nếu chỉ cắt file bằng `write_text("")` mà không mở lại handler, handler vẫn
    ghi tiếp từ offset cũ, nên file sẽ có một đoạn byte NUL ở đầu cho tới khi vị
    trí ghi bắt kịp kích thước cũ.
    """
    h = _handler
    if h is None:
        return
    try:
        if h.stream is not None:
            h.stream.close()
        h.stream = h._open()
        h.stream.seek(0)
    except Exception:
        pass



def current_path() -> Path | None:
    return _log_path


def tail(lines: int = 400) -> list[str]:
    """N dòng cuối của file log, đã che bí mật. Rỗng nếu chưa có file."""
    path = _log_path
    if path is None or not path.is_file():
        return []
    lines = max(1, min(int(lines), 5000))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail_deque: deque[str] = deque(fh, maxlen=lines)
    except OSError:
        return []
    return [redact(l.rstrip("\n")) for l in tail_deque]


class Follower:
    """Đuôi file log, chỉ đẩy dòng mới, có nhịp tim để client biết còn sống.

    Cố ý KHÔNG giữ file mở giữa hai lần đọc, mở/đóng mỗi nhịp. Trên Windows,
    một file đang mở mà không có `FILE_SHARE_DELETE` sẽ không rename được: loguru
    xoay vòng log bằng cách đổi tên `lumi.log` -> `lumi.log.1`, nên nếu ta giữ
    handle thì chính panel đang xem log sẽ làm hỏng việc xoay vòng. Đọc kiểu
    này cũng tự xử lý được việc file bị cắt ngắn (nút "Xoá log") và bị thay thế.

    Ba tình huống phải xử lý, cả ba đều xảy ra thật:
      - file bị cắt về 0 byte: vị trí đọc nằm ngoài EOF mới, đọc sẽ ra rỗng mãi
        cho tới khi file mọc lại đủ lớn;
      - file bị xoay vòng (đổi inode, giữ nguyên tên): đọc tiếp handle cũ là đọc
        file không còn ai ghi vào;
      - file chưa tồn tại lúc bắt đầu (logging.file vừa đổi).
    """

    def __init__(self, path: Path | None = None, heartbeat: float = 15.0) -> None:
        self._path = path
        self._heartbeat = heartbeat
        self._pos = 0
        self._ident: tuple[int, int] | None = None

    def close(self) -> None:
        self._pos = 0
        self._ident = None

    def read_new(self) -> list[str]:
        path = self._path or _log_path
        if path is None:
            self.close()
            return []
        try:
            st = path.stat()
        except OSError:
            self.close()  # chưa có file; lần sau có là đọc tiếp từ đầu
            return []
        ident = (st.st_dev, st.st_ino)
        if ident != self._ident or st.st_size < self._pos:
            # File mới (xoay vòng) hoặc bị cắt: mọi dòng đều là dòng mới.
            self._pos = 0
            self._ident = ident
        if st.st_size == self._pos:
            return []
        out: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._pos)
                # Phải dùng readline(), KHÔNG dùng `for line in fh`: iterator của
                # file text mode có readahead buffer, nên sau lần đọc hết nó vẫn
                # trả về rỗng cho tới khi bộ đệm đầy lại - tức là dòng do chính
                # server ghi qua logging handler không bao giờ hiện trong panel.
                while len(out) < 500:
                    line = fh.readline()
                    if not line:
                        break
                    out.append(redact(line.rstrip("\n")))
                self._pos = fh.tell()
        except OSError:
            return out
        return out


import os  # noqa: E402,F401  (giữ import ở cuối như trước; đã dùng ở phần trên)


async def sse_lines(path: Path | None = None, heartbeat: float = 15.0):
    """Generator SSE: dòng log mới, kèm `: ping` định kỳ.

    Nhảy một dòng `: connected` ngay để client biết kết nối đã sống, nếu không
    panel sẽ im lặng cho tới lúc có dòng log đầu tiên.
    """
    follower = Follower(path, heartbeat)
    try:
        yield ": connected\n\n"
        last_ping = time.monotonic()
        while True:
            lines = follower.read_new()
            for line in lines:
                yield f"data: {line}\n\n"
            if lines:
                last_ping = time.monotonic()
            elif time.monotonic() - last_ping >= heartbeat:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(1.0)
    finally:
        follower.close()
