#!/usr/bin/env python3
"""OpenAI-compatible HTTP server backed by the Gemini web app.

Exposes /v1/chat/completions and /v1/models so any OpenAI SDK / Agents SDK
client can point its base_url and api_key here and talk to Gemini under the
hood.

Khi config.json bật "browser.enabled", server tự clone profile Chrome đã đăng
nhập Google, mở Chrome ẩn vào gemini.google.com; extension "Gemini Cookie Sync"
trong profile đó tự lấy API key qua /v1/auth/bootstrap rồi tự đẩy cookie qua
/v1/auth/sync. Không cần mở trình duyệt hay bấm gì thủ công.

Environment variables:
  GEMINI_API_KEY    API key clients must send. A random one is generated
                    and printed at startup if this is not set.
  GEMINI_AUTH_FILE  Path to the auth JSON (default: ./gemini-auth.json).
  GEMINI_MODEL      Default Gemini model used when the caller requests a
                    name Gemini does not know (e.g. "gpt-4o-mini").
  GEMINI_PROXY      Optional proxy URL passed to GeminiClient.
  GEMINI_TEMPLATE_PROFILE  Chrome profile (User Data\\Default hoặc User Data\\Profile N)
                    dùng làm mẫu để clone; ghi đè browser.source_profile.
"""

import argparse
import asyncio
import base64
import hmac
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from curl_cffi import CurlFollow, CurlHttpVersion
from curl_cffi.requests import AsyncSession
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

if (_src := str(Path(__file__).resolve().parent / "src")) not in sys.path:
    sys.path.insert(0, _src)

from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import BROWSER_TYPE, AccountStatus
from gemini_webapi.exceptions import AuthError, TemporarilyBlockedError, UsageLimitExceededError

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
set_log_level("WARNING")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


def _load_config() -> dict:
    try:
        # utf-8-sig chứ không phải utf-8: Notepad và PowerShell 5.1 thêm BOM khi
        # lưu, json.loads sẽ ném JSONDecodeError và làm server không khởi động được.
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid config.json: {e}") from e


CONFIG = _load_config()

AUTH_FILE = Path(os.getenv("GEMINI_AUTH_FILE") or CONFIG.get("auth_file") or "gemini-auth.json")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL") or CONFIG.get("model") or None
PROXY = os.getenv("GEMINI_PROXY") or CONFIG.get("proxy") or None
STARTUP_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT") or CONFIG.get("timeout") or 60)
STARTUP_RETRIES = int(os.getenv("GEMINI_RETRIES") or CONFIG.get("retries") or 3)
REFRESH_INTERVAL = float(os.getenv("GEMINI_REFRESH_INTERVAL") or CONFIG.get("refresh_interval") or 300)
HEALER_INTERVAL = int(os.getenv("GEMINI_HEALER_INTERVAL") or CONFIG.get("healer_interval") or 30)
RELOAD_BACKOFF = (5, 8, 12, 20)
MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT") or CONFIG.get("max_concurrent") or 6)
MAX_QUEUE = int(os.getenv("GEMINI_MAX_QUEUE") or CONFIG.get("max_queue") or 50)
QUEUE_TIMEOUT = float(os.getenv("GEMINI_QUEUE_TIMEOUT") or CONFIG.get("queue_timeout") or 15)
RELOAD_COOLDOWN = float(os.getenv("GEMINI_RELOAD_COOLDOWN") or CONFIG.get("reload_cooldown") or 5)
DRAIN_GRACE = float(os.getenv("GEMINI_DRAIN_GRACE") or CONFIG.get("drain_grace") or 30)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from chrome_launcher import (
    browser_supervisor,
    close_agent_chrome,
    resolve_agent_profile,
    user_data_root,
)

BROWSER = CONFIG.get("browser") or {}
EXTENSION = CONFIG.get("extension") or {}
# Extension cần API key để gọi /v1/auth/sync, nhưng lấy key thì chưa có gì để chứng
# minh. Endpoint này trả cho extension chạy cùng máy. Tắt bằng
# config.json -> "extension": {"bootstrap": false}.
EXT_BOOTSTRAP = EXTENSION.get("bootstrap", True) is not False
# Tắt server (Ctrl+C) thì tắt luôn Chrome agent. Cửa sổ của nó đã bị đẩy ra ngoài
# màn hình + SW_HIDE nên người dùng không đóng được bằng tay; để lại sẽ thành
# tiến trình mồ côi giữ phiên Google đã đăng nhập. Đặt false nếu muốn giữ Chrome.
CLOSE_ON_EXIT = BROWSER.get("close_on_exit", True) is not False
CLOSE_TIMEOUT = float(os.getenv("GEMINI_CLOSE_TIMEOUT") or BROWSER.get("close_timeout") or 25)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_extension_client(request: Request, origin: str) -> bool:
    """Bootstrap chỉ phục vụ extension, nhưng KHÔNG thể dựa vào `Origin`.

    Theo Fetch spec, `Origin` KHÔNG được gửi cho request `GET`/`HEAD` ở chế độ
    `cors` khi request đó "simple". Extension gọi `GET /v1/auth/bootstrap` bằng
    `fetch()` mặc định, nên Chrome **không** gửi header này — server trả 403 và
    extension im lặng mãi, không bao giờ POST /v1/auth/sync. (Đã gặp thật.)

    Vì vậy phải kiểm tra theo thứ thứ tự:
      1. `Origin` là `chrome-extension://`  -> chắc chắn là extension.
      2. Không có `Origin` + peer là loopback -> chấp nhận. Đây chính là extension
         (xem giải thích ở trên).
      3. Còn lại -> từ chối. Điểm này mới là hàng rào thật sự có tác dụng: chặn
         trang web bất kỳ trong Chrome (Gemini, Google...) gọi vào localhost để lấy
         trộn key. Trang web luôn gửi `Origin` khi fetch chéo origin.
    """
    if origin.startswith("chrome-extension://"):
        return True
    host = request.client.host if request.client else ""
    return not origin and host in LOOPBACK_HOSTS

