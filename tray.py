#!/usr/bin/env python3
"""Supervisor: chạy server ẩn + icon trên system tray.

Vì sao cần riêng một tiến trình, thay vì làm server tự quản lý bản thân:

* **Sự sống còn.** `uvicorn` không tự hồi sinh sau khi crash. Tiến trình tray giữ
  cấu hình port và nếu server chết bất thường thì bật lại, kèm giới hạn số lần
  để tránh vòng lặp khởi động lại vô tận khi cấu hình sai.
* **Đóng cửa sổ.** Đóng cửa sổ console bằng X sẽ giết luôn server. Ở đây cửa
  sổ console bị ẩn, nên không còn cửa sổ để đóng nhầm.
* **Nút bật/tắt.** Tắt server rồi muốn bật lại thì cần một thứ còn sống. Chính
  icon tray là thứ đó.

Giao tiếp với server: qua HTTP (mở trang, hỏi `/health`) chứ không qua cổng
riêng. Thêm cổng điều khiển thì phải tự bảo vệ nó, còn HTTP thì tái dùng đúng
cơ chế token của panel.

Cách chạy:

    python tray.py            # chạy nền, có icon tray
    python tray.py --no-tray  # không có pystray: chạy như supervisor trong console
    python tray.py --status   # in trạng thái rồi thoát
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SERVER_PY = BASE_DIR / "server.py"
RESTART_EXIT = 75  # EX_TEMPFAIL: cố ý restart, KHÔNG phải crash
STOP_EXIT = 76  # EX_PROTOCOL: cố ý dừng, KHÔNG phải crash (xem server.py)
MAX_FAST_RESTARTS = 5
FAST_RESTART_WINDOW = 60.0

try:
    import pystray
    from pystray import Icon, Menu, MenuItem
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False
    Icon = Menu = MenuItem = None  # type: ignore[assignment]


def harden_streams() -> None:
    """Bảo đảm in log không bao giờ giết tiến trình supervisor.

    Chạy tách rời thì mọi thứ vỡ theo hai kiểu khác nhau:

    1. `pythonw.exe` -> `sys.stdout` là `None`, `print()` ném AttributeError.
    2. Console với code page cp1252 -> dòng log tiếng Việt ném UnicodeEncodeError.
       Đúng lỗi này đã xảy ra: dòng "icon tray đã sẵn sàng" làm cả tray chết
       *trước khi* bật được server, và mất sạch dấu vết vì pythonw nuốt stderr.

    Vì vậy chuyển cả hai stream sang UTF-8 với `errors="replace"`: mất tối đa
    vài ký tự lạ, đổi lại supervisor không bao giờ chết vì muốn ghi log.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - stream kiểu cũ/đã đóng vẫn phải chạy được
            pass
    # pythonw.exe để cả hai stream là None -> print() ném AttributeError. Gán
    # stream rỗng để mọi lệnh in còn lại trong file hoạt động bình thường.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _log(msg: str) -> None:
    try:
        print(f"[tray] {msg}", flush=True)
    except Exception:  # noqa: BLE001 - ghi log hỏng không được phép giết supervisor
        pass


