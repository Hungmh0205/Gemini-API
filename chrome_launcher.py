#!/usr/bin/env python3
"""Launcher/giám hó Chrome ẩn cho extension "Gemini Cookie Sync".

Kiến trúc: agent có User Data RIÊNG, không clone, không dùng chung với Chrome cá
nhân. Xem `agent_user_data_dir()` và `resolve_agent_profile()`.

Vòng đời tự động (tự chạy khi server bật):

  1. Chọn profile agent: `user_data_dir` + `profile_directory` (mặc định
     %LOCALAPPDATA%\\Lumi-Agent\\Chrome + Lumi-Agent).
  2. Launch Chrome ẩn trên profile đó, mở thẳng gemini.google.com.
  3. Extension tự hỏi server lấy API key (GET /v1/auth/bootstrap) rồi tự
     POST /v1/auth/sync - không cần bấm popup, không cần thao tác tay.
  4. Giám hó: Chrome chết là mở lại (keep_alive).

SAU CÙNG PHẢI LÀM BẰNG TAY ĐÚNG MỘT LẦN
-----------------------------------------
Google Chrome >= 137 hard-block `--load-extension`. Đã kiểm tra trên 152.0.7977.65:
binary chứa literal `--load-extension is not allowed in Google Chrome, ignoring.`
và không có feature flag nào bypass được.

Ngoài ra `Secure Preferences` được MAC hoá, nên Chrome **prune sạch entry của mọi
extension unpacked** khi khởi động trên một user-data-dir bị copy. Đo thực tế:
8 entry -> 4 entry, mất cả extension. Đây là lý do chế độ "clone profile" đã bị
bỏ hoàn toàn - bản clone luôn có extension chết.

Vì vậy cần load unpacked 1 lần, trong đúng User Data của agent:
    python chrome_launcher.py --setup
rồi đăng nhập Google 1 lần trong profile đó.

Lệnh chẩn đoán:  python chrome_launcher.py --doctor
"""

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("chrome_launcher")

EXT_NAME = "Gemini Cookie Sync"
EXT_DIR_NAME = "gemini-cookie-sync-extension"
GEMINI_URL = "https://gemini.google.com/app"
BASE_DIR = Path(__file__).resolve().parent
EXT_DIR = BASE_DIR / EXT_DIR_NAME
DEFAULT_WORK_DIR = "chrome-profile"
DEFAULT_AGENT_PROFILE = "Lumi-Agent"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"

# Giám hó Chrome: giới hạn số lần mở lại khi Chrome liên tục chết, và nghỉ dần
# tăng giữa các lần để không khoét CPU. Quá ngưỡng thì supervisor thoát (server
# vẫn chạy), thay vì mở Chrome mãi. Cùng triết lý tray.ServerProcess.should_restart.
MAX_RELAUNCHES = 5
RELAUNCH_WINDOW = 300.0
RELAUNCH_BACKOFF = (15, 30, 60, 120, 300)

_hwnd_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SW_HIDE = 0


class ChromeProfile(NamedTuple):
    """Một profile Chrome: thư mục User Data chứa nó + tên thư mục profile."""

    user_data_dir: str
    name: str
    label: str | None = None

    @property
    def profile_dir(self) -> str:
        return os.path.join(self.user_data_dir, self.name)

    def describe(self) -> str:
        return f"{self.name} ({self.profile_dir})"