BIND_HOST = ""
BIND_PORT = 0


def _bind() -> tuple[str, int]:
    host = BIND_HOST or os.getenv("HOST") or CONFIG.get("host") or "127.0.0.1"
    port = BIND_PORT or int(os.getenv("PORT") or CONFIG.get("port") or 8000)
    return str(host), int(port)


def _base_url() -> str:
    host, port = _bind()
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def load_cookies(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("cookie"), str):
        cookies = {}
        for pair in raw["cookie"].split(";"):
            if "=" in pair and pair.strip():
                name, value = pair.strip().split("=", 1)
                if name and value:
                    cookies[name] = value
        return cookies
    if isinstance(raw, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        return dict(raw)
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), dict):
        return dict(raw["cookies"])
    raise RuntimeError(f"Unsupported auth file format: {path}")


def build_client() -> GeminiClient:
    cookies = load_cookies(AUTH_FILE)
    psid = cookies.get("__Secure-1PSID")
    psidts = cookies.get("__Secure-1PSIDTS") or ""
    extra = {k: v for k, v in cookies.items() if k not in {"__Secure-1PSID", "__Secure-1PSIDTS"}}
    if not psid:
        raise RuntimeError("Missing __Secure-1PSID in auth file.")
    return GeminiClient(
        secure_1psid=psid,
        secure_1psidts=psidts,
        cookies=extra or None,
        proxy=PROXY,
        verify=True,
    )


def write_auth_payload(
    cookie_string: str,
    sapisid: str | None = None,
    auth_user: str | None = None,
    xsrf_token: str | None = None,
    gemini_bl: str | None = None,
) -> bool:
    payload = {
        "cookie": cookie_string,
        "sapisid": sapisid,
        "auth_user": auth_user,
        "xsrf_token": xsrf_token,
        "gemini_bl": gemini_bl,
    }
    try:
        tmp = AUTH_FILE.with_name(AUTH_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, AUTH_FILE)
        return True
    except OSError as e:
        logging.error("Failed to write auth file: %s", e)
        return False


# ---------- Xác thực: chỉ dựa vào extension ----------
# Server KHÔNG đọc cookie Google từ ổ đĩa (browser profile) nữa. Chrome v127+
# mã hóa database cookie bằng App-Bound Encryption nên cách đọc trực tiếp không
# còn dùng được chắc chắn. Toàn bộ phiên được duy trì qua extension
# "Gemini Cookie Sync": extension đẩy cookie tươi tới /v1/auth/sync, server ghi
# gemini-auth.json, và auth watchdog bên dưới tự reload Gemini client ngay khi
# file đổi mtime.


client: GeminiClient | None = None
client_ready = False
_client_lock: asyncio.Lock | None = None
_last_auth_mtime: float = 0.0


class _QueueFull(Exception):
    """Quá nhiều request đang chờ slot concurrency."""


class _QueueTimeout(Exception):
    """Hết thời gian chờ slot concurrency."""


class _ConcurrencyGuard:
    """Bounded concurrency + hàng đợi có giới hạn + theo dõi request đang chạy (drain).

    __aenter__ có thể raise _QueueFull / _QueueTimeout; stream phải giữ slot trong
    suốt vòng đời để reload chỉ đóng client cũ khi không còn ai đang dùng nó.
    """

    def __init__(self, max_concurrent: int, max_queue: int, queue_timeout: float) -> None:
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._max_queue = max_queue
        self._queue_timeout = queue_timeout
        self._waiters = 0
        self._active = 0
        self._no_active = asyncio.Event()
        self._no_active.set()

    async def __aenter__(self) -> "_ConcurrencyGuard":
        self._waiters += 1
        if self._waiters > self._max_queue:
            self._waiters -= 1
            raise _QueueFull()
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._queue_timeout)
        except asyncio.TimeoutError:
            raise _QueueTimeout() from None
        finally:
            self._waiters -= 1
        self._active += 1
        self._no_active.clear()
        return self

    async def __aexit__(self, *_exc) -> None:
        self._sem.release()
        self._active -= 1
        if self._active == 0:
            self._no_active.set()


_guard: _ConcurrencyGuard | None = None
_reload_running = False
_reload_pending = False
_last_reload_done = 0.0
_last_reload_ok = False
_flight_lock = asyncio.Lock()
_in_flight: dict[int, int] = {}

API_KEY = os.getenv("GEMINI_API_KEY") or CONFIG.get("api_key") or ""
if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    logging.warning(
        "GEMINI_API_KEY not set - generated API key: %s\n"
        "Set it in config.json (api_key) or the GEMINI_API_KEY env var.",
        API_KEY,
    )


