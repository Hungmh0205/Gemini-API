#!/usr/bin/env python3
"""Đọc/ghi config.json theo schema, kèm validate và chốt bảo mật.

Vì sao cần file này: server.py trước đây đọc config bằng `json.loads` thẳng, nên
gõ sai một chữ là server khởi động im lặng với giá trị mặc định, và các knob chết
(`drain_grace`, `cookie_source`, `extension.auto_sync`) không ai biết là chết.
Khi có bảng điều khiển ghi config từ trình duyệt thì chỗ này là hàng rào: mọi
giá trị đều phải qua schema trước khi chạm đĩa.

Nguyên tắc:
  - Ghi atomic (tmp + os.replace) để không bao giờ để lại config.json nửa vời.
  - `extra="allow"`: giữ nguyên key lạ. Upgrade phiên bản sau không xoá sạt
    cấu hình người dùng tự thêm.
  - Env vẫn thắng config (giữ nguyên thứ tự ưu tiên cũ của server.py), và các
    field bị env ghi đè được đánh dấu `locked` để UI không cho sửa.
  - Chặn `browser.user_data_dir` trỏ vào User Data mặc định của Chrome: nếu
    không chặn, `--reset` từ UI sẽ xoá Chrome cá nhân của người dùng.

Environment:
  LUMI_CONFIG          Đường dẫn config (mặc định ./config.json). Dùng để chạy
                      nhiều instance cùng lúc, ví dụ instance test.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from chrome_launcher import user_data_root

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# Những env var nào ghi đè field nào. Dùng chung cho `apply_env` (giá trị hiệu
# lực) và `locked_fields` (UI biết field nào bị khoá).
_SCALAR_ENV = {
    "auth_file": "GEMINI_AUTH_FILE",
    "api_key": "GEMINI_API_KEY",
    "model": "GEMINI_MODEL",
    "proxy": "GEMINI_PROXY",
    "timeout": "GEMINI_TIMEOUT",
    "retries": "GEMINI_RETRIES",
    "refresh_interval": "GEMINI_REFRESH_INTERVAL",
    "healer_interval": "GEMINI_HEALER_INTERVAL",
    "max_concurrent": "GEMINI_MAX_CONCURRENT",
    "max_queue": "GEMINI_MAX_QUEUE",
    "queue_timeout": "GEMINI_QUEUE_TIMEOUT",
    "reload_cooldown": "GEMINI_RELOAD_COOLDOWN",
    "drain_grace": "GEMINI_DRAIN_GRACE",
    "host": "HOST",
    "port": "PORT",
}
_NESTED_ENV = {
    ("browser", "user_data_dir"): "GEMINI_AGENT_USER_DATA_DIR",
    ("browser", "close_timeout"): "GEMINI_CLOSE_TIMEOUT",
    ("browser", "chrome_path"): "GEMINI_CHROME_PATH",
}

HIDDEN_MODES = ("offscreen", "headless", "")


class BrowserConfig(BaseModel):
    """Cấu hình Chrome agent."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    mode: str = "agent"
    user_data_dir: str = ""
    profile_directory: str = "Lumi-Agent"
    chrome_path: str = ""
    hidden: str = "offscreen"
    keep_alive: bool = True
    close_on_exit: bool = True
    close_timeout: float = 25.0
    gemini_url: str = "https://gemini.google.com/app"
    ext_dir: str = ""
    health_url: str = ""
    sync_timeout: float = 180.0

    @field_validator("mode")
    @classmethod
    def _mode_is_agent(cls, v: str) -> str:
        # Chế độ "clone" đã bị bỏ: Chrome prune extension unpacked trên
        # user-data-dir bị copy, nên bản clone luôn có extension chết.
        if (v or "agent").strip().lower() != "agent":
            raise ValueError(
                'browser.mode chỉ còn "agent" (chế độ clone đã bị bỏ vì Chrome prune extension)'
            )
        return "agent"

    @field_validator("hidden")
    @classmethod
    def _hidden_known(cls, v: str) -> str:
        v = (v or "offscreen").strip()
        if v not in HIDDEN_MODES:
            raise ValueError(f"browser.hidden phải là một trong {HIDDEN_MODES}")
        return v

    @field_validator("close_timeout", "sync_timeout")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("thời gian phải > 0")
        return v


class ExtensionConfig(BaseModel):
    """Cấu hình extension "Gemini Cookie Sync"."""

    model_config = ConfigDict(extra="allow")

    bootstrap: bool = True