# =============================================================================
# Tìm Chrome + User Data
# =============================================================================
def find_chrome_exe() -> str:
    """Tìm đường dẫn chrome.exe (registry App Paths, rồi các vị trí mặc định)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            if val and os.path.isfile(val):
                return val
    except OSError:
        pass
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return str(c)
    return os.environ.get("CHROME_EXE") or "chrome"


def agent_user_data_dir() -> Path:
    """User Data riêng cho agent, nằm NGOÀI thư mục dự án.

    Tách hẳn khỏi User Data của Chrome thường là để Lumi-Agent không dính vào
    profile cá nhân: Chrome chỉ cho 1 instance trên mỗi User Data, nên dùng chung
    sẽ khiến mở Chrome tay là gián đoạn việc sync của agent. Ngoài thư mục dự án
    để cập nhật code không mất phiên đăng nhập.
    """
    raw = str(os.getenv("GEMINI_AGENT_USER_DATA_DIR") or "").strip()
    if raw:
        return Path(os.path.expandvars(raw))
    return Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / "Lumi-Agent" / "Chrome"


def user_data_root() -> Path:
    return Path(
        os.environ.get("CHROME_USER_DATA_DIR")
        or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    )


def list_profiles(udd: str | None = None) -> list[ChromeProfile]:
    """Các profile thật (Default + Profile N), Default trước."""
    root = Path(udd or user_data_root())
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile"))]
    found.sort(key=lambda p: (p.name != "Default", p.name))
    return [ChromeProfile(str(root), p.name, p.name) for p in found]


# =============================================================================
# Extension
# =============================================================================
def extension_id_from_path(ext_dir: str) -> str:
    """Extension id của unpacked extension, đúng thuật toán Chromium.

    SHA-256(đường dẫn UTF-16LE) -> 16 byte đầu -> mỗi nibble thành 'a' + nibble.
    Đây là ID Chrome ghi vào Secure Preferences, nên phải khớp tuyệt đối, nếu
    không extension_installed_in() sẽ luôn False.
    """
    raw = os.path.abspath(os.path.expandvars(ext_dir))
    digest = hashlib.sha256(raw.encode("utf-16le")).hexdigest()
    return "".join(chr(ord("a") + int(c, 16)) for c in digest[:32])


def _ext_entry(profile_dir: str, ext_id: str) -> dict | None:
    """Đọc entry của extension trong Preferences / Secure Preferences."""
    for name in ("Secure Preferences", "Preferences"):
        path = os.path.join(profile_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        entry = data.get("extensions", {}).get("settings", {}).get(ext_id)
        if isinstance(entry, dict):
            return entry
    return None


def extension_registration(profile: str, ext_dir: str | None = None) -> dict:
    """Trạng thái đăng ký extension trong profile, đủ để biết Chrome có thực sự
    load nó hay không.

    Quan trọng: Chrome prune entry của extension unpacked khi khởi động trên một
    user-data-dir đã copy (Secure Preferences có MAC gắn với nơi tạo). Entry còn
    đúng ID nhưng mất `path` là trạng thái chết - service worker không chạy, và
    extension im lặng, nên phải coi là CHƯA cài.
    """
    ext = Path(ext_dir or EXT_DIR)
    info: dict = {
        "id": extension_id_from_path(str(ext)) if ext.is_dir() else "",
        "in_store": False,
        "path": None,
        "entry": False,
        "usable": False,
        "reason": "",
    }
    if not ext.is_dir():
        info["reason"] = f"Không thấy thư mục extension: {ext}"
        return info

    store = Path(profile) / "Extensions" / info["id"]
    if store.is_dir() and any((v / "manifest.json").is_file() for v in store.iterdir()):
        info["in_store"] = True
        info["usable"] = True
        info["reason"] = "cài từ Chrome Web Store"
        return info

    entry = _ext_entry(profile, info["id"])
    if not entry:
        info["reason"] = "không có entry trong Preferences/Secure Preferences"
        return info
    info["entry"] = True
    path = entry.get("path")
    if path:
        info["path"] = path
        if Path(path).is_dir():
            info["usable"] = True
            info["reason"] = f"unpacked: {path}"
            return info
        info["reason"] = f"entry trỏ tới thư mục không tồn tại: {path}"
        return info
    # Còn ID nhưng mất path => Chrome đã prune (bản clone).
    info["reason"] = (
        "Chrome đã prune entry (chỉ còn ID, mất `path`) - extension KHÔNG chạy. "
        "Xảy ra khi copy profile sang user-data-dir khác; phải load unpacked lại trong chính profile đó."
    )
    return info


def extension_installed_in(profile: str, ext_dir: str | None = None) -> bool:
    """Extension có thực sự chạy được trong profile này không.

    `profile` là thư mục profile (User Data\\Default, User Data\\Profile 1, ...).
    """
    return extension_registration(profile, ext_dir)["usable"]


def discover_profiles(ext_dir: str | None = None) -> list[ChromeProfile]:
    """Profile nào đã cài extension "Gemini Cookie Sync"."""
    return [p for p in list_profiles() if extension_installed_in(p.profile_dir, ext_dir)]


def find_profiles_with_extension(ext_name: str = EXT_NAME) -> list[tuple[str, str]]:
    """Tương thích: [(label, thư mục profile)] trong User Data hiện tại."""
    return [(p.label or p.name, p.profile_dir) for p in discover_profiles()]


SETUP_HINT = (
    "Chưa tìm thấy profile nào đã cài extension.\n"
    "  1) chạy:  python chrome_launcher.py --setup\n"
    "  2) bật 'Developer mode' ở chrome://extensions\n"
    "  3) click 'Load unpacked' -> chọn thư mục: " + str(EXT_DIR) + "\n"
    "  4) đóng Chrome, vào gemini.google.com đăng nhập (nếu chưa), rồi bật lại server.\n"
    "  Từ đây mỗi lần bật server chỉ cần chạy, không thao tác tay nữa."
)

INSTALL_HINT = (
    "Chrome >= 137 chặn --load-extension (không có cờ nào bypass được), và Chrome\n"
    "  prune extension unpacked khi chạy trên user-data-dir đã copy. Nên profile mà\n"
    "  server dùng PHẢI được load unpacked 1 lần bằng tay, trong chính profile đó:\n"
    "    python chrome_launcher.py --setup\n"
    "  Thư mục cần load: " + str(EXT_DIR)
)


# =============================================================================
# Quản lý tiến trình Chrome
# =============================================================================
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expandvars(path))))


def _parse_user_data_dirs(cmdline: str) -> set[str]:
    dirs: set[str] = set()
    key = "--user-data-dir="
    i = 0
    while True:
        j = cmdline.find(key, i)
        if j < 0:
            break
        rest = cmdline[j + len(key):]
        if rest.startswith('"'):                      # --user-data-dir="D:\New folder\Profile 1"
            end = rest.find('"', 1)
            if end < 0:
                break
            val = rest[1:end]
            i = j + len(key) + end + 1
        elif rest.startswith('\\"'):                  # dạng chống dấu nháu --user-data-dir=\"...\"
            end = rest.find('\\"', 2)
            if end < 0:
                break
            val = rest[2:end]
            i = j + len(key) + end + 2
        else:
            # Chrome 152 truyền KHÔNG có dấu nháy, và đường dẫn mặc định có
            # khoảng trắng ("...\Chrome\User Data"). Phải cắt tại cờ kế tiếp
            # (--flag, /prefetch:4) hoặc URL, nếu không sẽ lấy cả đuôi.
            m = re.match(r'\s*"?([^"]*)', rest)
            val = m.group(1) if m else ""
            val = re.split(r"\s+(?:--|/|https?://)", val, maxsplit=1)[0].strip()
            i = j + len(key) + len(rest) - len(val)
        if val:
            dirs.add(_norm(val))
    return dirs


def _chrome_processes() -> list[tuple[int, str]]:
    """Trả về [(pid, commandline)] của các tiến trình chrome.exe."""
    ps = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    result: list[tuple[int, str]] = []
    for line in ps.stdout.splitlines():
        if "\t" not in line:
            continue
        pid_str, _, cmdline = line.partition("\t")
        if pid_str.strip().isdigit():
            result.append((int(pid_str), cmdline))
    return result


def _parse_profile_directory(cmdline: str) -> str | None:
    """Đọc --profile-directory, chấp nhận cả dạng có ngoặc nháy lẫn dạng trần.

    Tên profile có thể chứa khoảng trắng ("Profile 28"), nên phải cắt tại cờ kế
    tiếp chứ không cắt tại khoảng trắng.
    """
    key = "--profile-directory="
    j = cmdline.find(key)
    if j < 0:
        return None
    rest = cmdline[j + len(key):]
    if rest.startswith('"'):                      # --profile-directory="Lumi Agent"
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else None
    m = re.match(r'\s*"?([^"]*)', rest)
    val = m.group(1) if m else ""
    return re.split(r"\s+(?:--|/|https?://)", val, maxsplit=1)[0].strip() or None


def profile_running_pids(user_data_dir: str, profile_name: str | None = None) -> list[int]:
    """pid của các chrome.exe đang chạy với --user-data-dir này.

    `profile_name` lọc thêm theo --profile-directory. Cần khi nhiều profile dùng
    chung một User Data: Chrome mở tay (profile Default) không có cờ
    --profile-directory, còn Chrome do server mở thì có, nên không lọc sẽ tưởng
    profile agent đã chạy và không bao giờ launch.
    """
    target = _norm(user_data_dir)
    pids = []
    for pid, cmdline in _chrome_processes():
        if target not in _parse_user_data_dirs(cmdline):
            continue
        if profile_name and _norm(_parse_profile_directory(cmdline) or "") != _norm(profile_name):
            continue
        pids.append(pid)
    return sorted(pids)


def any_chrome_running() -> bool:
    return bool(_chrome_processes())


def browser_pids(user_data_dir: str) -> list[int]:
    """pid của browser process Chrome (không phải tiến trình con) đang sở hữu
    user-data-dir này.

    Chrome chỉ cho phép MỘT instance trên mỗi user-data-dir. Nếu instance đã
    chạy, lệnh launch mới sẽ bàn giao URL cho instance cũ rồi tự thoát, nên
    không có tiến trình nào mang cờ --user-data-dir/--profile-directory của ta.
    Vì vậy phải dò browser process thật, và phải tính cả case Chrome mở tay
    (không hề có --user-data-dir -> dùng User Data mặc định). Nếu không, keep_alive
    sẽ tưởng Chrome đã chết và launch lại mỗi 15s cho tới khi bàn giao vô hại.
    """
    target = _norm(user_data_dir)
    default_root = _norm(str(user_data_root()))
    pids = []
    for pid, cmdline in _chrome_processes():
        if "--type=" in cmdline:
            continue
        dirs = _parse_user_data_dirs(cmdline)
        if target in dirs or (not dirs and target == default_root):
            pids.append(pid)
    return sorted(pids)


def chrome_instance_alive(user_data_dir: str) -> bool:
    return bool(browser_pids(user_data_dir))


def _is_locked(path: str) -> bool:
    """Windows: file đang bị giữ không cho đổi tên nghĩa là có tiến trình mở khoá."""
    probe = f"{path}.gemini-probe"
    try:
        os.rename(path, probe)
    except OSError:
        return True
    try:
        os.rename(probe, path)
    except OSError:
        pass
    finally:
        if os.path.exists(probe):
            try:
                os.remove(probe)
            except OSError:
                pass
    return False


def chrome_data_in_use(profile_dir: str) -> bool:
    """Profile này đang được Chrome mở không?

    Chrome giữ khoá độc quyền `Network\\Cookies` (không read/write, không
    delete-share) nên probe bằng cách đổi tên file là đủ để biết. Cần cảm
    biến này vì Chrome được mở tay thì command line không có --user-data-dir,
    không dò được bằng pid.
    """
    db = os.path.join(profile_dir, "Network", "Cookies")
    return os.path.isfile(db) and _is_locked(db)


def stop_chrome_on(user_data_dir: str, timeout: float = 20.0, profile_name: str | None = None) -> int:
    """Tắt mọi tiến trình Chrome đang dùng user-data-dir này."""
    pids = profile_running_pids(user_data_dir, profile_name)
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            log.warning("Không tắt được chrome pid=%d: %s", pid, e)
    if pids:
        log.info("Đã tắt %d tiến trình Chrome trên %s", len(pids), user_data_dir)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and profile_running_pids(user_data_dir, profile_name):
            time.sleep(0.5)
    return len(pids)


# =============================================================================
# Clone profile
# =============================================================================
# Cache/dữ liệu tải về: không cần copy, lại chiếm phần lớn dung lượng.
_CLONE_SKIP_DIRS = (
    "Cache", "Cache_Data", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
    "component_crx_cache", "extensions_crx_cache", "Crashpad", "blob_storage",
    "Media Cache", "Safe Browsing", "Safe Browsing Network", "OptimizationHints",
    "OptimizationGuideModelsManifest", "optimization_guide_model_store",
    "Service Worker\\CacheStorage", "CacheStorage", "Service Worker\\ScriptCache",
    "File System", "AutofillAiModelCache", "AutofillStrikeDatabase",
    "optimization_guide_hint_cache_store", "SegmentedPlatform", "Segmentation Platform",
    # Phiên tab đang mở: file bị Chrome khoá, và bản clone không cần khôi phục tab.
    "Sessions", "Sessions_Encrypted", "JumpListIconsMostVisited", "JumpListIconsRecentClosed",
    "Current Session", "Current Tabs", "Last Session", "Last Tabs",
)
# File tạm/khoá: copy vào sẽ khiến Chrome trong bản clone tưởng đang chạy.
_CLONE_SKIP_FILES = ("Singleton*", "lockfile", "LOCK", "LOG", "LOG.old", "*.tmp", "*.pma")
# Nếu clone thiếu các file này thì coi như hỏng: cookie/prefs/đăng nhập đều hỏng.
_CLONE_ESSENTIALS = ("Preferences", "Secure Preferences", os.path.join("Network", "Cookies"))


def _robocopy_args(src: str, dst: str, mirror: bool, files: list[str] | None = None) -> list[str]:
    args = ["robocopy", src, dst, *(files or []), "/E", "/R:2", "/W:1", "/MT:8",
            "/NFL", "/NDL", "/NP", "/NJH", "/NJS", "/NS", "/NC"]
    if mirror:
        args.append("/MIR")
    for d in _CLONE_SKIP_DIRS:
        args += ["/XD", d]
    for f in _CLONE_SKIP_FILES:
        args += ["/XF", f]
    return args


def _copytree_fallback(src: str, dst: str) -> None:
    """Không có robocopy (ngoài Windows) thì dùng shutil."""
    skip = {d.lower() for d in _CLONE_SKIP_DIRS} | {
        "singletonlock", "singlesocket", "lockfile", "log", "log.old",
    }

    def _ignore(_dir, names):
        return [n for n in names if n.lower() in skip]

    shutil.copytree(src, dst, ignore=_ignore, symlinks=False, dirs_exist_ok=True)


def _copy_local_state(src_udd: str, dst_root: str) -> bool:
    """Copy "Local State" - bắt buộc, chứa khoá giải mã cookie (DPAPI/App-Bound).

    Dùng shutil thay vì robocopy: đây là một file duy nhất, mà robocopy vẫn dựng
    nguyên cây thư mục nguồn (rất chậm và tạo ra thư mục rỗng).
    """
    src = os.path.join(src_udd, "Local State")
    if not os.path.isfile(src):
        log.warning(
            "Không tìm thấy 'Local State' trong %s. Bản clone sẽ KHÔNG đọc được "
            "cookie đã đăng nhập và phải đăng nhập Google lại.", src_udd,
        )
        return False
    os.makedirs(dst_root, exist_ok=True)
    for attempt in (1, 2):
        try:
            shutil.copy2(src, os.path.join(dst_root, "Local State"))
            return True
        except OSError as e:
            if attempt == 2:
                log.error("Không copy được 'Local State': %s", e)
    return False


def _dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def _clone_state(dst_profile: str) -> dict[str, bool]:
    """Trạng thái các file trọng yếu của bản clone (thiếu -> phiên hỏng)."""
    return {f: os.path.isfile(os.path.join(dst_profile, f)) for f in _CLONE_ESSENTIALS}


def clone_profile(src: ChromeProfile, dst_root: str, retries: int = 2) -> bool:
    """Clone profile `src` (kèm "Local State") sang user-data-dir `dst_root`.

    Giữ nguyên tên thư mục profile để profile.info_cache trong "Local State" và
    mọi đường dẫn tương đối bên trong vẫn khớp.
    """
    dst_root = os.path.abspath(os.path.expandvars(dst_root))
    if _norm(src.user_data_dir) == _norm(dst_root):
        log.info("Profile mẫu và thư mục làm việc trùng nhau, bỏ qua clone: %s", src.user_data_dir)
        return True
    if not os.path.isdir(src.profile_dir):
        log.error("Profile mẫu không tồn tại: %s", src.profile_dir)
        return False

    dst_profile = os.path.join(dst_root, src.name)
    started = time.monotonic()
    # Bản clone có sẵn từ lần trước vẫn dùng được: nếu chỉ một file bị Chrome
    # khoá (Network\Cookies là thường gặp) thì giữ bản cũ thay vì bỏ cả clone.
    before = _clone_state(dst_profile)
    mirror = before["Preferences"]
    ok = False

    for attempt in range(1, max(1, retries) + 1):
        os.makedirs(dst_profile, exist_ok=True)
        code = 0
        if os.name == "nt" and shutil.which("robocopy"):
            proc = subprocess.run(
                _robocopy_args(src.profile_dir, dst_profile, mirror),
                capture_output=True, text=True, timeout=900,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            code = proc.returncode
        else:
            try:
                _copytree_fallback(src.profile_dir, dst_profile)
            except Exception as e:
                log.warning("shutil.copytree lỗi lần %d/%d: %s", attempt, retries, e)

        after = _clone_state(dst_profile)
        regressed = [f for f in _CLONE_ESSENTIALS if before[f] and not after[f]]
        if regressed:
            log.error(
                "Clone mất %s so với bản cũ (robocopy code %d). Giữ nguyên bản cũ để tránh hỏng phiên.",
                ", ".join(regressed), code,
            )
            return False
        missing = [f for f in _CLONE_ESSENTIALS if not after[f]]
        if not missing:
            if code >= 8:
                log.warning(
                    "robocopy báo lỗi (code %d) nhưng %s đã vào đủ; chấp nhận bản clone.",
                    code, ", ".join(_CLONE_ESSENTIALS),
                )
            ok = True
            break
        log.warning("Clone thiếu %s (robocopy code %d) - lần %d/%d.",
                    ", ".join(missing), code, attempt, retries)
        time.sleep(1.0)

    if not ok:
        return False
    _copy_local_state(src.user_data_dir, dst_root)
    log.info(
        "Đã clone profile %s -> %s [%s] (%.1f MB, %.1fs)",
        src.describe(), dst_profile, "cập nhật" if mirror else "mới",
        _dir_size_mb(dst_profile), time.monotonic() - started,
    )
    return True


# =============================================================================
# Launch / ẩn cửa sổ
# =============================================================================
def build_chrome_command(
    exe: str,
    profile: str,
    hidden: str = "offscreen",
    url: str = GEMINI_URL,
    ext_dir: str | None = None,
    profile_directory: str | None = None,
) -> list[str]:
    cmd = [
        exe,
        f"--user-data-dir={os.path.abspath(os.path.expandvars(profile))}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]
    if profile_directory:
        cmd.append(f"--profile-directory={profile_directory}")
    # Chrome >= 137 bỏ qua cờ này, nhưng Chromium/Edge vẫn dùng được: để dự phòng.
    if ext_dir and os.path.isdir(ext_dir):
        cmd.append(f"--load-extension={os.path.abspath(ext_dir)}")
    if hidden == "offscreen":
        cmd += ["--window-position=-32000,-32000", "--window-size=1200,800"]
    elif hidden == "headless":
        cmd += ["--headless=new"]
    cmd.append(url)
    return cmd


def hide_chrome_windows(pids) -> int:
    """Ẩn cửa sổ của (các) tiến trình Chrome khỏi màn hình và taskbar."""
    if isinstance(pids, int):
        pids = {pids}
    targets = {int(p) for p in pids}
    if not targets:
        return 0
    user32 = ctypes.windll.user32
    hidden = [0]

    @_hwnd_type
    def _cb(hwnd, _):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value in targets and user32.IsWindowVisible(hwnd):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW)
            user32.ShowWindow(hwnd, SW_HIDE)
            hidden[0] += 1
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception as e:
        log.warning("Không thể ẩn cửa sổ Chrome (pids=%s): %s", sorted(targets), e)
    if hidden[0]:
        log.info("Đã ẩn %d cửa sổ Chrome (pids=%s) khỏi màn hình + taskbar.", hidden[0], sorted(targets))
    return hidden[0]


def _pid_alive(pid: int) -> bool:
    """Tiến trình còn sống không. Syscall thuần, không spawn tiến trình con.

    `browser_pids()` phải chạy PowerShell `Get-CimInstance Win32_Process` (~0.5s),
    nên dùng nó để poll trong vòng đợi là tốn ~25 lần spawn mỗi lần đóng Chrome.
    """
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def close_agent_chrome(user_data_dir: str, timeout: float = 25.0) -> bool:
    """Đóng Chrome agent mà không làm hỏng profile.

    KHÔNG dùng TerminateProcess / taskkill /F: Chrome tắt cưỡng bức sẽ không ghi
    `Secure Preferences`, mà đó chính là nơi Chrome lưu extension unpacked. Đã
    gặp thật: kill cứng xong extension biến mất và phải load lại.

    Thứ tự:
      1. `taskkill /PID` không `/F` — gửi WM_CLOSE tới các cửa sổ top-level, đúng
         kiểu Chrome tự thoát và tự ghi trạng thái.
      2. Chờ tối đa `timeout` giây.
      3. Chỉ còn đích cùng, mới `taskkill /F`, và nói rõ là cưỡng bức.
    """
    pids = browser_pids(user_data_dir)
    if not pids:
        return True

    log.info("Đang đóng Chrome agent (pids=%s) để giải phóng profile...", sorted(pids))
    for pid in pids:
        # /F vắng mặt = yêu cầu đóng lịch sự, KHÔNG giết.
        subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True, text=True, timeout=30, check=False,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            # Các pid đã báo tắt. Chrome có thể đổi pid lúc thoát, nên quét lại
            # `browser_pids()` một lần cho chắc — nhưng chỉ một lần, thay vì mỗi
            # vòng lặp.
            left = browser_pids(user_data_dir)
            if not left:
                log.info("Chrome agent đã đóng sạch.")
                return True
            pids = left
        time.sleep(1.5)

    left = browser_pids(user_data_dir)
    log.warning(
        "Chrome agent không chịu thoát sau %.0fs (pids=%s) — buộc phải giết cưỡng bức. "
        "Profile có thể mất bản ghi extension trong 'Secure Preferences'.",
        timeout, sorted(left),
    )
    for pid in left:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    time.sleep(2)
    return not browser_pids(user_data_dir)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated() -> int | None:
    """Chạy lại chính lệnh này với quyền admin qua UAC. Trả về mã thoát nếu đã chạy."""
    if os.name != "nt" or is_admin():
        return None
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]]
    params = subprocess.list2cmdline(args)
    try:
        # "runas" = hien UAC; user bam 'Yes' thi lenh chay lai voi quyen admin.
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    except Exception as e:
        log.error("Không mở được UAC: %s", e)
        return 1
    if rc <= 32:
        log.error("Yêu cầu quyền admin bị từ chối hoặc lỗi (ShellExecute=%s).", rc)
        return 1
    return None


def launch_profile(
    profile: str,
    chrome_path: str = "",
    hidden: str = "offscreen",
    url: str = GEMINI_URL,
    ext_dir: str | None = None,
    profile_directory: str | None = None,
) -> int | None:
    """Launch Chrome với user-data-dir này ở chế độ ẩn; trả pid hoặc None."""
    profile = os.path.abspath(os.path.expandvars(profile))
    if not os.path.isdir(profile):
        log.error("Chrome user-data-dir không tồn tại: %s", profile)
        return None
    existing = browser_pids(profile)
    if existing:
        log.info(
            "Chrome đã chạy sẵn trên %s (pid=%s). Chrome chỉ cho 1 instance/user-data-dir "
            "nên lệnh mới chỉ mở thêm tab vào instance đó - không mở instance ẩn riêng. "
            "Extension vẫn tự mở tab Gemini ẩn khi cần sync.",
            profile, existing,
        )
        return None
    exe = chrome_path or find_chrome_exe()
    cmd = build_chrome_command(exe, profile, hidden, url, ext_dir, profile_directory)
    log.info("Launching Chrome (%s) %s -> %s", hidden or "visible", profile, exe)
    try:
        proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    except Exception as e:
        log.error("Không khởi động được Chrome: %s", e)
        return None
    if hidden == "offscreen":
        time.sleep(4)
        # Chrome có thể đổi pid của browser process, nên quét theo --user-data-dir.
        hide_chrome_windows(set(profile_running_pids(profile)) | {proc.pid})
    return proc.pid


# =============================================================================
# Chờ extension đồng bộ xong
# =============================================================================
def check_health(health_url: str, timeout: float = 4.0) -> dict | None:
    """GET /health; None nếu không gọi được."""
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


async def wait_for_auth(health_url: str, timeout: float = 180.0) -> bool:
    """Chờ /health báo ok, tức extension đã đẩy cookie và server đã xác thực."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        data = await asyncio.to_thread(check_health, health_url)
        if data and data.get("status") == "ok":
            # /health có thể đã "ok" từ trước nhờ gemini-auth.json cũ, nên không
            # được kết luận là extension vừa sync. Chỉ kết luận khi cookie vừa
            # được làm mới (auth_age_sec nhỏ); nếu không thì nói rõ là phiên cũ.
            try:
                age = float(data.get("auth_age_sec"))
            except (TypeError, ValueError):
                age = None
            if age is None or age <= 300:
                log.info(
                    "Cookie vừa được extension đồng bộ, server đã xác thực "
                    "(%s, auth_age=%.0fs).", health_url, age if age is not None else -1,
                )
            else:
                log.info(
                    "Server đã xác thực nhờ phiên CŨ (auth_age=%.0fs), extension chưa "
                    "sync lần nào. Cookie trong %s là di sản lần chạy trước, không "
                    "phải của profile agent.", age, health_url,
                )
            return True
        if not announced:
            log.info("Đang chờ extension đồng bộ cookie từ Chrome (tối đa %.0fs)...", timeout)
            announced = True
        await asyncio.sleep(3)
    log.warning(
        "Chưa nhận được cookie sau %.0fs. Kiểm tra: (1) profile mẫu đã đăng nhập gemini.google.com, "
        "(2) extension 'Gemini Cookie Sync' đã bật, (3) log server không có lỗi 401.",
        timeout,
    )
    return False