async def client_init() -> None:
    global client, client_ready, _last_auth_mtime
    try:
        _last_auth_mtime = AUTH_FILE.stat().st_mtime
    except OSError:
        _last_auth_mtime = 0.0
    try:
        client = build_client()
    except Exception as e:
        logging.error("Failed to build Gemini client from %s: %s", AUTH_FILE, e)
        client_ready = False
        return
    client.refresh_interval = REFRESH_INTERVAL
    last_err = None
    for attempt in range(1, STARTUP_RETRIES + 1):
        try:
            await client.init(timeout=STARTUP_TIMEOUT, auto_refresh=True)
            last_err = None
            break
        except AuthError as e:
            logging.error("Gemini authentication failed: %s", e)
            client_ready = False
            return
        except Exception as e:
            last_err = e
            logging.warning("Gemini init attempt %d/%d failed: %s", attempt, STARTUP_RETRIES, e)
            if attempt < STARTUP_RETRIES:
                await asyncio.sleep(5)
    if last_err is not None:
        logging.error("Gemini init failed after %d attempts: %s", STARTUP_RETRIES, last_err)
        client_ready = False
        return
    client_ready = True
    models = client.list_models()
    if models:
        logging.info("Authenticated. Available models: %s", ", ".join(m.model_name for m in models))
    else:
        logging.info("Authenticated (no model list reported).")


async def reload_client() -> bool:
    """Rebuild the Gemini client from the current auth file.

    On failure the existing client is kept untouched. Returns True when the
    client was replaced with a freshly authenticated one.
    """
    global client, client_ready, _last_auth_mtime
    lock = _client_lock
    if lock is None:
        return False
    async with lock:
        last_err: Exception | None = None
        candidate: GeminiClient | None = None
        for attempt, delay in enumerate((0,) + RELOAD_BACKOFF):
            if attempt > 0:
                await asyncio.sleep(delay + random.uniform(0, 3))
            try:
                if candidate is not None:
                    try:
                        await candidate.close()
                    except Exception:
                        pass
                candidate = build_client()
                candidate.refresh_interval = REFRESH_INTERVAL
                await candidate.init(timeout=STARTUP_TIMEOUT, auto_refresh=True)
                if candidate.account_status == AccountStatus.UNAUTHENTICATED:
                    raise AuthError("Session is not authenticated")
                last_err = None
                break
            except AuthError as e:
                # Google xoay __Secure-1PSIDTS ngay khi extension đẩy cookie;
                # cặp vừa đẩy có thể bị reject thoáng qua rồi tự hồi phục.
                last_err = e
                logging.warning(
                    "Auth reload attempt %d/%d still unauthenticated; retrying.",
                    attempt + 1, 1 + len(RELOAD_BACKOFF),
                )
            except Exception as e:
                last_err = e
                logging.warning(
                    "Auth reload attempt %d/%d failed (transient): %s; retrying.",
                    attempt + 1, 1 + len(RELOAD_BACKOFF), e,
                )

        if last_err is not None or candidate is None:
            logging.error(
                "New auth credentials rejected after %d attempts; keeping current client: %s",
                1 + len(RELOAD_BACKOFF), last_err,
            )
            if candidate is not None:
                try:
                    await candidate.close()
                except Exception:
                    pass
            return False
        old = client
        client = candidate
        client_ready = True
        try:
            _last_auth_mtime = AUTH_FILE.stat().st_mtime
        except OSError:
            pass
        if old is not None:
            if _in_flight.get(id(old), 0) > 0:
                logging.info(
                    "Client swapped; %d request(s) still on the old client - closing when idle, not blocking reload.",
                    _in_flight.get(id(old), 0),
                )
            asyncio.create_task(_close_old_when_idle(old))
        models = client.list_models()
        if models:
            logging.info("Client reloaded. Models: %s", ", ".join(m.model_name for m in models))
        else:
            logging.info("Client reloaded (no model list reported).")
        return True


async def _request_reload() -> bool:
    """Single-flight reload với cooldown; trả kết quả reload gần nhất.

    Nếu một reload đang chạy, caller chờ nó xong và nhận kết quả thay vì khởi
    động reload thứ hai. Nếu reload thất bại sẽ tự thử lại sau cooldown.
    """
    global _reload_running, _reload_pending, _last_reload_done, _last_reload_ok
    _reload_pending = True
    if _reload_running:
        while _reload_running:
            await asyncio.sleep(0.2)
        return _last_reload_ok
    _reload_running = True
    try:
        while _reload_pending:
            _reload_pending = False
            elapsed = time.monotonic() - _last_reload_done
            if elapsed < RELOAD_COOLDOWN:
                await asyncio.sleep(RELOAD_COOLDOWN - elapsed)
            ok = await reload_client()
            _last_reload_ok = ok
            if not ok:
                logging.warning("Auth reload failed; will retry after cooldown.")
                _reload_pending = True
            _last_reload_done = time.monotonic()
    finally:
        _reload_running = False
    return _last_reload_ok


async def _ensure_client() -> None:
    """Kích reload khi session bị None (thư viện từng đóng session dùng chung)."""
    if client is None:
        await _request_reload()


async def _mark_flight(c) -> None:
    if c is None:
        return
    async with _flight_lock:
        _in_flight[id(c)] = _in_flight.get(id(c), 0) + 1


async def _unmark_flight(c) -> None:
    if c is None:
        return
    async with _flight_lock:
        n = _in_flight.get(id(c), 0) - 1
        if n > 0:
            _in_flight[id(c)] = n
        else:
            _in_flight.pop(id(c), None)


