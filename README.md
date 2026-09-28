# Lumi-Agent — Gemini Web to OpenAI-compatible API

Chuyển Google Gemini Web (`gemini.google.com`) thành một **API server cục bộ tương thích OpenAI**. Bất kỳ client nào nói được OpenAI (Python `openai` SDK, OpenAI Agents SDK, Cursor, NextChat, Cherry Studio, ChatBox, Codex CLI...) chỉ cần trỏ `base_url` về máy này là dùng Gemini qua tài khoản **miễn phí** của bạn — không cần trả tiền cho Google AI Studio, không cần API key của Google.

```
┌─────────────┐   OpenAI format   ┌──────────────────┐   Gemini web proto   ┌────────────────────┐
│  Client app  │ ────────────────► │  server.py        │ ───────────────────► │  gemini.google.com │
│ (SDK/UI/CLI) │ ◄──────────────── │ 127.0.0.1:8000   │ ◄─────────────────── │ (tài khoản của bạn)│
└─────────────┘   chat/completions └──────────────────┘     curl-cffi        └────────────────────┘
```

---

## Tính năng nổi bật

- **Tương thích OpenAI hoàn toàn**: `POST /v1/chat/completions`, `POST /v1/responses` (Responses API), `GET /v1/models`.
- **Streaming SSE** dạng typewriter, dùng được với mọi client hỗ trợ streaming.
- **Tool / Function Calling**: chuyển lệnh gọi hàm của OpenAI thành định dạng Gemini hiểu được.
- **Đa dạng model**: Flash, Pro, Thinking... (tùy tài khoản), có thể map tên model lạ (như `gpt-4o-mini`) sang model Gemini mặc định.
- **Tự động xác thực không cần thao tác tay**:
  - Launcher tự mở **Chrome ẩn** với profile riêng (`Lumi-Agent`).
  - Extension Chrome "Gemini Cookie Sync" tự lấy API key từ server rồi tự đẩy cookie tươi qua `POST /v1/auth/sync` (không cần bấm popup).
  - Watchdog tự reload session khi cookie hết hạn, tự hồi sinh nếu bị lỗi tạm thời.
- **Bảng điều khiển Web** (`/admin`): xem trạng thái, sửa cấu hình, xem log, rotate token, bật/tắt Chrome agent, khởi động lại / dừng server.
- **System tray supervisor**: icon tray giữ server sống, khởi động lại khi crash, menu Start/Stop/Restart/Open panel.
- **Chat UI thử nghiệm** ngay tại `/`.
- **Quản lý đồng thời**: hàng đợi, giới hạn concurrent, timeout — tránh quá tải tài khoản Gemini.
- **CLI đầy đủ** cho thư viện gốc: `ask`, `reply`, `research`, `list`, `read`, `models`, `download`, `inspect`.
- **Bản deploy serverless** bằng Cloudflare Workers (thư mục `cloudflare/`) cho việc chia sẻ API qua internet.

---

## Kiến trúc & Các thành phần

| Thành phần | Tệp | Vai trò |
|---|---|---|
| Thư viện lõi | `src/gemini_webapi/` | Wrapper async cho Gemini Web (giao thức nội bộ của Gemini), nhánh của [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) |
| Server gateway | `server.py` | FastAPI, expose các endpoint OpenAI-compatible, quản lý session/client, watchdog auth |
| Launcher Chrome | `chrome_launcher.py` | Clone/điều phối profile Chrome ẩn, cài extension, giữ Chrome sống |
| Extension | `gemini-cookie-sync-extension/` | Đọc cookie + token XSRF (`SNlM0e`) + `gemini_bl` từ tab Gemini, tự động push lên server |
| Admin Panel | `admin_api.py` + `admin.html` | Router `/admin` — điều khiển server qua HTTP |
| System Tray | `tray.py` | Supervisor riêng, giữ server chạy, icon tray |
| Cấu hình | `config_store.py` + `config.json` | Model Pydantic + file cấu hình (được gitignore) |
| Log | `logstore.py` | Log xoay vòng (rotating) với kích thước và số bản backup cấu hình được |
| CLI | `cli.py` | Giao diện dòng lệnh dùng trực tiếp thư viện gốc |
| Deploy serverless | `cloudflare/` | Worker chuyển Gemini Web thành OpenAI API trên Cloudflare |
| Tích hợp Windows | `Gemini-API.bat`, `startup.bat`, `run_server-debug.bat`, `Gemini-API.exe` | Chạy bằng Python portable, tự thêm vào Startup |