class AdminConfig(BaseModel):
    """Bảng điều khiển web + token riêng (KHÔNG dùng chung API key)."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    token: str = ""


class LoggingConfig(BaseModel):
    """Log file xoay vòng. Log ra console vẫn luôn bật."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    file: str = "logs/server.log"
    level: str = "INFO"
    max_bytes: int = 2_000_000
    backups: int = 3

    @field_validator("level")
    @classmethod
    def _level_known(cls, v: str) -> str:
        v = (v or "INFO").strip().upper()
        if v not in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("logging.level không hợp lệ")
        return v

    @field_validator("max_bytes")
    @classmethod
    def _positive_bytes(cls, v: int) -> int:
        if v < 10_000:
            raise ValueError("logging.max_bytes tối thiểu 10000")
        return v


class AppConfig(BaseModel):
    """Toàn bộ nội dung config.json."""

    model_config = ConfigDict(extra="allow")

    auth_file: str = "gemini-auth.json"
    api_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    model: str | None = None
    proxy: str = ""
    timeout: float = 60.0
    retries: int = 3
    refresh_interval: float = 300.0
    healer_interval: int = 30
    max_concurrent: int = 6
    max_queue: int = 50
    queue_timeout: float = 30.0
    reload_cooldown: float = 5.0
    # Giữ lại để không phá config cũ. KHÔNG có code nào đọc giá trị này: thời
    # gian chờ client cũ rảnh đã gắn cứng làm tham số `cap` của
    # `_close_old_when_idle`. Xem `_close_old_when_idle` trong server.py.
    drain_grace: float = 30.0

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    extension: ExtensionConfig = Field(default_factory=ExtensionConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port phải trong khoảng 1-65535")
        return v

    @field_validator("host")
    @classmethod
    def _host_ok(cls, v: str) -> str:
        v = (v or "127.0.0.1").strip()
        if any(c in v for c in " \t\r\n/\\"):
            raise ValueError("host không hợp lệ")
        return v

    @field_validator("max_concurrent", "max_queue")
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError("giá trị phải >= 1")
        return v

    @field_validator("timeout", "refresh_interval", "queue_timeout", "reload_cooldown")
    @classmethod
    def _positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("giá trị phải > 0")
        return v

    @field_validator("api_key")
    @classmethod
    def _key_no_space(cls, v: str) -> str:
        # Khoảng trắng trong key là nguyên nhân 401 kinh điển: client gửi
        # "Bearer  sk-lumi" còn header so sánh với "sk-lumi".
        return (v or "").strip()

    def check_paths(self) -> None:
        """Chặn cấu hình nguy hiểm trước khi nó được ghi xuống đĩa.

        Hai trường hợp:

        1. `user_data_dir` là tham số của `taskkill` (xem `close_agent_chrome`), nên
           trỏ nhầm vào User Data mặc định là tự xoá Chrome của người dùng. Server
           có chặn ở lúc đóng (`_shutdown_agent_browser`) nhưng chặn muộn: đến lúc
           đó profile đã bị copy đi rồi. Chặn ngay lúc ghi config.
        2. `auth_file` là file token Google. `..` hoặc đường dẫn tuyệt đối ra
           ngoài thư mục dự án sẽ khiến panel ghi đè/xoá nhầm file của tiến trình
           khác chỉ vì một ô text trong form.
        """
        udd = (self.browser.user_data_dir or "").strip()
        if udd:
            try:
                default = str(user_data_root())
            except Exception:
                default = ""
            if default and touches(udd, default):
                raise UnsafePathError(
                    f"browser.user_data_dir đang trỏ vào (hoặc nằm trong) User Data mặc định "
                    f"của Chrome: {udd}. Agent cần một User Data riêng, nếu không Chrome cá "
                    f"nhân của bạn sẽ bị đụng."
                )
        auth = (self.auth_file or "").strip()
        if auth:
            p = Path(auth)
            if p.is_absolute():
                try:
                    inside = os.path.commonpath(
                        [str(p.resolve()), str(BASE_DIR.resolve())]
                    ) == str(BASE_DIR.resolve())
                except (ValueError, OSError):
                    inside = False
                if not inside:
                    raise UnsafePathError(
                        f"auth_file phải nằm trong thư mục dự án ({BASE_DIR}), không phải {auth}."
                    )
            elif ".." in p.parts:
                raise UnsafePathError(f"auth_file không được chứa '..': {auth}")


class ConfigError(ValueError):
    """config.json sai - kèm danh sách lỗi để hiện lên UI."""


class UnsafePathError(ConfigError):
    """Config sai kiểu nguy hiểm (trỏ vào Chrome cá nhân / ngoài thư mục dự án).

    Tách riêng khỏi `ConfigError` vì người dùng cần phản ứng khác nhau: lỗi cú
    pháp thì server vẫn 500 được, còn lỗi này là "bạn đang cấu hình sai, sửa
    đi" -> admin trả 400 kèm đúng lời giải thích thay vì "Internal Server Error".
    """


def norm_path(value: str) -> str:
    """Chuẩn hoá đường dẫn để so sánh: expand %VAR%, ~, abspath, normpath, không phân biệt hoa/thường."""
    text = os.path.expanduser(os.path.expandvars(str(value or "").strip()))
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def touches(path: str, root: str) -> bool:
    """True nếu `path` chính là `root`, nằm bên trong `root`, hoặc chứa `root`.

    Chặn cả hai chiều là cố ý: profile agent nằm trong User Data cá nhân thì
    `close_agent_chrome`/`browser_reset` sẽ đụng Chrome thật; còn User Data cá nhân
    nằm trong thư mục được trỏ tới thì thao tác ghi/đóng Chrome có thể lan sang
    cây thư mục thật. Chỉ so sánh chuỗi bằng `==` là thiếu cả hai.
    """
    a, b = norm_path(path), norm_path(root)
    if not a or not b:
        return False
    try:
        shared = os.path.commonpath([a, b])
    except ValueError:  # khác ổ đĩa (ví dụ C: vs D:) -> chắc chắn không lồng nhau
        return False
    return shared == a or shared == b


def config_path() -> Path:
    return Path(os.environ.get("LUMI_CONFIG") or CONFIG_FILE)


def read_raw(path: Path | None = None) -> dict:
    """Đọc nguyên văn file (chưa validate). Trả {} nếu chưa có file."""
    p = path or config_path()
    if not p.is_file():
        return {}
    # utf-8-sig: Notepad và PowerShell 5.1 thêm BOM khi lưu, json.loads sẽ ném
    # JSONDecodeError và làm server không khởi động được.
    raw = p.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json không phải JSON hợp lệ: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("config.json phải chứa một object ở cấp gốc")
    return data


def _validate(raw: dict) -> AppConfig:
    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(gốc)'}: {err['msg']}" for err in e.errors()
        )
        raise ConfigError(f"config không hợp lệ: {details}") from e
    try:
        cfg.check_paths()
    except UnsafePathError:
        raise  # giữ nguyên loại để admin_api trả 400 chứ không 500
    except ValueError as e:
        # check_paths() raise ValueError thuần vì nó là validator; ở biên này mọi
        # lỗi cấu hình đều phải là ConfigError để admin_api trả 400 với thông điệp
        # đọc được, thay vì 500 "Internal Server Error".
        raise ConfigError(str(e)) from e
    return cfg