async def _close_old_when_idle(old, cap: float = 900.0) -> None:
    """Đóng client cũ khi KHÔNG còn stream nào của nó đang chạy.

    Không block reload: reload swap ngay, close đợi theo từng client. Cap dài
    (15 phút) chỉ để chống rò rỉ tài nguyên trong ca hiếm không bao giờ rảnh.
    """
    deadline = time.monotonic() + cap
    while _in_flight.get(id(old), 0) > 0:
        if time.monotonic() >= deadline:
            logging.warning("Previous client still busy after %.0fs; closing forcibly.", cap)
            break
        await asyncio.sleep(1.0)
    try:
        await old.close()
    except Exception:
        pass


async def auth_watchdog() -> None:
    """Detect gemini-auth.json changes on disk and reload the client."""
    while True:
        await asyncio.sleep(2.0)
        try:
            mtime = AUTH_FILE.stat().st_mtime
        except OSError:
            continue
        if mtime != _last_auth_mtime:
            _last_auth_mtime = mtime
            logging.info("Auth file %s changed on disk; reloading client.", AUTH_FILE)
            await _request_reload()


async def session_healer() -> None:
    """Chờ extension cập nhật cookie (chế độ extension-only).

    Khi phiên chết, server không đọc cookie từ ổ đĩa mà chỉ chờ extension đẩy
    cookie tươi qua /v1/auth/sync; auth watchdog đã tự reload client khi file
    gemini-auth.json đổi mtime. Task này chỉ đưa ra lời nhắc hướng dẫn để khỏi
    im lặng suốt thời gian chờ.
    """
    warned_at = 0.0
    while True:
        await asyncio.sleep(HEALER_INTERVAL)
        dead = (not client_ready) or (
            client is not None and client.account_status == AccountStatus.UNAUTHENTICATED
        )
        if not dead:
            warned_at = 0.0
            continue
        if time.monotonic() - warned_at >= 120:
            warned_at = time.monotonic()
            logging.warning(
                "Phiên Gemini chưa được xác thực. Nếu browser.enabled=true thì launcher sẽ tự "
                "mở Chrome agent ẩn; extension tự đẩy cookie trong ~30s. Nếu không, "
                "chạy 'python chrome_launcher.py --doctor' để xem extension đã nạp chưa, "
                "và đăng nhập Google trong profile agent."
            )


def _same_path(a: str, b: str) -> bool:
    """Hai đường dẫn có cùng một thư mục không (như cách chrome_launcher._norm so)."""
    def n(p: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expandvars(str(p)))))
    return n(a) == n(b)


async def _shutdown_agent_browser(browser_cfg: dict) -> None:
    """Tắt Chrome agent khi server dừng (Ctrl+C).

    Chỉ đóng Chrome đang chạy `user_data_dir` của agent — `close_agent_chrome` lọc
    bằng so khớp đường dẫn chuẩn hóa chính xác trên cờ `--user-data-dir`, nên Chrome
    cá nhân (User Data khác) không bị đụng tới.

    `close_agent_chrome` đóng lịch sự trước (WM_CLOSE, không /F) để Chrome kịp
    ghi `Secure Preferences`; chỉ `taskkill /F` khi quá `close_timeout`. Giết cứng
    làm Chrome prune entry extension unpacked -> phải load lại extension.

    Cả hai hàm đó gọi PowerShell (chặn ~0.5s/lần), nên chạy trong thread; có
    `wait_for` bao ngoài để một WMI treo không giữ server lại mãi.
    """
    try:
        udd = resolve_agent_profile(browser_cfg).user_data_dir
    except Exception as e:
        logging.error("Không xác định được User Data của Chrome agent để đóng: %s", e)
        return

    # `browser_pids` coi một Chrome không có cờ --user-data-dir là chủ User Data
    # mặc định. Nên nếu cấu hình trỏ nhầm vào User Data mặc định, Chrome cá nhân
    # của người dùng sẽ khớp và bị đóng. Chặn lại trước khi làm hỏng phiên duyệt.
    try:
        default_root = _same_path(udd, user_data_root())
    except Exception:
        default_root = False
    if default_root:
        logging.error(
            "browser.user_data_dir đang trỏ vào User Data mặc định của Chrome (%s) — "
            "KHÔNG đóng để tránh tắt nhầm Chrome cá nhân. Cần một User Data riêng cho agent.",
            udd,
        )
        return

    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(close_agent_chrome, udd, CLOSE_TIMEOUT),
            timeout=CLOSE_TIMEOUT + 15,
        )
    except asyncio.TimeoutError:
        logging.error(
            "Không đóng được Chrome agent trong %.0fs (User Data: %s). "
            "Thử: python chrome_launcher.py --reset",
            CLOSE_TIMEOUT, udd,
        )
        return
    except Exception as e:
        logging.error("Lỗi khi đóng Chrome agent: %s", e)
        return

    if ok:
        logging.info("Đã đóng Chrome agent (User Data: %s).", udd)
    else:
        logging.warning("Chrome agent còn tiến trình sau khi đóng: %s", udd)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client, _client_lock, _guard
    _client_lock = asyncio.Lock()
    _guard = _ConcurrencyGuard(MAX_CONCURRENT, MAX_QUEUE, QUEUE_TIMEOUT)
    logging.info(
        "Concurrency guard: max_concurrent=%d, max_queue=%d, queue_timeout=%ss",
        MAX_CONCURRENT, MAX_QUEUE, QUEUE_TIMEOUT,
    )
    await client_init()
    watchdog = asyncio.create_task(auth_watchdog())
    healer = asyncio.create_task(session_healer())
    browser_task: asyncio.Task | None = None
    browser_cfg: dict | None = None
    if BROWSER.get("enabled"):
        browser_cfg = dict(BROWSER)
        if not browser_cfg.get("health_url"):
            browser_cfg["health_url"] = f"{_base_url()}/health"
        browser_task = asyncio.create_task(browser_supervisor(browser_cfg))
        logging.info("Browser supervisor enabled: %s", browser_cfg)
    yield
    if browser_task is not None:
        # Phải chờ task kịp dừng hẳn trước khi đóng Chrome: nếu không, supervisor
        # còn đang ở giữa vòng `while True` sẽ thấy Chrome chết và mở lại.
        browser_task.cancel()
        with suppress(asyncio.CancelledError):
            await browser_task
    watchdog.cancel()
    healer.cancel()
    if client:
        await client.close()
    if browser_cfg is not None and CLOSE_ON_EXIT:
        await _shutdown_agent_browser(browser_cfg)
    elif browser_cfg is not None:
        with suppress(Exception):
            logging.info(
                "browser.close_on_exit=false: để Chrome agent chạy tiếp (User Data: %s).",
                resolve_agent_profile(browser_cfg).user_data_dir,
            )