### Luồng hoạt động (chế độ tự động)

```
 ┌──────────────────────────────────────────────────────────────────┐
 │ server.py khởi động                                              │
 │   │                                                             │
 │   ├─► chrome_launcher mở Chrome ẩn trên profile "Lumi-Agent"    │
 │   │      └─► extension "Gemini Cookie Sync" nạp theo profile     │
 │   │             └─► GET /v1/auth/bootstrap → lấy {base_url, key} │
 │   │                    └─► POST /v1/auth/sync → đẩy cookie tươi  │
 │   │                          └─► server ghi gemini-auth.json     │
 │   │                                                              │
 │   ├─► GeminiClient.init(__Secure-1PSID…) → login Gemini Web     │
 │   │                                                              │
 │   └─► Listen 127.0.0.1:8000 → /v1/chat/completions, /v1/models   │
 │        + watchdog: cookie hết hạn → yêu cầu extension sync lại   │
 │        + healer:  Chrome chết → mở lại; client lỗi → reload      │
 └──────────────────────────────────────────────────────────────────┘
```

---

## Công nghệ sử dụng

- **Python ≥ 3.11** (được đóng gói chạy bằng Python 3.13 embeddable trong `python_portable/`, không cần cài Python).
- **FastAPI + Uvicorn** — server HTTP async, tự động hóa OpenAPI, CORS cho extension.
- **curl-cffi** — HTTP/2 + TLS fingerprint giả lập Chrome để Gemini Web chấp nhận request.
- **Pydantic v2** — validate config và payload request.
- **orjson** — JSON nhanh.
- **loguru** — log của thư viện lõi.
- **pywin32 + WMI** (Windows) — điều khiển cửa sổ Chrome ẩn, tìm process theo command line.
- **pystray + Pillow** — icon system tray.
- **browser-cookie3** (tuỳ chọn) — đọc cookie trực tiếp từ profile Chrome (không còn tin cậy từ Chrome ≥ 127 do App-Bound Encryption).
- **Chrome Extension Manifest V3** — đọc cookie Google/nhận XSRF token.
- **JavaScript/HTML thuần** — chat UI và admin panel.

---

## Cấu trúc thư mục

```
Gemini-API/
├── server.py                          # Server OpenAI-compatible (FastAPI)
├── admin_api.py                       # Router /admin (API điều khiển)
├── admin.html                         # Giao diện bảng điều khiển
├── chat.html                          # Chat UI thử nghiệm
├── chrome_launcher.py                 # Launcher + giám hộ Chrome ẩn
├── cli.py                             # CLI thư viện gốc
├── config_store.py                    # Model + quản lý config.json
├── config.json                        # Cấu hình server (không commit lên git)
├── logstore.py                        # Log xoay vòng
├── tray.py                            # System tray supervisor
├── gemini-cookie-sync-extension/      # Extension Chrome "Gemini Cookie Sync"
├── src/gemini_webapi/                 # Thư viện lõi (wrapper Gemini Web)
├── cloudflare/                        # Bản deploy Cloudflare Workers
├── python_portable/                   # Python 3.13 embeddable (gitignore)
├── logs/                              # Log runtime (gitignore)
├── tests/                             # Unit tests
├── Gemini-API.exe                     # Bản chạy đóng gói
├── Gemini-API.bat                     # Khởi động bằng Python portable
├── startup.bat                        # Tự thêm vào Startup Windows
├── run_server-debug.bat               # Chạy server.py trực tiếp (debug)
└── requirements.txt
```