_INT_FIELDS = ("port", "retries", "healer_interval", "max_concurrent", "max_queue")
_FLOAT_FIELDS = ("timeout", "refresh_interval", "queue_timeout", "reload_cooldown", "drain_grace")


def apply_env(cfg: AppConfig) -> AppConfig:
    """Env ghi đè config - giữ đúng thứ tự ưu tiên cũ của server.py."""
    data = cfg.model_dump()
    for field, env in _SCALAR_ENV.items():
        raw = os.getenv(env)
        if not raw:
            continue
        try:
            if field in _INT_FIELDS:
                data[field] = int(raw)
            elif field in _FLOAT_FIELDS:
                data[field] = float(raw)
            else:
                data[field] = raw
        except ValueError:
            # Env sai kiểu thì bỏ qua, giữ giá trị trong file. Server vẫn chạy
            # được và UI sẽ hiện field này là bị khoá.
            continue
    for (section, field), env in _NESTED_ENV.items():
        raw = os.getenv(env)
        if raw:
            data.setdefault(section, {})[field] = raw
    return _validate(data)


def locked_fields() -> list[str]:
    """Field nào đang bị env ghi đè - UI không cho sửa (sửa cũng không có tác dụng)."""
    out = [f for f, env in _SCALAR_ENV.items() if os.getenv(env)]
    for (section, field), env in _NESTED_ENV.items():
        if os.getenv(env):
            out.append(f"{section}.{field}")
    return out


def load(path: Path | None = None) -> AppConfig:
    """Config hiệu lực = file + env, đã validate."""
    return apply_env(_validate(read_raw(path)))