app = FastAPI(
    title="Gemini-API OpenAI-compatible server",
    docs_url="/docs",
    openapi_url=None,
    lifespan=lifespan,
)


def require_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()
    if not key or not hmac.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _known_models() -> set[str]:
    models = client.list_models() if client else None
    if not models:
        return set()
    names = set()
    for m in models:
        names.update(x for x in (m.model_name, m.model_id, m.display_name) if x)
    return names


def resolve_model(requested: str | None) -> str | None:
    known = _known_models()
    if not known:
        return None

    def valid(name: str) -> bool:
        return name in known or name.lower() in {n.lower() for n in known}

    if requested and valid(requested):
        return requested
    if DEFAULT_MODEL and valid(DEFAULT_MODEL):
        return DEFAULT_MODEL
    return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def _iter_images(content):
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        img = part.get("image_url")
        if isinstance(img, dict):
            url = img.get("url")
        elif isinstance(img, str):
            url = img
        else:
            url = part.get("image_url")
        if url:
            yield url


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_TMP_DIR = Path(tempfile.mkdtemp(prefix="gemini_server_"))


def _content_type_ext(content_type: str | None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip().lower())
        if ext in _IMAGE_EXTS:
            return ext
    return None


def _sniff_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def _url_ext(src: str) -> str | None:
    path = urlparse(src).path.lower()
    return next((e for e in _IMAGE_EXTS if path.endswith(e)), None)


async def _load_image(src: str) -> tuple[bytes, str]:
    if src.startswith("data:"):
        head, _, b64 = src.partition(",")
        data = base64.b64decode(b64)
        mime = head.split(":", 1)[1].split(";", 1)[0] if ":" in head else ""
        ext = _content_type_ext(mime) or _sniff_ext(data)
        return data, ext
    parsed = urlparse(src)
    if parsed.scheme in ("http", "https"):
        async with AsyncSession(
            impersonate=BROWSER_TYPE,
            allow_redirects=CurlFollow.SAFE,
            http_version=CurlHttpVersion.NONE,
        ) as session:
            resp = await session.get(src)
            resp.raise_for_status()
            ext = _content_type_ext(resp.headers.get("content-type")) or _url_ext(src) or _sniff_ext(resp.content)
            return resp.content, ext
    raise ValueError(f"Cannot load image source: {src}")


async def _prepare_files(images: list[str]) -> tuple[list[str], list[Path]]:
    files, tmpfiles = [], []
    for src in images:
        data, ext = await _load_image(src)
        tmp = _TMP_DIR / f"{uuid.uuid4().hex}{ext}"
        tmp.write_bytes(data)
        tmpfiles.append(tmp)
        files.append(str(tmp))
    return files, tmpfiles


def build_prompt(messages: list[dict]) -> str:
    system_parts, turns = [], []
    for msg in messages:
        role = msg.get("role", "user")
        text = _text_of(msg.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
            continue
        if role == "assistant":
            bits = []
            if text:
                bits.append(text.rstrip())
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                bits.append(
                    "```tool_call\n"
                    + json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
                    + "\n```"
                )
            if bits:
                turns.append("[Assistant]: " + "\n".join(bits))
            continue
        if role in ("tool", "function"):
            if text:
                turns.append(
                    "[Tool result for %s]: %s" % (msg.get("name") or "tool", text.rstrip())
                )
            continue
        if text:
            turns.append(text.rstrip())
    prompt = "\n".join(turns)
    if system_parts:
        prompt = (
            "[System instruction]: " + " ".join(system_parts) + ("\n\n" + prompt if prompt else "")
        )
    return prompt.strip()


CHAT_HTML = Path(__file__).with_name("chat.html")


@app.get("/", response_class=HTMLResponse)
async def index():
    return CHAT_HTML.read_text(encoding="utf-8")


@app.get("/health")
async def health():
    try:
        mtime = AUTH_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "status": "ok" if client_ready else "unauthenticated",
        "model": DEFAULT_MODEL,
        "cookie_source": "extension",
        "account_status": client.account_status.name if client else None,
        "auth_age_sec": round(time.time() - mtime, 1) if mtime else None,
    }