# =============================================================================
# Chọn profile + chuẩn bị bản clone
# =============================================================================
def resolve_work_dir(cfg: dict) -> Path:
    raw = str(cfg.get("work_dir") or DEFAULT_WORK_DIR).strip()
    p = Path(os.path.expandvars(raw))
    return p if p.is_absolute() else (BASE_DIR / p)


def resolve_template_profile(cfg: dict) -> ChromeProfile | None:
    """Profile mẫu sẽ được clone mỗi lần bật server.

    Thứ tự: browser.source_profile → GEMINI_TEMPLATE_PROFILE → profile trong User
    Data đã cài extension → chính bản clone (nếu lần trước đã chuẩn bị xong).
    """
    explicit = str(cfg.get("source_profile") or os.getenv("GEMINI_TEMPLATE_PROFILE") or "").strip()
    if explicit:
        path = os.path.abspath(os.path.expandvars(explicit))
        if not os.path.isdir(path):
            log.error("browser.source_profile không tồn tại: %s", path)
            return None
        # Chấp nhận cả thư mục profile lẫn thư mục User Data.
        if os.path.isdir(os.path.join(path, "Default")) or os.path.isdir(
            os.path.join(path, "Profile 1")
        ):
            udd = path
        else:
            udd, name = os.path.split(path)
            if os.path.isfile(os.path.join(udd, "Local State")):
                return ChromeProfile(udd, name, name)
            log.error("browser.source_profile không thuộc User Data nào (thiếu 'Local State'): %s", path)
            return None
        matches = [p for p in list_profiles(udd) if extension_installed_in(p.profile_dir)]
        if not matches:
            log.error("Profile trong %s chưa cài extension. %s", udd, SETUP_HINT)
            return None
        return matches[0]

    for profile in discover_profiles():
        log.info("Dùng profile mẫu đã cài extension: %s", profile.describe())
        return profile

    work = resolve_work_dir(cfg)
    if work.is_dir():
        for profile in list_profiles(str(work)):
            if extension_installed_in(profile.profile_dir):
                log.info("Không thấy profile mẫu; tái dùng bản clone: %s", profile.describe())
                return profile
    return None