# ----------------------------------------------------------------------------
# Cấu hình
# ----------------------------------------------------------------------------
def read_settings() -> dict:
    """Đọc host/port/token từ config.json mà không cần pydantic.

    Supervisor phải chạy được kể cả khi môi trường hỏng (sai kiểu, thiếu file),
    vì nhiệm vụ của nó là cứu server. Import config_store ở đây chỉ để đường
    fallback; nếu import lỗi thì vẫn còn đường thủ công bên dưới.
    """
    path = BASE_DIR / os.getenv("LUMI_CONFIG", "config.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        raw = {}
    host = os.getenv("HOST") or raw.get("host") or "127.0.0.1"
    try:
        port = int(os.getenv("PORT") or raw.get("port") or 8000)
    except (TypeError, ValueError):
        port = 8000
    return {
        "host": host,
        "port": port,
        "base_url": f"http://{host}:{port}",
        "token": (raw.get("admin") or {}).get("token") or "",
        "path": path,
    }


# ----------------------------------------------------------------------------
# Server con
# ----------------------------------------------------------------------------
class ServerProcess:
    """Tiến trình server, khởi động lại được, và báo trạng thái cho tray."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.proc: subprocess.Popen | None = None
        self.restarts: list[float] = []
        self.last_error: str | None = None
        self.stopping = False
        self.quitting = False
        self.idle = False
        self.external = False
        self.wake = threading.Event()
        self.lock = threading.RLock()
        # Tray chết mà server con sống dai = một server mồ côi giữ cổng, không ai
        # quản lý, và lần `tray.py` sau phải giành lại nó. atexit gửi tín hiệu dừng
        # khi tray thoát bình thường (Ctrl+C, exception, SystemExit).
        atexit.register(self._cleanup_on_exit)

    def _cleanup_on_exit(self) -> None:
        with self.lock:
            proc = self.proc
            external = self.external
        if external or proc is None or proc.poll() is not None:
            return  # server do tiến trình khác giữ: không đụng
        _log("tray đang thoát, dừng server con")
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def start(self) -> bool:
        with self.lock:
            if self.proc and self.proc.poll() is None and not self.external:
                return True
            # Bắt đầu một server mới: bỏ mọi trạng thái "đang theo dõi/không chạy".
            self.external = False
            self.stopping = False
            self.idle = False
            self.wake.set()
            env = dict(os.environ)
            # Đánh dấu để /admin biết có thể tự restart, và để tray nhận ra tiến
            # trình do mình quản lý (tránh tranh chấp khi user cũng chạy tay).
            env["LUMI_SUPERVISED"] = "1"
            env.setdefault("LUMI_CONFIG", str(self.settings["path"]))
            env.setdefault("PYTHONIOENCODING", "utf-8")
            _log(f"khởi động server -> {self.settings['base_url']}")
            self.proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(SERVER_PY)],
                cwd=str(BASE_DIR),
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True

    def adopt(self) -> bool:
        """Theo dõi server đã chạy sẵn (do tiến trình khác giữ cổng).

        Không spawn thêm: nếu không, server mới sẽ crash vì cổng đã bị chiếm, rồi
        supervisor bật lại không ngừng. Ở trạng thái này `wait()` chỉ canh
        `/health`, và nếu server đang chạy biến mất thì vòng giám sát tự spawn lại.
        """
        h = health(self.settings)
        if not h or "status" not in h:
            return False
        with self.lock:
            self.external = True
            self.proc = None
            self.stopping = False
            self.idle = False
            self.last_error = None
            self.restarts.clear()
        _log("cổng đã bị chiếm: theo dõi server đang chạy thay vì bật thêm cái mới")
        return True

    def wait(self) -> int:
        """Chờ server kết thúc, trả mã thoát."""
        with self.lock:
            external = self.external
            proc = self.proc
        if external:
            while not self.stopping and health(self.settings) is not None:
                time.sleep(1.0)
            return 0
        if proc is None:
            return 0
        return proc.wait()

    def alive(self) -> bool:
        with self.lock:
            if self.external:
                return health(self.settings) is not None
            return bool(self.proc and self.proc.poll() is None)

    def poll(self) -> int | None:
        with self.lock:
            return self.proc.poll() if self.proc else None

    def should_restart(self, code: int) -> bool:
        """Có nên bật lại không.

        Có 2 kiểu thoát: mã 75 là yêu cầu cố ý từ panel, còn mọi mã khác là
        crash/kill. Cả hai đều bật lại, nhưng phải chặn trường hợp server crash
        liên tục (sai cấu hình, port bị chiếm) - nếu không nó sẽ spawn process
        không ngừng và làm nặng máy.
        """
        if self.stopping:
            return False
        now = time.monotonic()
        self.restarts = [t for t in self.restarts if now - t < FAST_RESTART_WINDOW]
        if len(self.restarts) >= MAX_FAST_RESTARTS:
            self.last_error = (
                f"Server thoát {MAX_FAST_RESTARTS} lần trong {FAST_RESTART_WINDOW:.0f}s. "
                "Dừng bật lại để tránh khoét CPU. Mở console chạy tay server.py để xem lỗi."
            )
            return False
        self.restarts.append(now)
        return True

    def terminate(self, timeout: float = 10.0) -> None:
        """Tắt server: ưu tiên chạy endpoint admin để nó đóng Chrome cho sạch.

        Chrome agent là con Chrome, không phải con trực tiếp của server (server
        spawn nó, nhưng nó sống độc lập). Nếu kill thẳng server thì Chrome agent
        vẫn chạy và giữ file profile - lần sau mở Chrome lại có thể báo profile
        đang bị khoá.
        """
        if self.stopping:
            return
        self.stopping = True
        if self.alive():
            _log("yêu cầu server tắt gọn (đóng cả Chrome agent)...")
            if not self._ask_stop_via_admin():
                _log("admin không phản hồi, dùng terminate trực tiếp")
                with self.lock:
                    if self.proc:
                        self.proc.terminate()
        with self.lock:
            proc = self.proc
        if proc:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _log("server không chịu thoát, kill")
                proc.kill()
        if not self.quitting:
            # Dừng nhưng không thoát: vòng giám sát chờ ở trạng thái rảnh cho tới
            # khi người dùng bấm "Khởi động server" trên menu.
            self.idle = True
            self.wake.clear()

    def _ask_stop_via_admin(self) -> bool:
        url = f"{self.settings['base_url']}/admin/api/server/stop"
        req = urllib.request.Request(url, method="POST")
        # Đọc lại token lúc này: người dùng có thể đã xoay token trên panel, và
        # token cũ khiến endpoint từ chối -> fallback sang kill cưỡng bức.
        token = read_settings().get("token") or self.settings["token"]
        if token:
            req.add_header("X-Lumi-Admin", token)
        try:
            with urllib.request.urlopen(req, timeout=6.0) as r:
                return r.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False


# ----------------------------------------------------------------------------
# Giao tiếp HTTP
# ----------------------------------------------------------------------------
def get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def health(settings: dict) -> dict | None:
    return get_json(f"{settings['base_url']}/health")


def open_panel(settings: dict) -> None:
    """Mở panel, đưa token qua fragment để không lọt vào access log."""
    url = f"{settings['base_url']}/admin"
    # Đọc lại token: người dùng có thể đã xoay nó trên panel, và mở bằng token
    # cũ thì panel hiện màn hình đăng nhập vô dụng.
    token = read_settings().get("token") or settings.get("token", "")
    if token:
        url += "#t=" + token
    webbrowser.open(url)


def port_busy(host: str, port: int) -> bool:
    """Có server khác (không phải của ta) đang giữ port không."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host if host not in ("0.0.0.0", "::") else "127.0.0.1", port)) == 0


LOGO_PNG = BASE_DIR / "logo.png"


def _draw_fallback_icon(size: int):
    """Icon dự phòng khi thiếu/hỏng logo.png: hình tròn "đèn báo" vẽ bằng code."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(26, 32, 44, 255), outline=(76, 141, 255, 255),
              width=max(2, size // 16))
    # Chấm sáng ở giữa: "đèn báo" của một dịch vụ đang chạy.
    r = size * 0.22
    d.ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), fill=(76, 141, 255, 255))
    return img


def make_icon_image(size: int = 64):
    """Icon tray: logo.png nếu đọc được, không thì vẽ dự phòng.

    pystray trên Windows cần icon 32bit ARGB có alpha thật, nên logo.png được
    ép về RGBA rồi thu nhỏ bằng LANCZOS để viền mượt ở 16/32px.

    Logo gốc có khoảng 10% viền trong suốt mỗi bên; ở 16px mà để nguyên thì
    nội dung chỉ còn ~33% khung và trông rất nhỏ, nên cắt theo vùng có alpha rồi
    thêm lại một chút đệm cân bằng.
    """
    from PIL import Image

    if LOGO_PNG.is_file():
        try:
            with Image.open(LOGO_PNG) as src:
                logo = src.convert("RGBA")
            box = logo.getchannel("A").getbbox()
            if box:
                # Cắt thành hình vuông quanh tâm vùng nội dung: khung vuông giữ
                # đúng tỉ lệ khi ép về size, thay vì bị kéo giãn nhẹ.
                cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                half = max(box[2] - box[0], box[3] - box[1]) / 2 * 1.04
                logo = logo.crop((int(cx - half), int(cy - half),
                                  int(cx + half), int(cy + half)))
            return logo.resize((size, size), Image.LANCZOS)
        except Exception:  # logo hỏng thì vẫn phải có tray icon
            pass
    return _draw_fallback_icon(size)


# ----------------------------------------------------------------------------
# Icon
# ----------------------------------------------------------------------------
def run_tray(server: ServerProcess, settings: dict) -> int:
    icon = Icon(
        "lumi",
        make_icon_image(),
        "Lumi Agent",
        menu=Menu(
            MenuItem(lambda item: "Mở bảng điều khiển", lambda: open_panel(settings), default=True),
            MenuItem(lambda item: "Mở trang chat", lambda: webbrowser.open(settings["base_url"])),
            Menu.SEPARATOR,
            MenuItem(
                lambda item: "Khởi động server",
                lambda: start_from_menu(server),
                enabled=lambda item: not server.alive(),
            ),
            MenuItem(
                lambda item: "Khởi động lại server",
                lambda: restart_server(server),
                enabled=lambda item: server.alive(),
            ),
            MenuItem(
                lambda item: "Dừng server",
                lambda: stop_from_menu(server),
                enabled=lambda item: server.alive(),
            ),
            Menu.SEPARATOR,
            MenuItem("Thoát", lambda: quit_everything(server, icon)),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()
    _log("icon tray đã sẵn sàng. Chuột phải lên icon để có menu.")
    return supervise(server, settings)


def start_from_menu(server: ServerProcess) -> None:
    _log("khởi động server từ menu")
    server.start()


def restart_server(server: ServerProcess) -> None:
    _log("yêu cầu restart từ menu")
    if server.alive():
        server.terminate()
    server.stopping = False
    server.idle = False
    server.restarts.clear()
    server.start()


def stop_from_menu(server: ServerProcess) -> None:
    """Dừng server nhưng giữ icon tray, để bật lại được ngay."""
    server.terminate()
    _log("đã dừng server. Chọn 'Khởi động server' để chạy lại.")


def quit_everything(server: ServerProcess, icon) -> None:
    server.quitting = True
    server.terminate()
    icon.stop()


# ----------------------------------------------------------------------------
# Vòng lặp giám sát (dùng khi không có tray)
# ----------------------------------------------------------------------------
def supervise(server: ServerProcess, settings: dict) -> int:
    """Giữ server sống. Chạy tới khi Ctrl+C, người dùng chọn Thoát, hoặc server
    crash liên tục quá giới hạn.

    Ba trạng thái: `idle` = người dùng đã dừng server (chờ bật lại), `external` =
    đang theo dõi server do tiến trình khác giữ cổng, còn lại là server con.
    """
    while True:
        if server.quitting:
            return 0
        if server.idle:
            server.wake.wait(0.5)
            continue
        server.start()
        rc = server.wait()
        if server.quitting:
            return 0
        if rc == RESTART_EXIT:
            _log("server yêu cầu restart")
            continue
        if rc == STOP_EXIT:
            # Cố ý dừng từ panel: vẫn giữ tray sống (nút "Khởi động server" trong
            # menu sẽ bật lại), nhưng tuyệt đối không tự bật lại.
            _log("server được dừng theo yêu cầu. Chọn 'Khởi động server' để chạy lại.")
            with server.lock:
                server.idle = True
                server.wake.clear()
            continue
        if rc is not None and rc < 0:
            _log(f"server bị kill (mã {-rc})")
        if not server.should_restart(rc):
            _log(server.last_error or "dừng giám sát")
            return 1
        time.sleep(1.0)


def main() -> int:
    harden_streams()
    ap = argparse.ArgumentParser(description="Supervisor cho Lumi Agent")
    ap.add_argument("--no-tray", action="store_true", help="không dùng pystray, chạy trong console")
    ap.add_argument("--status", action="store_true", help="in trạng thái server rồi thoát")
    args = ap.parse_args()

    settings = read_settings()
    use_tray = HAVE_TRAY and not args.no_tray and not args.status
    if not HAVE_TRAY and not args.no_tray and not args.status:
        print("Chưa cài pystray: chạy chế độ console. "
              "`pip install pystray pillow` để có icon tray.", file=sys.stderr)

    if args.status:
        h = health(settings)
        if not h:
            print(f"Không phản hồi tại {settings['base_url']}")
            return 1
        print(f"{settings['base_url']} -> {h.get('status')} / model={h.get('model')}")
        print("admin:", "bật" if settings["token"] else "tắt")
        return 0

    if not SERVER_PY.is_file():
        print(f"Không thấy {SERVER_PY}", file=sys.stderr)
        return 1

    busy = port_busy(settings["host"], settings["port"])
    if busy:
        _log(f"Cổng {settings['host']}:{settings['port']} đã bị chiếm.")

    server = ServerProcess(settings)
    if busy and not server.adopt():
        print("Cổng đang bị một chương trình khác (không phải Lumi) chiếm. "
              "Đóng nó, hoặc đổi port trong config.json.", file=sys.stderr)
        return 1

    if not use_tray:
        _log("chạy không tray (--no-tray). Ctrl+C để thoát.")
        try:
            return supervise(server, settings)
        except KeyboardInterrupt:
            _log("thoát, đang tắt server")
            server.quitting = True
            server.terminate()
            return 0

    return run_tray(server, settings)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