@app.get("/v1/auth/bootstrap", include_in_schema=False)
async def auth_bootstrap(request: Request):
    """Cho extension lấy base_url + API key mà không cần thao tác tay.

    Vòng lặp: extension cần key để POST /v1/auth/sync, nên phải có đường nối để
    lấy key trước. Endpoint này không yêu cầu auth nhưng chỉ trả lời extension
    (Origin phải là chrome-extension://) và chỉ khi server chạy, tắt được bằng
    config.json -> "extension": {"bootstrap": false}.
    """
    if not EXT_BOOTSTRAP:
        raise HTTPException(status_code=404, detail="Bootstrap disabled in config.")
    origin = (request.headers.get("origin") or "").strip()
    if not _is_extension_client(request, origin):
        # Ghi lại ai đã bị chặn, nếu không thì 403 này không tìm ra nguyên nhân được.
        # Triệu chứng: extension im lặng, không bao giờ POST /v1/auth/sync.
        logging.warning(
            "Bootstrap rejected: origin=%r client=%r sec_fetch_site=%r",
            origin or "(khong co)",
            request.client.host if request.client else "?",
            request.headers.get("sec-fetch-site") or "(khong co)",
        )
        raise HTTPException(status_code=403, detail="Extension clients only.")
    return {
        "base_url": _base_url(),
        "api_key": API_KEY,
        "auto_sync": EXTENSION.get("auto_sync", True) is not False,
        "sync_max_age_sec": int(EXTENSION.get("sync_max_age_sec", 180)),
        "server_pid": os.getpid(),
    }


@app.get("/v1/models", dependencies=[Depends(require_key)])
async def list_models():
    models = client.list_models() if client and client_ready else None
    data = [
        {
            "id": m.model_name,
            "object": "model",
            "created": 0,
            "owned_by": "gemini",
            "display_name": m.display_name,
        }
        for m in (models or [])
    ]
    return {"object": "list", "data": data}


_AUTH_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


class AuthSyncRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    cookie: str = ""
    sapisid: str | None = None
    auth_user: str | None = None
    xsrf_token: str | None = None
    gemini_bl: str | None = None


@app.options("/v1/auth/sync", include_in_schema=False)
async def auth_sync_preflight():
    return JSONResponse(content=None, headers=_AUTH_CORS)


@app.post("/v1/auth/sync")
async def auth_sync(request: Request, req: AuthSyncRequest):
    try:
        require_key(
            authorization=request.headers.get("authorization"),
            x_api_key=request.headers.get("x-api-key"),
        )
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.detail}},
            headers=_AUTH_CORS,
        )

    cookie_string = (req.cookie or "").strip()
    if not cookie_string:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Missing cookie string."}},
            headers=_AUTH_CORS,
        )
    if "__Secure-1PSID=" not in cookie_string:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Cookie string is missing __Secure-1PSID."}},
            headers=_AUTH_CORS,
        )

    if not write_auth_payload(
        cookie_string,
        sapisid=req.sapisid,
        auth_user=req.auth_user,
        xsrf_token=req.xsrf_token,
        gemini_bl=req.gemini_bl,
    ):
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Failed to write auth file."}},
            headers=_AUTH_CORS,
        )

    # Ghi mtime sớm để watchdog dedupe không bắn reload thứ hai song song.
    try:
        _last_auth_mtime = AUTH_FILE.stat().st_mtime
    except OSError:
        pass

    if not await _request_reload():
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Client did not accept the new cookies; keeping the previous session."}},
            headers=_AUTH_CORS,
        )
    return JSONResponse(
        content={"status": "ok", "cookies": cookie_string.count(";") + 1},
        headers=_AUTH_CORS,
    )


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict]
    stream: bool = False
    tools: list[dict] = []


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _openai_message(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _func_names(tools: list[dict]) -> set[str]:
    names = set()
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t) if t.get("type") == "function" else t
        name = fn.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _tools_block(tools: list[dict]) -> str:
    lines = [
        "You can use the tools below when asked.",
        "To call a tool, respond with EXACTLY one fenced code block tagged `tool_call` "
        "containing a single JSON object shaped like this:",
        "```tool_call",
        '{"name": "<tool name>", "arguments": {<arguments matching its schema>}}',
        "```",
        "Only use the tool_call block when a tool call is required.",
        "",
        "Available tools:",
    ]
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t) if t.get("type") == "function" else t
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description", "")
        schema = json.dumps(fn.get("parameters") or {"type": "object", "properties": {}}, ensure_ascii=False)
        lines.append(f"- {name}: {desc}\n  Schema: {schema}")
    return "\n".join(lines)


_TOOL_CALL_BLOCK_RE = re.compile(r"```tool_call\s*\n(.*?)\n```", re.DOTALL)


def _parse_tool_call(text: str, names: set[str]) -> dict | None:
    if not names:
        return None

    def valid(obj) -> dict | None:
        if isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj["name"] in names:
            args = obj.get("arguments") or {}
            return {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}
        return None

    for m in _TOOL_CALL_BLOCK_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        hit = valid(obj)
        if hit:
            return hit

    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\s*", "", t).strip()
        t = re.sub(r"```\s*$", "", t).strip()
    candidates = []
    try:
        candidates.append(json.loads(t))
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            candidates.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    for obj in candidates:
        hit = valid(obj)
        if hit:
            return hit
    return None