def _clone_is_usable(work: Path, name: str) -> bool:
    """Bản clone cũ còn dùng được không (đủ file trọng yếu + extension chạy được)?"""
    profile_dir = work / name
    if not profile_dir.is_dir():
        return False
    if any(not v for v in _clone_state(str(profile_dir)).values()):
        return False
    return os.path.isfile(work / "Local State") and extension_installed_in(str(profile_dir))


def resolve_agent_profile(cfg: dict) -> ChromeProfile:
    """Profile dành riêng cho agent, trong User Data riêng của agent (KHÔNG clone,
    KHÔNG dùng chung User Data với Chrome cá nhân).

    Đây là chế độ mặc định. Hai lý do khiến kiến trúc cũ (clone profile) không
    dùng được:

      1. `Secure Preferences` được MAC hoá. Chrome prune sạch entry của mọi
         extension unpacked khi khởi động trên user-data-dir đã copy - đo thực tế
         8 entry -> 4, mất cả extension. Nên extension unpacked chỉ sống được ở
         đúng profile gốc, không thừa hưởng việc copy.
      2. Dùng chung User Data với Chrome thường nghĩa là dùng chung instance: mở
         Chrome tay sẽ giành quyền quản lý instance, và việc giấu cửa sổ của agent
         có thể đụng vào cửa sổ cá nhân.

    Nhược điểm duy nhất: phải load unpacked + đăng nhập Google 1 lần, trong đúng
    User Data này (`--setup` lo phần mở cửa sổ).
    """
    raw = str(cfg.get("user_data_dir") or "").strip()
    root = Path(os.path.expandvars(raw)) if raw else agent_user_data_dir()
    name = str(cfg.get("profile_directory") or DEFAULT_AGENT_PROFILE).strip() or DEFAULT_AGENT_PROFILE
    return ChromeProfile(str(root), name, "agent")