def ensure_secrets(path: Path | None = None) -> tuple[dict, bool]:
    """Sinh `api_key` / `admin.token` nếu đang thiếu, rồi ghi file.

    Trả về `(raw, changed)`. Lý do phải ghi xuống đĩa thay vì chỉ in ra log:
    API key là thứ client OpenAI gửi kèm, còn admin token là thứ bảng điều khiển
    và tray dùng để mở panel. Nếu chỉ giữ trong RAM thì mỗi lần restart là key
    đổi, và người dùng phải bò log server mới biết.
    """
    raw = read_raw(path)
    changed = False
    if not raw.get("api_key"):
        raw["api_key"] = "sk-lumi-" + secrets.token_urlsafe(24)
        changed = True
    admin = raw.setdefault("admin", {})
    if not admin.get("token"):
        admin["token"] = secrets.token_urlsafe(32)
        changed = True
    if changed:
        save(raw, path)
    return raw, changed


def save(raw: dict, path: Path | None = None) -> AppConfig:
    """Validate rồi ghi atomic. Trả về config đã lưu."""
    p = path or config_path()
    cfg = _validate(raw)
    payload = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)
    return cfg


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _reject_unknown(patch: dict, fields: dict, path: str = "") -> None:
    """Chặn key lạ trong patch.

    `extra="allow"` có chủ ý để đọc config cũ không vỡ, nhưng ở đường GHI thì sai:
    gõ `prot` thay vì `port` sẽ thêm một key lạ vào file, còn `port` vẫn giữ giá
    trị cũ - panel hiện "đã lưu" trong khi không có gì thay đổi. Tốt hơn là báo lỗi.
    """
    for key, value in patch.items():
        here = f"{path}{key}"
        if key not in fields:
            raise ConfigError(
                f"Không có cấu hình '{here}'. Các key hợp lệ: {', '.join(sorted(fields))}"
            )
        ann = fields[key].annotation
        if isinstance(value, dict) and isinstance(ann, type) and issubclass(ann, BaseModel):
            _reject_unknown(value, ann.model_fields, here + ".")


def _reject_locked(patch: dict, locked: list[str], path: str = "") -> None:
    """Chặn sửa field đang bị env ghi đè.

    Ghi vào đây là viết vào hư không: file đổi, panel hiện giá trị mới, nhưng
    server vẫn chạy bằng giá trị env. Người dùng sẽ debug rất lâu.
    """
    for key, value in patch.items():
        here = f"{path}{key}"
        if here in locked:
            env = _SCALAR_ENV.get(key) or _NESTED_ENV.get((path.rstrip("."), key)) or "biến môi trường"
            raise ConfigError(
                f"'{here}' đang bị {env} ghi đè nên sửa trong config.json không có tác dụng. "
                f"Bỏ {env} khỏi môi trường rồi sửa lại."
            )
        if isinstance(value, dict):
            _reject_locked(value, locked, here + ".")


def update(patch: dict, path: Path | None = None) -> AppConfig:
    """Merge patch vào file hiện có rồi validate + ghi.

    Merge trên dict thô chứ không trên model: nếu merge trên model, các field
    mặc định sẽ bị ghi đè lên giá trị người dùng đặt trước đó.
    """
    if not isinstance(patch, dict):
        raise ConfigError("body phải là object")
    _reject_unknown(patch, AppConfig.model_fields)
    _reject_locked(patch, locked_fields())
    merged = _deep_merge(read_raw(path), patch)
    save(merged, path)
    return load(path)


def redact(raw: dict) -> dict:
    """Bản sao an toàn để trả ra HTTP: che key, nhưng vẫn cho biết có hay không."""
    out = _deep_merge(raw, {})
    for section, field in (("", "api_key"), ("admin", "token")):
        target = out if not section else out.setdefault(section, {})
        target[field] = _mask(target.get(field))
    return out


def _mask(value: object) -> str:
    """Giữ 4 ký tự cuối để còn nhận ra key, nhưng chỉ khi bí mật đủ dài.

    Với bí mật ngắn thì 4 ký tự cuối gần như là toàn bộ - `********abc` không che
    gì. Ngưỡng 12 ký tự dưới đó thì che hẳn.
    """
    s = str(value) if value else ""
    if not s:
        return ""
    if len(s) <= 12:
        return "*" * 8
    return "*" * 8 + s[-4:]


def summary(cfg: AppConfig) -> dict:
    """Vài con số để tray/UI hiển thị nhanh."""
    return {
        "host": cfg.host,
        "port": cfg.port,
        "model": cfg.model,
        "admin_enabled": cfg.admin.enabled,
        "browser_enabled": cfg.browser.enabled,
        "log_file": cfg.logging.file,
    }