async def _wait_for_client(timeout: float = 15.0) -> bool:
    """Wait briefly for a re-authentication to land before rejecting a request."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            client_ready
            and client is not None
            and client.account_status != AccountStatus.UNAUTHENTICATED
        ):
            return True
        if client is None:
            asyncio.create_task(_ensure_client())
        await asyncio.sleep(1.0)
    return bool(
        client_ready and client is not None and client.account_status != AccountStatus.UNAUTHENTICATED
    )


def _as_http_error(e: Exception) -> HTTPException:
    """Map lỗi generate sang mã HTTP chuẩn để client backoff đúng."""
    msg = str(e) or e.__class__.__name__
    m = msg.lower()
    if isinstance(e, _QueueFull):
        return HTTPException(status_code=429, detail="Too many concurrent requests queued.",
                             headers={"Retry-After": "2"})
    if isinstance(e, _QueueTimeout):
        return HTTPException(status_code=429, detail="Timed out waiting for a concurrency slot.",
                             headers={"Retry-After": "2"})
    if isinstance(e, UsageLimitExceededError) or "usage limit" in m or "quota" in m:
        return HTTPException(status_code=429, detail=msg, headers={"Retry-After": "60"})
    if isinstance(e, TemporarilyBlockedError) or ("temporarily" in m and "blocked" in m):
        return HTTPException(status_code=503, detail=msg, headers={"Retry-After": "120"})
    if isinstance(e, AuthError) or "unauthenticated" in m or "not initialized" in m:
        return HTTPException(status_code=503, detail=msg, headers={"Retry-After": "5"})
    return HTTPException(status_code=503, detail=msg, headers={"Retry-After": "5"})


async def _generate(
    messages: list[dict], tools: list[dict], requested_model: str | None
) -> tuple[str | None, dict | None, str | None]:
    if not await _wait_for_client():
        raise HTTPException(status_code=503, detail="Gemini client unavailable (re-authenticating).",
                            headers={"Retry-After": "5"})
    prompt = build_prompt(messages)
    if tools:
        block = _tools_block(tools)
        prompt = (prompt + "\n\n" + block) if prompt else block
    if not prompt:
        raise HTTPException(status_code=400, detail="No text content in messages.")

    images = []
    for msg in messages:
        images.extend(_iter_images(msg.get("content")))

    files, tmpfiles = [], []
    if images:
        try:
            files, tmpfiles = await _prepare_files(images)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logging.error("Image download failed: %s", e)
            raise HTTPException(status_code=400, detail=f"Image download failed: {e}")

    model = resolve_model(requested_model)
    c = None
    try:
        guard = _guard
        if guard is None:
            raise HTTPException(status_code=503, detail="Server still starting.",
                                headers={"Retry-After": "2"})
        async with guard:
            if not await _wait_for_client():
                raise HTTPException(status_code=503, detail="Gemini client unavailable (re-authenticating).",
                                    headers={"Retry-After": "5"})
            c = client
            if c is None:
                await _ensure_client()
                c = client
            await _mark_flight(c)
            output = await c.generate_content(
                prompt,
                files=files or None,
                model=model,
            )
    except (_QueueFull, _QueueTimeout) as e:
        logging.warning("Concurrency guard rejected generate: %s", e)
        raise _as_http_error(e) from None
    except HTTPException:
        raise
    except Exception as e:
        logging.error("Generate failed: %s", e)
        raise _as_http_error(e)
    finally:
        for tmp in tmpfiles:
            tmp.unlink(missing_ok=True)
        await _unmark_flight(c)

    text = output.text
    tool_call = _parse_tool_call(text, _func_names(tools))
    if tool_call:
        return None, tool_call, model
    return text, None, model


def _completion_body(cid, created, model, text, tool_call, stream):
    if tool_call:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": json.dumps(tool_call["arguments"], ensure_ascii=False),
                    },
                }
            ],
        }
        finish = "tool_calls"
    else:
        message = _openai_message(text or "")
        finish = "stop"

    deltas = []
    cn = f"chatcmpl-{uuid.uuid4().hex}"
    if stream:
        deltas.append(
            ({"role": "assistant", "finish_reason": None}, None)
        )
        if tool_call:
            deltas.append(({"tool_calls": message["tool_calls"]}, finish))
        else:
            deltas.append(({"content": text or ""}, "stop"))

        async def stream_body():
            for delta, fr in deltas:
                yield f"data: {json.dumps({'id': cn, 'object': 'chat.completion.chunk', 'created': created, 'model': model or 'gemini', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': fr}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_body(), media_type="text/event-stream")

    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model or "gemini",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
async def chat_completions(req: ChatRequest):
    cid = _completion_id()
    created = int(time.time())

    if req.tools:
        text, tool_call, model = await _generate(req.messages, req.tools, req.model)
        return _completion_body(cid, created, model, text, tool_call, stream=req.stream)

    prompt = build_prompt(req.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="No text content in messages.")

    images = []
    for msg in req.messages:
        images.extend(_iter_images(msg.get("content")))

    files, tmpfiles = [], []
    if images:
        try:
            files, tmpfiles = await _prepare_files(images)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logging.error("Image download failed: %s", e)
            raise HTTPException(status_code=400, detail=f"Image download failed: {e}")

    model = resolve_model(req.model)

    if req.stream:
        guard = _guard
        if guard is None:
            raise HTTPException(status_code=503, detail="Server still starting.",
                                headers={"Retry-After": "2"})
        try:
            await guard.__aenter__()
        except (_QueueFull, _QueueTimeout) as e:
            raise _as_http_error(e) from None

        async def stream():
            c = None
            try:
                yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': req.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
                if not await _wait_for_client():
                    raise HTTPException(status_code=503, detail="Gemini client unavailable (re-authenticating).",
                                        headers={"Retry-After": "5"})
                c = client
                if c is None:
                    await _ensure_client()
                    c = client
                await _mark_flight(c)
                async for chunk in c.generate_content_stream(
                    prompt,
                    files=files or None,
                    model=model,
                ):
                    delta = chunk.text_delta
                    if delta:
                        yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': req.model, 'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]})}\n\n"
                yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': req.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            except Exception as e:
                logging.error("Stream failed: %s", e)
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'server_error'}})}\n\n"
            finally:
                for tmp in tmpfiles:
                    tmp.unlink(missing_ok=True)
                await _unmark_flight(c)
                await guard.__aexit__(None, None, None)
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    c = None
    try:
        guard = _guard
        if guard is None:
            raise HTTPException(status_code=503, detail="Server still starting.",
                                headers={"Retry-After": "2"})
        async with guard:
            if not await _wait_for_client():
                raise HTTPException(status_code=503, detail="Gemini client unavailable (re-authenticating).",
                                    headers={"Retry-After": "5"})
            c = client
            if c is None:
                await _ensure_client()
                c = client
            await _mark_flight(c)
            output = await c.generate_content(
                prompt,
                files=files or None,
                model=model,
            )
    except (_QueueFull, _QueueTimeout) as e:
        logging.warning("Concurrency guard rejected chat completion: %s", e)
        raise _as_http_error(e) from None
    except HTTPException:
        raise
    except Exception as e:
        logging.error("Generate failed: %s", e)
        raise _as_http_error(e)
    finally:
        for tmp in tmpfiles:
            tmp.unlink(missing_ok=True)
        await _unmark_flight(c)

    return _completion_body(cid, created, model, output.text, None, stream=False)


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    instructions: str | None = None
    input: list | None = None
    tools: list[dict] = []
    stream: bool = False


def _responses_to_messages(instructions: str | None, input_items) -> list[dict]:
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    call_names = {}
    for item in input_items or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        role = item.get("role")
        if itype == "function_call" or role == "function_call":
            cid = item.get("call_id") or item.get("id")
            name = item.get("name") or "tool"
            if cid:
                call_names[cid] = name
            args = item.get("arguments") or ""
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid or f"call_{uuid.uuid4().hex[:16]}",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                    ],
                }
            )
        elif itype == "function_call_output" or role == "function_call_output":
            out = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "name": call_names.get(item.get("call_id")) or "tool",
                    "content": str(out) if out else "",
                }
            )
        elif itype == "message":
            text = _text_of(item.get("content"))
            if text:
                messages.append({"role": "assistant", "content": text})
        elif role == "user":
            text = _text_of(item.get("content"))
            if text:
                messages.append({"role": "user", "content": text})
        elif role == "assistant":
            text = _text_of(item.get("content"))
            if text:
                messages.append({"role": "assistant", "content": text})
    return messages


@app.post("/v1/responses", dependencies=[Depends(require_key)])
async def responses(req: ResponsesRequest):
    messages = _responses_to_messages(req.instructions, req.input)
    text, tool_call, model = await _generate(messages, req.tools, req.model)
    rid = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())

    if tool_call:
        output = [
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:16]}",
                "call_id": f"call_{uuid.uuid4().hex[:16]}",
                "name": tool_call["name"],
                "arguments": json.dumps(tool_call["arguments"], ensure_ascii=False),
                "status": "completed",
            }
        ]
    else:
        output = [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text or "", "annotations": []}],
            }
        ]

    body = {
        "id": rid,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model or "gemini",
        "output": output,
        "parallel_tool_calls": True,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }

    if req.stream:
        async def stream():
            yield f"data: {json.dumps({'type': 'response.completed', 'response': body})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return body


def main() -> None:
    global AUTH_FILE, BIND_HOST, BIND_PORT
    parser = argparse.ArgumentParser(description="OpenAI-compatible server for Gemini web app")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--cookies", default="")
    args = parser.parse_args()

    host = args.host or os.getenv("HOST") or CONFIG.get("host") or "127.0.0.1"
    port = args.port or int(os.getenv("PORT") or CONFIG.get("port") or 8000)
    auth = Path(args.cookies or os.getenv("GEMINI_AUTH_FILE") or str(AUTH_FILE))
    BIND_HOST, BIND_PORT = str(host), int(port)

    if auth != AUTH_FILE:
        AUTH_FILE = auth
    if not EXT_BOOTSTRAP:
        logging.warning("Extension bootstrap disabled (config extension.bootstrap=false).")

    logging.info("Endpoints: http://%s:%s/v1/chat/completions", host, port)
    logging.info("Models:    http://%s:%s/v1/models", host, port)
    logging.info("Chat UI:   http://%s:%s/", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()