def prepare_work_profile(cfg: dict) -> ChromeProfile | None:
    """Chuẩn bị profile mà server sẽ chạy. Trả None nếu chưa sẵn sàng.

    Chỉ còn một chế độ: "agent". Chế độ "clone" (copy profile mẫu) đã bị bỏ vì
    Chrome prune extension unpacked trên user-data-dir bị copy - profile clone
    luôn có extension chết, nên nó vô dụng.
    """
    mode = str(cfg.get("mode") or "agent").strip().lower()
    if mode != "agent":
        log.error(
            "browser.mode=%s không còn được hỗ trợ. Đặt \"mode\": \"agent\" trong config.json.", mode,
        )
        return None
    return prepare_agent_profile(cfg)


def prepare_agent_profile(cfg: dict) -> ChromeProfile:
    agent = resolve_agent_profile(cfg)
    os.makedirs(agent.user_data_dir, exist_ok=True)
    if not os.path.isdir(agent.profile_dir):
        log.info(
            "Profile agent %s chưa tồn tại - Chrome sẽ tự tạo ở lần chạy đầu. "
            "Cần load extension 1 lần: python chrome_launcher.py --setup", agent.profile_dir,
        )
    elif not extension_installed_in(agent.profile_dir):
        log.warning("Profile agent chưa có extension. %s", INSTALL_HINT)
    return agent


