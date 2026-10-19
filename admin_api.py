#!/usr/bin/env python3
"""Bảng điều khiển: cấu hình, log, điều khiển Chrome agent, điều khiển server.

KHÔNG dùng chung `require_key` của /v1 với panel. Lý do: API key là thứ mọi
client OpenAI đều cầm, còn panel thì đọc/ghi được API key và chạy
`taskkill` trên Chrome. Nếu dùng chung một key thì bất kỳ client nào cũng đổi
được cấu hình và tắt Chrome.

Bảo mật của panel, ba lớp:

  1. `X-Lumi-Admin: <token>` bắt buộc. Header tùy biến này khiến trình duyệt
     phải preflight trước. Server KHÔNG trả `Access-Control-Allow-*` cho các
     route admin, nên preflight fail và trang web bất kỳ trong Chrome không gọi
     được. Đây là lớp quan trọng nhất.
  2. Token nằm ở *fragment* URL (`/admin#t=...`), nên nó không bao giờ tới server
     và không nằm trong access log. JS đọc rồi `history.replaceState` xoá ngay.
  3. Chặn `Origin` lạ. Same-origin thì Origin là `http://127.0.0.1:8000` (giống
     hệt `_is_extension_client` đang làm cho bootstrap, chỉ khác bên nhận diện).

Mọi thao tác trên Chrome đều chạy trong thread: chúng gọi PowerShell/WMI, mà
`taskkill` chặn khoảng nửa giây - chặn event loop thì mọi request khác đứng hình.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import chrome_launcher
import config_store
import logstore
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

log = logging.getLogger("lumi.admin")

BASE_DIR = Path(__file__).resolve().parent
ADMIN_HTML = BASE_DIR / "admin.html"
ADMIN_HEADER = "X-Lumi-Admin"
_START_TIME = time.time()

# Token đang hiệu lực. server.py nạp từ config lúc khởi động.
_admin_token = ""
# Đường dẫn config đang dùng (server.py set; mặc định là config.json).
_config_path: Path | None = None
# base_url của chính server này, để mở Chrome agent trỏ health_url vào đúng.
_base_url = ""

_browser_lock = asyncio.Lock()

# Những field chỉ có tác dụng lúc khởi động, nên đổi xong phải báo UI restart.
_RESTART_FIELDS = ("api_key", "host", "port", "auth_file")


class Control:
    """Khoá điều khiển vòng đời server, để không phải import vòng vèo với server.py."""

    def __init__(self) -> None:
        self.uvicorn: Any = None
        self.restart = False
        self.stop = False
        self.supervised = os.getenv("LUMI_SUPERVISED") == "1"
        self.state_provider: Callable[[], dict] | None = None

    def request_restart(self) -> None:
        self.restart = True
        if self.uvicorn is not None:
            self.uvicorn.should_exit = True

    def request_stop(self) -> None:
        self.stop = True
        if self.uvicorn is not None:
            self.uvicorn.should_exit = True


HOOK = Control()


def configure(token: str, base_url: str, config_path: Path | None = None) -> None:
    global _admin_token, _base_url, _config_path
    _admin_token = token or ""
    _base_url = base_url.rstrip("/")
    _config_path = config_path


def require_admin(request: Request, x_lumi_admin: str | None = Header(default=None)) -> None:
    """Xác thực panel. 401 khi thiếu/sai token, 403 khi Origin lạ."""
    if not _admin_token:
        raise HTTPException(status_code=404, detail="Admin panel is disabled.")
    if not x_lumi_admin or not hmac.compare_digest(x_lumi_admin, _admin_token):
        raise HTTPException(status_code=401, detail="Admin token sai hoặc thiếu.")
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin != _base_url:
        log.warning("Admin request bị chặn: origin=%r", origin)
        raise HTTPException(status_code=403, detail="Origin không được phép.")


router = APIRouter(prefix="/admin", include_in_schema=False)
# Dùng chung một list dependency cho mọi route API để không bỏ sót route nào.
_auth = [Depends(require_admin)]


def _load_cfg() -> config_store.AppConfig:
    """Đọc config, biến mọi lỗi thành HTTP có thông điệp đọc được.

    Không được để `ConfigError` lọt ra ngoài: nó là `ValueError`, FastAPI không
    có handler cho loại đó, và request sẽ treo vô hạn thay vì trả lỗi (đã tái
    hiện: config.json hỏng -> mọi route /admin/* treo, chỉ /api/logs/clear còn
    sống vì nó không đọc config).
    """
    try:
        return config_store.load(_config_path)
    except config_store.UnsafePathError as e:
        # Config trỏ vào Chrome cá nhân: đây là "cấu hình sai, sửa đi" chứ không
        # phải lỗi server -> 400 kèm lời giải thích, đừng trả 500 chung chung.
        log.error("%s", e)
        raise HTTPException(status_code=400, detail=str(e)) from None
    except config_store.ConfigError as e:
        log.error("config.json không hợp lệ: %s", e)
        raise HTTPException(status_code=500, detail=f"config.json không hợp lệ: {e}") from None


# ---------- Trang ----------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_page():
    """HTML của panel. Cố ý KHÔNG xác thực: nó không chứa bí mật nào, và token
    được truyền qua fragment nên không có gì để bảo vệ ở đây. Chỉ /admin/api/*
    mới cần token."""
    if not ADMIN_HTML.is_file():
        raise HTTPException(status_code=500, detail="Thiếu admin.html")
    return HTMLResponse(ADMIN_HTML.read_text(encoding="utf-8"))


# ---------- Trạng thái ----------
@router.get("/api/state", dependencies=_auth)
async def state():
    cfg = _load_cfg()
    out: dict[str, Any] = {
        "config": config_store.summary(cfg),
        "locked_fields": config_store.locked_fields(),
        "supervised": HOOK.supervised,
        "restart_supported": HOOK.supervised,
        "process": {
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()}",
            "uptime_sec": round(time.time() - _START_TIME),
            "cwd": str(BASE_DIR),
        },
        "supervisor": {
            "tray": HOOK.supervised,
            "note": None if HOOK.supervised else
                    "Server không chạy dưới tray.py nên không tự khởi động lại được. "
                    "Chạy `python tray.py` để có icon trên system tray.",
        },
    }
    if HOOK.state_provider is not None:
        try:
            out.update(HOOK.state_provider())
        except Exception as e:  # provider hỏng không được làm hỏng cả panel
            log.warning("state_provider lỗi: %s", e)
            out["provider_error"] = str(e)
    return out


_START_TIME = time.time()


# ---------- Cấu hình ----------
@router.get("/api/config", dependencies=_auth)
async def get_config():
    """Cấu hình thô đã che bí mật + trạng thái hợp lệ.

    Cố ý dùng `read_raw` chứ không `load`: endpoint này phải hiện được cả config
    đang hỏng, vì đó chính là lúc người dùng cần đọc nó để sửa. Nên nó báo kèm
    `valid`/`error` để UI hiện cảnh báo thay vì im lặng.
    """
    raw = config_store.read_raw(_config_path)
    out: dict = {
        "config": config_store.redact(raw),
        "locked_fields": config_store.locked_fields(),
        "path": str(_config_path or config_store.config_path()),
    }
    try:
        config_store.load(_config_path)
        out["valid"] = True
        out["error"] = None
    except config_store.ConfigError as e:
        out["valid"] = False
        out["error"] = str(e)
    return out


@router.patch("/api/config", dependencies=_auth)
async def patch_config(request: Request):
    """Ghi một phần config. Trả về config mới (đã che) + việc còn phải restart.

    Không tự restart ở đây. `host`/`port`/`api_key` chỉ có tác dụng lúc khởi
    động (uvicorn bind một lần), nên nói rõ với UI còn cần restart thay vì tự
    làm - tự restart là thứ không thể đảo ngược từ trong chính request đó.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body phải là JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body phải là object")
    patch = payload.get("patch", payload)
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="Thiếu trường 'patch'")
    before = config_store.read_raw(_config_path)
    try:
        config_store.update(patch, _config_path)
    except config_store.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except OSError as e:
        log.error("Ghi config lỗi: %s", e)
        raise HTTPException(status_code=500, detail=f"Không ghi được config: {e}") from None

    raw = config_store.read_raw(_config_path)
    # Tự so sánh trước/sau thay vì tin `before` từ client: client có thể gửi sai
    # (hoặc gửi cố ý để giấu việc cần restart), còn server thì không lừa được ai.
    restart_required = sorted(k for k in _RESTART_FIELDS if before.get(k) != raw.get(k))
    return {
        "config": config_store.redact(raw),
        "locked_fields": config_store.locked_fields(),
        "restart_required": restart_required,
    }


@router.post("/api/config/rotate", dependencies=_auth)
async def rotate_secret(request: Request):
    """Đổi `api_key` hoặc `admin_token`.

    Xoay token admin là một lỗ hổng dễ xảy ra nếu bạn từng mở panel trên máy
    công cộng; nên có sẵn nút này thay vì phải sửa tay file rồi restart.
    """
    try:
        which = (await request.json()).get("which", "")
    except Exception:
        which = ""
    path = _config_path
    raw = config_store.read_raw(path)
    if which == "api_key":
        raw["api_key"] = "sk-lumi-" + secrets.token_urlsafe(24)
    elif which == "admin_token":
        raw["admin_token_issued_at"] = time.time()
        raw.setdefault("admin", {})["token"] = secrets.token_urlsafe(32)
    else:
        raise HTTPException(status_code=400, detail="which phải là api_key hoặc admin_token")
    try:
        config_store.save(raw, path)
    except config_store.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if which == "admin_token":
        # Token mới phải có hiệu lực ngay, không cần restart: client hiện tại sẽ
        # bị 401 và phải nhập lại (mở lại từ tray là xong).
        configure(raw["admin"]["token"], _base_url, path)
    return {"status": "ok", "which": which, "config": config_store.redact(config_store.read_raw(path))}


# ---------- Log ----------
@router.get("/api/logs", dependencies=_auth)
async def get_logs(lines: int = 400):
    path = logstore.current_path()
    return {
        "file": str(path) if path else None,
        "lines": logstore.tail(lines),
        "redacted": True,
    }


@router.get("/api/logs/stream", dependencies=_auth)
async def stream_logs():
    return StreamingResponse(
        logstore.sse_lines(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/api/logs/clear", dependencies=_auth)
async def clear_logs():
    """Xoá file log. Giữ nguyên handler nên log vẫn ghi tiếp, chỉ mất lịch sử.

    Sau khi cắt phải `logstore.reopen()`: handler đang giữ vị trí ghi ở offset cũ,
    nếu không mở lại thì file sẽ có một đoạn byte NUL ở đầu cho tới khi vị trí
    ghi bắt kịp kích thước cũ (vài KB rác trong tab Log).
    """
    path = logstore.current_path()
    if path is None or not path.is_file():
        return {"status": "skipped", "reason": "chưa có file log"}
    try:
        with path.open("w", encoding="utf-8"):
            pass
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Không xoá được log: {e}") from None
    logstore.reopen()
    return {"status": "ok", "file": str(path)}


# ---------- Chrome agent / profile ----------
def _browser_cfg() -> dict:
    """Cấu hình browser đang hiệu lực + health_url trỏ vào chính server này.

    Đọc lại từ đĩa mỗi lần gọi để thay đổi trên panel có tác dụng ngay, thay vì
    phải khởi động lại server mới ăn.
    """
    cfg = _load_cfg()
    bcfg = cfg.browser.model_dump()
    bcfg["health_url"] = f"{_base_url}/health" if _base_url else bcfg.get("health_url", "")
    return bcfg


def _agent_profile(bcfg: dict) -> chrome_launcher.ChromeProfile:
    return chrome_launcher.resolve_agent_profile(bcfg)


@router.get("/api/profiles", dependencies=_auth)
async def profiles():
    """Tình trạng profile agent + Chrome đang chạy.

    Phần "Chrome cá nhân" chỉ để đối chiếu, server không bao giờ đụng tới: agent
    có User Data riêng, và `close_agent_chrome` lọc theo đúng cờ
    `--user-data-dir` của agent.
    """
    bcfg = _browser_cfg()
    out: dict[str, Any] = {"agent": None, "personal_chrome": None, "extension_dir": None}
    try:
        target = await asyncio.to_thread(_agent_profile, bcfg)
    except Exception as e:
        out["error"] = str(e)
        return out

    ext_dir = str(bcfg.get("ext_dir") or chrome_launcher.EXT_DIR)

    def collect() -> dict[str, Any]:
        prof = target.profile_dir
        cookies = os.path.join(prof, "Network", "Cookies")
        auth_rel = _load_cfg().auth_file
        return {
            "user_data_dir": target.user_data_dir,
            "profile": target.name,
            "profile_dir": prof,
            "exists": os.path.isdir(prof),
            "local_state": os.path.isfile(os.path.join(target.user_data_dir, "Local State")),
            "cookies_db_bytes": os.path.getsize(cookies) if os.path.isfile(cookies) else 0,
            "running_pids": chrome_launcher.browser_pids(target.user_data_dir),
            "extension": chrome_launcher.extension_registration(prof, ext_dir),
            "auth_file": str(auth_rel),
            "auth_file_exists": (BASE_DIR / auth_rel).is_file(),
        }

    out["agent"] = await asyncio.to_thread(collect)
    out["extension_dir"] = ext_dir

    def personal() -> dict[str, Any]:
        mine = chrome_launcher.browser_pids(chrome_launcher.user_data_root())
        return {"count_any_chrome": len(mine), "touched_by_server": False}

    out["personal_chrome"] = await asyncio.to_thread(personal)
    return out


async def _close_chrome_bounded(bcfg: dict, user_data_dir: str) -> tuple[bool, str]:
    """Gọi `close_agent_chrome` có trần thời gian, trả về `(ok, note)`.

    `server.py` đã bọc hàm này bằng `asyncio.wait_for` (`_shutdown_agent_browser`),
    nhưng hai endpoint dưới đây thì không — mà chúng giữ `_browser_lock`, nên một
    lần treo là `/api/browser/start` và `/api/browser/restart` xếp hàng vô tận.

    Bên trong `close_agent_chrome` gọi `browser_pids()` tối đa 3 lần, mỗi lần
    spawn PowerShell (`timeout=15`). Nếu WMI kẹt thì `subprocess.run` ném
    `TimeoutExpired`; không bắt thì endpoint trả 500 thay vì thông báo cho
    người dùng biết phải xử lý tay.
    """
    budget = float(bcfg.get("close_timeout") or 25) + 60.0
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(
                chrome_launcher.close_agent_chrome,
                user_data_dir,
                float(bcfg.get("close_timeout") or 25),
            ),
            timeout=budget,
        )
        return bool(ok), ""
    except (TimeoutError, subprocess.TimeoutExpired):
        log.warning("close_agent_chrome treo quá %.0fs (%s)", budget, user_data_dir)
        return False, (
            f"Chrome không phản hồi trong {budget:.0f}s. Thử `python chrome_launcher.py --reset`."
        )
    except Exception as e:
        log.error("Lỗi khi đóng Chrome agent: %s", e)
        return False, f"Lỗi khi đóng Chrome agent: {e}"


@router.post("/api/browser/start", dependencies=_auth)
async def browser_start():
    bcfg = _browser_cfg()
    target = await asyncio.to_thread(_agent_profile, bcfg)

    async with _browser_lock:
        # keep_alive đọc trong config chỉ có tác dụng lúc khởi động, nên bật cả
        # override lúc chạy để supervisor không tự mở lại sau khi mình tắt.
        chrome_launcher.set_keep_alive(True)
        if chrome_launcher.browser_pids(target.user_data_dir):
            return {"status": "already-running", "pids": chrome_launcher.browser_pids(target.user_data_dir)}
        pid = await asyncio.to_thread(
            chrome_launcher.launch_profile,
            target.user_data_dir,
            str(bcfg.get("chrome_path") or ""),
            str(bcfg["hidden"]) if "hidden" in bcfg else "offscreen",
            str(bcfg.get("gemini_url") or chrome_launcher.GEMINI_URL),
            str(bcfg.get("ext_dir") or chrome_launcher.EXT_DIR),
            target.name,
        )
    return {"status": "started", "pid": pid, "profile": target.profile_dir}


@router.post("/api/browser/stop", dependencies=_auth)
async def browser_stop():
    """Đóng Chrome agent lịch sự (WM_CLOSE trước, /F sau timeout).

    Kill cứng làm Chrome không ghi `Secure Preferences` -> mất extension, phải
    `--setup` lại. Đó là lý do `close_agent_chrome` có thứ tự 3 bước đó.
    """
    bcfg = _browser_cfg()
    target = await asyncio.to_thread(_agent_profile, bcfg)
    default = await asyncio.to_thread(_is_default_root, target.user_data_dir)
    if default:
        raise HTTPException(
            status_code=400,
            detail=f"user_data_dir đang trỏ vào User Data mặc định của Chrome "
                   f"({target.user_data_dir}). Không đóng để tránh tắt nhầm Chrome cá nhân.",
        )
    async with _browser_lock:
        chrome_launcher.set_keep_alive(False)
        ok, note = await _close_chrome_bounded(bcfg, target.user_data_dir)
    out: dict[str, Any] = {"status": "stopped" if ok else "still-running",
                           "profile": target.user_data_dir}
    if note:
        out["note"] = note
    return out


@router.post("/api/browser/restart", dependencies=_auth)
async def browser_restart():
    await browser_stop()
    return await browser_start()


@router.post("/api/browser/setup", dependencies=_auth)
async def browser_setup():
    """Mở Chrome *hiện ra* ở chrome://extensions trong đúng User Data agent.

    Trả về ngay, không chờ: bản CLI chờ tới 5 phút, nhưng qua HTTP thì request
    đó sẽ treo và trông như server chết. Panel poll `/api/profiles` cho tới khi
    `extension.usable` thành true.
    """
    bcfg = _browser_cfg()
    target = await asyncio.to_thread(_agent_profile, bcfg)
    ext_dir = str(bcfg.get("ext_dir") or chrome_launcher.EXT_DIR)
    await asyncio.to_thread(os.makedirs, target.user_data_dir, exist_ok=True)

    def open_chrome() -> None:
        cmd = chrome_launcher.build_chrome_command(
            chrome_launcher.find_chrome_exe(),
            target.user_data_dir,
            hidden="",
            url="chrome://extensions",
            ext_dir=ext_dir,
            profile_directory=target.name,
        )
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)

    await asyncio.to_thread(open_chrome)
    return {
        "status": "opened",
        "profile": target.profile_dir,
        "extension_dir": ext_dir,
        "extension_id": chrome_launcher.extension_id_from_path(ext_dir),
        "steps": [
            "Bật Developer mode (góc trên bên phải).",
            f"Bấm 'Load unpacked' rồi chọn thư mục: {ext_dir}",
            "Mở gemini.google.com và đăng nhập Google (đúng profile này).",
            "Đóng Chrome. Từ đây chỉ cần bật server.",
        ],
    }


@router.post("/api/browser/reset", dependencies=_auth)
async def browser_reset(request: Request):
    """Xoá profile agent + phiên đồng bộ, để đổi tài khoản Google.

    Bắt buộc gửi `{"confirm": "xoa"}` - giống xác nhận gõ tay của CLI. Đây là
    thao tác xoá cả User Data, không nên chỉ cần một nút bấm.
    """
    try:
        confirm = (await request.json()).get("confirm", "")
    except Exception:
        confirm = ""
    if str(confirm).strip().lower() not in ("xoa", "x", "yes"):
        raise HTTPException(status_code=400, detail="Cần gửi {\"confirm\": \"xoa\"} để xác nhận.")

    bcfg = _browser_cfg()
    target = await asyncio.to_thread(_agent_profile, bcfg)
    if await asyncio.to_thread(_is_default_root, target.user_data_dir):
        raise HTTPException(status_code=400, detail="Từ chối xoá: đó là User Data mặc định của Chrome.")

    async with _browser_lock:
        chrome_launcher.set_keep_alive(False)
        if chrome_launcher.browser_pids(target.user_data_dir):
            ok, note = await _close_chrome_bounded(bcfg, target.user_data_dir)
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail=note or "Không đóng được Chrome agent. Đóng tay rồi thử lại.",
                )

        auth = BASE_DIR / str(_load_cfg().auth_file)

        def wipe() -> list[str]:
            removed = []
            if Path(target.user_data_dir).is_dir():
                shutil.rmtree(target.user_data_dir, ignore_errors=False)
                removed.append(target.user_data_dir)
            for extra in sorted(BASE_DIR.glob("gemini-auth.json.bak*")):
                try:
                    extra.unlink()
                    removed.append(str(extra))
                except OSError:
                    pass
            if auth.is_file():
                try:
                    auth.unlink()
                    removed.append(str(auth))
                except OSError as e:
                    log.error("Không xoá được %s: %s", auth, e)
            return removed

        try:
            removed = await asyncio.to_thread(wipe)
        except OSError as e:
            log.error("Reset lỗi: %s", e)
            raise HTTPException(status_code=500, detail=f"Không xoá được profile: {e}") from None
    return {
        "status": "reset",
        "removed": removed,
        "next": ["Bấm 'Mở Chrome để cài extension' và load unpacked lại.",
                 "Đăng nhập Google trong profile agent.",
                 "Bấm 'Khởi động lại server'."],
    }


def _is_default_root(user_data_dir: str) -> bool:
    """True nếu đường dẫn nằm trong hoặc chứa User Data mặc định của Chrome cá nhân.

    Dùng chung `config_store.touches` để khớp cả thư mục con: trỏ vào
    `User Data\\Default` cũng nguy hiểm không kém trỏ vào chính `User Data`.
    """
    try:
        return config_store.touches(user_data_dir, str(chrome_launcher.user_data_root()))
    except Exception:
        return False


# ---------- Vòng đời server ----------
@router.post("/api/server/restart", dependencies=_auth)
async def server_restart():
    """Yêu cầu supervisor khởi động lại server.

    Thoát bằng mã 75 (EX_TEMPFAIL) để tray.py biết đây là *cố ý* restart chứ
    không phải crash - nếu không thì tray sẽ tưởng server chết và bật lại theo
    chính sách supervise, hoặc tệ hơn là báo lỗi cho người dùng.
    """
    if not HOOK.supervised:
        raise HTTPException(
            status_code=409,
            detail="Server không chạy dưới tray.py nên không tự restart được. "
                   "Chạy `python tray.py` thay vì `run_server-debug.bat`.",
        )
    HOOK.request_restart()
    return {"status": "restarting"}


@router.post("/api/server/stop", dependencies=_auth)
async def server_stop():
    HOOK.request_stop()
    return {"status": "stopping"}