---

## Yêu cầu hệ thống

- **Windows 10/11** (dùng pywin32/WMI, pystray).
- Một tài khoản Google đã đăng nhập được vào [gemini.google.com](https://gemini.google.com) (kể cả tài khoản miễn phí).
- Chrome (hoặc Edge) hiện đại.
- Nếu chạy từ source: **Python ≥ 3.11**.

---

## Cài đặt & Setup

### Cách 1 — Chạy file đóng gói (nhanh nhất)

1. Giải nén thư mục, đảm bảo có `Gemini-API.exe` (hoặc `Gemini-API.bat` + `python_portable/`).
2. Bấm đúp `Gemini-API.exe` / `Gemini-API.bat`. Một icon **Lumi** xuất hiện trên system tray.
3. Lần đầu dùng, làm theo "Thiết lập Chrome agent" bên dưới để có phiên Gemini.

### Cách 2 — Tự thiết lập Chrome agent (một lần duy nhất)

Chrome ≥ 137 **chặn cứng** `--load-extension`, nên profiie agent phải có extension nạp bằng tay đúng một lần:

```bat
python chrome_launcher.py --setup
```

- Lệnh này mở Chrome trên profile riêng `Lumi-Agent` (không dùng chung với Chrome cá nhân).
- Trong cửa sổ vừa mở: **chrome://extensions** → bật **Developer mode** → **Load unpacked** → chọn thư mục `gemini-cookie-sync-extension`.
- Đăng nhập Google vào `gemini.google.com/app`, tải lại trang.
- Đóng Chrome lại, khởi động server. Kể từ đây mọi thứ tự động.

Kiểm tra trạng thái:

```bat
python chrome_launcher.py --doctor
```

### Cách 3 — Chạy từ source có cài đủ thư viện

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Server mặc định chạy tại **http://127.0.0.1:8000**.

Khi dùng Python portable (không cần cài Python):

```bat
python_portable\python.exe -X utf8 -m pip install -r requirements.txt
python_portable\python.exe -X utf8 server.py
```

### Lấy cookie thủ công (nếu bạn không dùng Chrome agent)

Mở `https://gemini.google.com/app` đã đăng nhập, lấy cookie `__Secure-1PSID` và `__Secure-1PSIDTS` (F12 → Application → Cookies), lưu vào `gemini-auth.json`:

```json
{
  "__Secure-1PSID": "...",
  "__Secure-1PSIDTS": "..."
}
```

Hoặc set biến môi trường `GEMINI_AUTH_FILE` trỏ tới file đó.

> Lưu ý: Chrome ≥ 127 mã hoá database cookie bằng App-Bound Encryption nên cách đọc cookie trực tiếp không còn đáng tin cậy — dùng extension là kênh khuyến nghị duy nhất.

---

## Cấu hình

### `config.json`

```jsonc
{
  "auth_file": "gemini-auth.json",  // file lưu session Google
  "api_key": "sk-lumi",             // API key client phải gửi (Authorization: Bearer)
  "host": "127.0.0.1",
  "port": 8000,
  "model": "gemini-flash",          // model mặc định khi client gửi tên lạ
  "cookie_source": "extension",
  "proxy": "",                      // proxy nếu cần
  "timeout": 60,                    // timeout khởi tạo client (giây)
  "retries": 3,                     // số lần thử khởi tạo lại
  "refresh_interval": 300,          // chu kỳ tự làm tươi session (giây)
  "healer_interval": 30,            // chu kỳ watchdog (giây)
  "max_concurrent": 6,              // số request Gemini tối đa đồng thời
  "max_queue": 50,                  // độ dài hàng đợi
  "queue_timeout": 30,              // chờ trong hàng đợi tối đa
  "reload_cooldown": 5,             // thời gian chờ tối thiểu giữa 2 lần reload
  "drain_grace": 30,                // thời gian chờ request đang chạy khi tắt
  "browser": {
    "enabled": true,
    "mode": "agent",                // "agent": profile riêng, không clone
    "user_data_dir": "%LOCALAPPDATA%\\Lumi-Agent\\Chrome",
    "profile_directory": "Lumi-Agent",
    "chrome_path": "",               // để trống = tự tìm Chrome
    "hidden": "offscreen",           // ẩn cửa sổ Chrome
    "keep_alive": true,              // Chrome chết là mở lại
    "close_on_exit": true,
    "close_timeout": 25,
    "gemini_url": "https://gemini.google.com/app",
    "sync_timeout": 180
  },
  "extension": { "bootstrap": true }, // cho phép extension tự lấy key qua /v1/auth/bootstrap
  "admin": { "enabled": true, "token": "admin" },
  "logging": {
    "enabled": true,
    "file": "logs/lumi.log",
    "level": "INFO",
    "max_bytes": 2000000,
    "backups": 3
  }
}
```

Để sinh `api_key` và `admin.token` mạnh ngẫu nhiên:

```bat
python server.py --gen-admin-token
```

### Biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `GEMINI_API_KEY` | API key client phải gửi (nếu chưa set, sinh ngẫu nhiên và in lúc khởi động) |
| `GEMINI_AUTH_FILE` | Đường dẫn file auth JSON (mặc định `./gemini-auth.json`) |
| `GEMINI_MODEL` | Model mặc định khi client gửi tên Gemini không biết |
| `GEMINI_PROXY` | Proxy URL truyền cho GeminiClient |
| `GEMINI_TIMEOUT` / `GEMINI_RETRIES` | Thời gian chờ và số lần thử khởi tạo |
| `GEMINI_REFRESH_INTERVAL` | Chu kỳ tự làm tươi session |
| `GEMINI_HEALER_INTERVAL` | Chu kỳ watchdog |
| `GEMINI_MAX_CONCURRENT` / `GEMINI_MAX_QUEUE` / `GEMINI_QUEUE_TIMEOUT` | Giới hạn đồng thời / hàng đợi |
| `PORT` / `HOST` | Cổng và địa chỉ bind (ghi đè config) |
| `LUMI_CONFIG` | Đường dẫn config khác (dùng để test nhiều instance) |
| `LUMI_LOG_FILE` / `LUMI_LOG_LEVEL` | Ghi đè cấu hình log |

---

## Sử dụng

### 1. Chat UI ngay tại server

Mở **http://127.0.0.1:8000/** — giao diện chat tối giản, đính kèm ảnh được, điền API key và model phía trên.

### 2. `curl`

```bash
# Không stream
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-lumi" \
  -d '{
    "model": "gemini-flash",
    "messages": [{"role": "user", "content": "Xin chào!"}],
    "stream": false
  }'

# Streaming
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-lumi" \
  -d '{"model":"gemini-flash","messages":[{"role":"user","content":"Kể chuyện"}],"stream":true}'

# Danh sách model
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-lumi"
```

### 3. Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-lumi")

resp = client.chat.completions.create(
    model="gemini-flash",
    messages=[{"role": "user", "content": "Giải thích AGPL là gì?"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

### 4. Các endpoint

| Method | Path | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/v1/models` | Danh sách model theo định dạng OpenAI | `Bearer` API key |
| `POST` | `/v1/chat/completions` | Chat completions (stream/không stream, tools) | `Bearer` API key |
| `POST` | `/v1/responses` | OpenAI Responses API | `Bearer` API key |
| `GET` | `/health` | Kiểm tra trạng thái server | không |
| `GET` | `/v1/auth/bootstrap` | Cấp `{base_url, api_key}` cho extension (chỉ chấp nhận origin `chrome-extension://`) | không |
| `POST` | `/v1/auth/sync` | Nhận cookie tươi từ extension | API key |
| `GET` | `/` | Chat UI | — |
| `GET` | `/admin` | Bảng điều khiển | token `X-Lumi-Admin` |

### 5. Bảng điều khiển `/admin`

- Token mặc định `admin` — đổi ngay trong `config.json` (`admin.token`) hoặc chạy `--gen-admin-token`.
- Đăng nhập bằng API key và admin token, quản lý: trạng thái Gemini, thông tin tài khoản, cấu hình (sửa lưu ngay), xoay API key, log realtime, quản lý profile Chrome, bật/tắt Chrome agent, restart/dừng server.

### 6. CLI thư viện gốc

```bat
python cli.py --cookies-json gemini-auth.json ask "Xin chào"
python cli.py --cookies-json gemini-auth.json reply c_xxxx... "Tiếp tục"
python cli.py --cookies-json gemini-auth.json models
python cli.py --cookies-json gemini-auth.json list
python cli.py --cookies-json gemini-auth.json read c_xxxx...
python cli.py --cookies-json gemini-auth.json research send --prompt "Nghiên cứu về LLM nhúng"
python cli.py --cookies-json gemini-auth.json research get c_xxxx... --output report.md
```

---

## Escape-hatch khi có sự cố

| Vấn đề | Kiểm tra / khắc phục |
|---|---|
| Server không khởi động, `config.json` lỗi | Nội dung phải hợp lệ; server chấp nhận cả BOM (utf-8-sig) |
| `401 Unauthorized` | Sai API key trong header `/admin` — sai admin token |
| Extension không sync | Bật `extension.bootstrap=true`; kiểm tra `chrome_launcher.py --doctor` |
| `HTTP 405` từ Gemini | Build label (`gemini_bl`) lỗi thời — update trong extension/server |
| Chrome agent không mở | Chạy `python chrome_launcher.py --doctor` để chẩn đoán |
| Server crash liên tục | `tray.py` giới hạn 5 lần restart nhanh trong 60 giây, sau đó dừng để tránh vòng lặp — đọc `logs/lumi.log` |
| Muốn chạy 2 instance | Đặt `LUMI_CONFIG` đến file config khác, đổi cổng |

---

## Bảo mật

- Server chỉ bind **127.0.0.1** theo mặc định — không lộ ra mạng ngoài.
- `gemini-auth.json` chứa **cookie phiên Google thật**, tương đương mật khẩu tài khoản: **tuyệt đối không commit lên Git, không gửi ra server bên ngoài** (các file này đã nằm trong `.gitignore`).
- Đổi `api_key` và `admin.token` ngay sau khi cài đặt (`python server.py --gen-admin-token`).
- `cloudflare/worker.js` nếu deploy công khai thì thiết lập `API_KEYS` whitelist và thay đổi key mặc định `sk-gemini`.

---

## Phát triển & kiểm thử

```bat
python -m unittest discover tests
```

Chạy lint/typecheck (nếu có): `pip install ruff pyright` rồi `ruff check .`.

---

## Giấy phép

Dự án này bao gồm:

- **Phần code gốc của dự án** (server.py, chrome_launcher.py, admin_api.py, tray.py, cli.py, config_store.py, logstore.py, extension...) được phát hành theo giấy phép **MIT** — xem `LICENSE`.
- **Thư viện lõi `src/gemini_webapi/`** là nhánh của [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API), kế thừa giấy phép **GNU AGPL-3.0** (file `LICENSE` gốc). Bất kỳ thay đổi/phân phối lại phần này đều phải tuân thủ AGPL-3.0.

---

*README viết cho mục đích minh hoạ. Tham chiếu chi tiết từng module nằm ngay trong docstring của `server.py`, `chrome_launcher.py`, `tray.py`.*