def prepare_clone_profile(cfg: dict) -> ChromeProfile | None:
    """Clone profile mẫu sang thư mục làm việc. Trả về profile cần chạy Chrome."""
    work = resolve_work_dir(cfg)
    template = resolve_template_profile(cfg)
    if not template:
        return None
    if not cfg.get("copy_profile", True):
        os.makedirs(work, exist_ok=True)
        return ChromeProfile(str(work), template.name, "clone")
    if _norm(template.user_data_dir) != _norm(str(work)):
        # Không copy khi Chrome còn mở trên thư mục đích (file bị khoá, DB rách).
        stop_chrome_on(str(work))
        if chrome_data_in_use(template.profile_dir):
            log.warning(
                "Chrome đang mở profile mẫu %s (giữ khoá Network\\Cookies) nên không "
                "clone được phiên mới nhất.", template.describe(),
            )
            if _clone_is_usable(work, template.name):
                log.info("Dùng bản clone có sẵn tại %s.", work)
            else:
                log.error(
                    "Chưa có bản clone nào. Hãy đóng Chrome (hoặc đóng profile %s), "
                    "bật lại server một lần để tạo bản clone.", template.name,
                )
                return None
        elif not clone_profile(template, str(work), retries=int(cfg.get("copy_retries", 2))):
            if _clone_is_usable(work, template.name):
                log.warning(
                    "Clone mới không hoàn chỉnh nhưng bản clone cũ vẫn dùng được - dùng bản cũ. "
                    "Đóng Chrome đang mở profile mẫu rồi bật lại server để lấy phiên mới nhất.",
                )
            else:
                log.error(
                    "Không clone được profile %s -> %s. Nếu Chrome đang mở profile mẫu thì "
                    "hãy đóng nó (Chrome giữ khoá Network\\Cookies) rồi bật lại server.",
                    template.describe(), work,
                )
                return None
    if not extension_installed_in(os.path.join(str(work), template.name)):
        log.warning(
            "Bản clone %s chưa có extension chạy được. %s",
            os.path.join(str(work), template.name), INSTALL_HINT,
        )
    return ChromeProfile(str(work), template.name, "clone")


def resolve_profile(cfg_profile: str) -> tuple[str, str | None]:
    """Tương thích ngược: (đường dẫn, label) của profile đã cài extension."""
    if cfg_profile and cfg_profile.strip():
        path = os.path.abspath(os.path.expandvars(cfg_profile.strip()))
        if not os.path.isdir(path):
            log.error("browser.profile trong config không tồn tại: %s", path)
            return path, None
        return path, os.path.basename(path)
    for label, path in find_profiles_with_extension():
        return path, label
    dedicated = BASE_DIR / DEFAULT_WORK_DIR
    dedicated.mkdir(parents=True, exist_ok=True)
    return str(dedicated), DEFAULT_WORK_DIR


# =============================================================================
# Giám hó (chạy trong event loop của server)
# =============================================================================
# `keep_alive` trong config chỉ được đọc một lần lúc khởi động, nên bảng điều
# khiển không tắt được Chrome đang chạy nếu không có đường tắt riêng. Panel gọi
# set_keep_alive() để chặn/tiếp tục giám hó ngay, và giá trị trong config giữ
# nguyên làm mặc định cho lần khởi động sau (nên bấm "dừng" rồi khởi động lại
# server thì Chrome vẫn được bật lại đúng như cấu hình).
_keep_alive_override: bool | None = None


def set_keep_alive(enabled: bool | None) -> None:
    global _keep_alive_override
    _keep_alive_override = enabled
    log.info("keep_alive override -> %s", "config" if enabled is None else enabled)


def _keep_alive(configured: bool) -> bool:
    return configured if _keep_alive_override is None else _keep_alive_override


async def browser_supervisor(browser_cfg: dict) -> None:
    """Clone -> launch -> giám hó Chrome ẩn để extension luôn đẩy cookie tươi."""
    if not browser_cfg.get("enabled", False):
        return

    chrome_path = str(browser_cfg.get("chrome_path") or "").strip()
    mode = browser_cfg.get("hidden", "offscreen") or "offscreen"
    if mode not in ("offscreen", "headless"):
        mode = "offscreen"
    keep_alive = bool(browser_cfg.get("keep_alive", True))
    url = str(browser_cfg.get("gemini_url") or GEMINI_URL)
    health_url = str(browser_cfg.get("health_url") or DEFAULT_HEALTH_URL).strip()
    ext_dir = str(browser_cfg.get("ext_dir") or EXT_DIR)
    sync_timeout = float(browser_cfg.get("sync_timeout", 180))

    warned_no_profile = False
    warned_no_ext = False
    waited_sync = False
    target: ChromeProfile | None = None
    # Chrome có thể chết ngay sau khi mở (profile bị khoá, Chrome tự cập nhật).
    # Không giới hạn thì vòng lặp này mở lại mỗi 15s vô tận, mỗi lần đều spawn
    # tiến trình Chrome. Cùng triết lý với `tray.ServerProcess.should_restart`.
    launch_attempts: list[float] = []
    alive_since: float = 0.0

    while True:
        if target is None:
            target = await asyncio.to_thread(prepare_work_profile, browser_cfg)
            if target is None:
                if not warned_no_profile:
                    log.warning("Chưa tìm được profile mẫu để clone. %s", SETUP_HINT)
                    log.warning("Tự thử lại mỗi 30s; đóng Chrome đang mở profile mẫu cũng giúp.")
                    warned_no_profile = True
                if not _keep_alive(keep_alive):
                    return
                await asyncio.sleep(30)
                continue
            warned_no_profile = False
            info = extension_registration(target.profile_dir, ext_dir)
            if not info["usable"]:
                log.warning(
                    "Profile %s chưa có extension chạy được (%s). %s",
                    target.profile_dir, info["reason"], INSTALL_HINT,
                )
                warned_no_ext = True

        if warned_no_ext:
            # Profile mới vừa được Chrome tạo ra có thể chưa kịp có Secure
            # Preferences; kiểm tra lại thay vì bỏ cuộc vĩnh viễn.
            if not extension_installed_in(target.profile_dir, ext_dir):
                if not _keep_alive(keep_alive):
                    return
                await asyncio.sleep(30)
                continue
            log.info("Extension đã sẵn sàng trong %s - tiếp tục.", target.profile_dir)
            warned_no_ext = False
            warned_no_profile = False
            target = None
            continue

        # `chrome_instance_alive` -> `browser_pids` -> `_chrome_processes` spawn
        # PowerShell `Get-CimInstance Win32_Process` (timeout 15s). Gọi trực tiếp ở
        # đây thì chặn event loop mỗi vòng lặp, và vòng lặp chạy mỗi ~15s: `/health`
        # trễ >2s khiến tray tưởng server đã chết rồi spawn thêm server tranh cổng.
        if not await asyncio.to_thread(chrome_instance_alive, target.user_data_dir):
            if alive_since:
                # Chrome sống được một lúc rồi lại chết thì đếm lại từ đầu.
                alive_since = 0.0
                launch_attempts.clear()
            now = time.monotonic()
            launch_attempts = [t for t in launch_attempts if now - t < RELAUNCH_WINDOW]
            if len(launch_attempts) >= MAX_RELAUNCHES:
                log.error(
                    "Chrome agent đã chết %d lần trong %.0fs — bỏ giám hó để khỏi mở "
                    "lại vô tận. Server vẫn phục vụ /v1 nếu phiên còn sống. "
                    "Thử `python chrome_launcher.py --doctor`, hoặc --reset nếu profile kẹt.",
                    len(launch_attempts), RELAUNCH_WINDOW,
                )
                return
            backoff = RELAUNCH_BACKOFF[min(len(launch_attempts), len(RELAUNCH_BACKOFF) - 1)]
            launch_attempts.append(now)
            if backoff > RELAUNCH_BACKOFF[0]:
                log.warning("Chrome agent lại chết; chờ %.0fs trước khi mở lại.", backoff)
                if not _keep_alive(keep_alive):
                    return
                await asyncio.sleep(backoff)
            try:
                launch_profile(
                    target.user_data_dir, chrome_path, mode, url, ext_dir, target.name
                )
            except Exception as e:  # noqa: BLE001 - supervisor phải sống sót lần lặp sau
                log.warning("Không khởi động được Chrome agent: %s", e)
            if health_url and not waited_sync:
                # Chỉ đánh dấu đã chờ khi chờ thành công: `wait_for_auth` hết giờ thì
                # vòng sau còn cơ hội thử lại, thay vì bỏ luôn.
                if await wait_for_auth(health_url, sync_timeout):
                    waited_sync = True
        elif not alive_since:
            alive_since = time.monotonic()

        if _keep_alive(keep_alive):
            await asyncio.sleep(15)
        else:
            return


# =============================================================================
# CLI
# =============================================================================
def _cfg_from_args(args: list[str]) -> dict:
    cfg: dict = {}

    def _val(flag: str) -> str | None:
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return None

    src = _val("--profile")
    if src:
        cfg["source_profile"] = src
    work = _val("--work-dir")
    if work:
        cfg["work_dir"] = work
    mode = _val("--mode")
    if mode:
        cfg["mode"] = mode
    pdir = _val("--profile-dir")
    if pdir:
        cfg["profile_directory"] = pdir
    udd = _val("--user-data-dir")
    if udd:
        cfg["user_data_dir"] = udd
    return cfg


def doctor(cfg: dict) -> int:
    """In tình trạng profile agent và việc cần làm, không đụng tiến trình nào.

    Chỉ báo cáo về User Data của agent. Profile cá nhân của bạn không liên quan
    và không được đụng tới: agent có User Data riêng, extension riêng, phiên riêng.
    """
    print("=== Chrome ===")
    print("  exe       :", find_chrome_exe())
    print("  extension :", EXT_DIR)
    print("  extension id:", extension_id_from_path(str(EXT_DIR)))

    mode = str(cfg.get("mode") or "agent").strip().lower()
    print(f"\n=== Profile agent (mode={mode}) ===")
    if mode == "clone":
        print("  CHẾ ĐỘ CŨ - đã lỗi thời: Chrome prune extension unpacked trong bản copy.")
        print("  Hãy đổi config.json sang \"mode\": \"agent\".")
        return 1

    target = resolve_agent_profile(cfg)
    print("  User Data :", target.user_data_dir)
    print("  profile   :", target.name)
    print("  tồn tại   :", os.path.isdir(target.profile_dir))
    print("  Local State:", os.path.isfile(os.path.join(target.user_data_dir, "Local State")),
          "(cần để đọc cookie đã đăng nhập)")
    running = browser_pids(target.user_data_dir)
    print("  Chrome đang chạy:", running or "không")

    info = extension_registration(target.profile_dir)
    print("  extension :", "CHẠY ĐƯỢC" if info["usable"] else "CHƯA ĐƯỢC")
    print("              ", info["reason"])

    if not info["usable"]:
        print("\n  -> chạy:  python chrome_launcher.py --setup")
        print("     rồi mở gemini.google.com trong profile đó và đăng nhập Google.")
        return 1

    cookies = os.path.join(target.profile_dir, "Network", "Cookies")
    size = os.path.getsize(cookies) if os.path.isfile(cookies) else 0
    print("  Cookies DB:", size, "bytes", "" if size else "(rỗng - chưa đăng nhập Google)")
    if not size:
        print("\n  -> mở Chrome với profile này, vào gemini.google.com, đăng nhập Google.")
        return 1
    return 0


def reset(cfg: dict) -> int:
    """Xoá sạch profile agent + phiên đăng nhập, để đổi tài khoản Google.

    Cần xoá CẢ HAI, nếu không thì vẫn dùng tài khoản cũ:
      1. User Data agent (%LOCALAPPDATA%\\Lumi-Agent\\Chrome) - chứa cookie Google
         và bản ghi extension trong 'Secure Preferences'.
      2. gemini-auth.json - phiên đã đồng bộ. Xoá riêng profile là vô nghĩa vì
         server vẫn xác thực bằng file này, và người dùng sẽ tưởng đã đổi acc.

    Sau khi reset, extension mất bản ghi nên bắt buộc chạy lại `--setup`.
    """
    target = resolve_agent_profile(cfg)
    udd = Path(target.user_data_dir)
    auth_file = BASE_DIR / str((cfg.get("auth_file") or "gemini-auth.json"))

    # Tự đóng Chrome agent thay vì bắt người dùng. Phải đóng TRƯỚC khi hỏi xác nhận,
    # vì nếu user để yên rồi bấm Enter thì Chrome lại mở giữa chừng.
    if browser_pids(target.user_data_dir) and not close_agent_chrome(target.user_data_dir):
        log.error("Không đóng được Chrome agent. Đóng tay rồi chạy lại lệnh này.")
        return 1

    print(f"User Data sẽ xoá : {udd}")
    print(f"Phiên sẽ xoá     : {auth_file}")
    for extra in sorted(BASE_DIR.glob("gemini-auth.json.bak*")):
        print(f"Backup sẽ xoá    : {extra}")
    print()
    print("Sẽ mất: cookie Google đã đăng nhập, lịch sử, và bản ghi extension.")
    print("Nhập 'xoa' để tiến hành: ", end="", flush=True)
    if input().strip().lower() not in ("xoa", "x", "yes"):
        print("Đã hủy.")
        return 1

    for path in [udd, auth_file, *sorted(BASE_DIR.glob("gemini-auth.json.bak*"))]:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
            else:
                path.unlink()
            log.info("Đã xoá %s", path)
        except PermissionError:
            # Thường không xảy ra (%LOCALAPPDATA% và thư mục code đều của user),
            # nhưng nếu profile đặt ở vị trí bị policy khoá thì cần admin.
            log.error("Không xoá được %s: cần quyền admin.", path)
            print("\nĐang xin quyền admin qua UAC để xoá...")
            rc = relaunch_elevated()
            if rc is None:
                print("Đã mở cửa sổ UAC. Xem kết quả ở cửa sổ đó, cửa sổ này đóng.")
                return 0
            return rc
        except OSError as e:
            log.error("Không xoá được %s: %s", path, e)
            return 1

    print()
    print("Đã reset. Bước tiếp theo:")
    print("  1) python chrome_launcher.py --setup   (load unpacked + đăng nhập Google tài khoản mới)")
    print("  2) run_server-debug.bat")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):  # console cp1252 không in được tiếng Việt
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    args = sys.argv[1:]
    cfg = _cfg_from_args(args)
    cfg.setdefault("health_url", DEFAULT_HEALTH_URL)

    if "--doctor" in args:
        raise SystemExit(doctor(cfg))

    if "--setup" in args:
        # PHẢI setup đúng profile server sẽ chạy, nếu không extension sẽ nằm ở
        # profile khác và Chrome sẽ bỏ qua nó.
        target = prepare_work_profile(cfg) or resolve_agent_profile(cfg)
        print(f"Profile cần cài extension: {target.describe()}")
        print("Extension ID (đối chiếu ở chrome://extensions):", extension_id_from_path(str(EXT_DIR)))
        print("Đang mở chrome://extensions - cửa sổ hiện ra để bạn thao tác...")
        cmd = build_chrome_command(
            find_chrome_exe(), target.user_data_dir, hidden="",
            url="chrome://extensions", ext_dir=str(EXT_DIR), profile_directory=target.name,
        )
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        print(
            f"1) Bật 'Developer mode'\n"
            f"2) 'Load unpacked' -> chọn: {EXT_DIR}\n"
            f"3) Mở gemini.google.com và đăng nhập Google (trong đúng profile này)\n"
            f"4) Đóng Chrome. Từ đây chỉ cần bật server."
        )
        print("Đang chờ nạp extension (tối đa 5 phút)...")
        for _ in range(60):
            time.sleep(5)
            if extension_installed_in(target.profile_dir):
                print(f"OK - extension đã sẵn sàng trong {target.profile_dir}.")
                raise SystemExit(0)
        print("Hết thời gian chờ. Chạy lại --setup khi nào sẵn sàng.")
        raise SystemExit(1)

    if "--reset" in args:
        raise SystemExit(reset(cfg))

    if "--clone-only" in args:
        cfg.setdefault("mode", "clone")
        work = prepare_work_profile(cfg)
        print(f"Đã chuẩn bị: {work.describe() if work else '(thất bại - xem log)'}")
        raise SystemExit(0 if work else 1)

    if os.environ.get("BROWSER_KEEPALIVE", "0") == "1":
        cfg.update({"enabled": True, "hidden": "offscreen", "keep_alive": True})
        asyncio.run(browser_supervisor(cfg))
    else:
        work = prepare_work_profile(cfg)
        if not work:
            print(SETUP_HINT)
            raise SystemExit(1)
        if not extension_installed_in(work.profile_dir):
            print(INSTALL_HINT)
            raise SystemExit(1)
        launch_profile(work.user_data_dir, hidden="offscreen",
                       ext_dir=str(EXT_DIR), profile_directory=work.name)
        print(f"Launched profile={work.user_data_dir} name={work.name} -> Gemini (ẩn).")
        asyncio.run(wait_for_auth(cfg["health_url"], 180))
