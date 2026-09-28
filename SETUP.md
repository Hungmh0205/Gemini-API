# SETUP — máy mới, chưa chạy lần nào

Tóm tắt: cài Python → cài thư viện → `python chrome_launcher.py --setup` (1 lần, 3 phút) → đăng nhập Google → `run_server.bat`. Hết.

---

## 0. Yêu cầu

| Thứ | Cần | Ghi chú |
|---|---|---|
| Windows | 10/11, 64-bit | chỉ dùng `pywin32` |
| Python | 3.11 – 3.13 | kiểm tra: `python --version` |
| Google Chrome | bất kỳ bản nào | phải **đăng nhập Google** |
| Google account | 1 | dùng cho Gemini |

> Không cần Node.js. Không cần Docker. Không cần build gì.

---

## 1. Tạo venv + cài thư viện

```powershell
cd "D:\New folder\Lumi-Agent\Gemini-API"
python -m venv venv
venv\Scripts\python.exe -m pip install -U pip
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m pip install -e .
```

Cài bản thư viện gốc ở chế độ editable là **bắt buộc** — `src\` không nằm trong `sys.path`, thiếu bước này `import gemini_webapi` sẽ hỏng.

---

## 2. Agent có User Data riêng, hoàn toàn độc lập

Profile agent nằm ở:

```
%LOCALAPPDATA%\Lumi-Agent\Chrome\Lumi-Agent
```

**Không dùng chung** với `C:\Users\<bạn>\AppData\Local\Google\Chrome\User Data`. Ba lý do:

1. **Chrome chỉ cho 1 instance trên mỗi User Data.** Dùng chung thì mở Chrome tay sẽ giành quyền instance, và việc giấu cửa sổ của agent có thể đụng vào cửa sổ cá nhân của bạn.
2. **`Secure Preferences` được MAC hoá.** Chrome **prune sạch** entry của mọi extension unpacked khi khởi động trên user-data-dir bị copy (đo thực tế: 8 entry → 4). Nên extension unpacked chỉ sống được ở đúng User Data gốc — không thể "copy profile rồi lấy extension theo".
3. Rủi ro tranh token `__Secure-1PSIDTS` nếu cùng phiên Google.

Cấu hình trong `config.json`:

```json
"browser": {
  "enabled": true,
  "mode": "agent",
  "user_data_dir": "%LOCALAPPDATA%\\Lumi-Agent\\Chrome",
  "profile_directory": "Lumi-Agent",
  "hidden": "offscreen",
  "keep_alive": true
}
```

`mode: "clone"` (bản cũ) **đã bị bỏ** — vô dụng vì lý do #2.

---

## 3. `--setup`: load extension (MỘT LẦN, bằng tay)

```powershell
venv\Scripts\python.exe chrome_launcher.py --setup
```

Lệnh mở Chrome hiện ra ở `chrome://extensions` với đúng User Data agent. Làm 2 việc:

1. Bật **Developer mode** (góc trên bên phải).
2. Bấm **Load unpacked** → chọn đúng thư mục:
   ```
   D:\New folder\Lumi-Agent\Gemini-API\gemini-cookie-sync-extension
   ```

Lệnh tự báo `OK - extension đã sẵn sàng` khi Chrome ghi entry vào profile.

> **Vì sao bắt buộc bấm tay?**
> Chrome ≥ 137 **hard-block** `--load-extension`. Đã kiểm tra trực tiếp trong `chrome.dll` bản 152.0.7977.65: có literal
> `--load-extension is not allowed in Google Chrome, ignoring.`
> và **không có feature flag nào bypass được** (đã thử, và quét toàn bộ DLL không tìm thấy feature liên quan). Đây là giới hạn của Chrome, không phải của dự án.

---

## 4. Đăng nhập Google (MỘT LẦN)

Trong cửa sổ Chrome vừa mở, vào <https://gemini.google.com/app> và đăng nhập tài khoản Google.

Cookie Google nằm riêng trong từng profile, nên profile agent mới không thừa hưởng phiên đăng nhập của profile cá nhân. Extension bắt buộc có `SAPISID` + `__Secure-1PSID` (`collect.js:60`) — thiếu là không lấy được cookie gì.

Chỉ cần 1 lần. `__Secure-1PSIDTS` được extension tự xoay mỗi ~1–3 phút nên không phải đăng nhập lại.

---

## 5. Kiểm tra

```powershell
venv\Scripts\python.exe chrome_launcher.py --doctor
```

Cần thấy `exit=0`:

```
  User Data : C:\Users\<bạn>\AppData\Local\Lumi-Agent\Chrome
  profile   : Lumi-Agent
  Local State: True
  extension : CHẠY ĐƯỢC
  Cookies DB: <số bytes>
```

Nếu `CHƯA ĐƯỢC` → `--setup` chưa xong, hoặc bạn load nhầm profile khác.

---

## 6. Chạy

```powershell
run_server.bat
```

Xong. Từ đây không cần động vào gì:

- Server tự mở Chrome **ẩn** (offscreen + `SW_HIDE`, không có nút taskbar) vào Gemini.
- Extension tự hỏi `GET /v1/auth/bootstrap` lấy API key — **không cần nhập tay**.
- Extension tự `POST /v1/auth/sync` mỗi ~1–3 phút.
- Chrome chết là server mở lại.

Kiểm tra:

```powershell
curl.exe http://127.0.0.1:8000/health
```

```json
{"status":"ok","account_status":"AVAILABLE","cookie_source":"extension","auth_age_sec":42}
```

`auth_age_sec` nhỏ và giảm dần = đang sync bình thường.

Dùng thử:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-lumi")
print(client.chat.completions.create(
    model="gemini-flash",
    messages=[{"role": "user", "content": "xin chào"}],
).choices[0].message.content)
```

---

## 7. Đổi sang tài khoản Google khác

```powershell
venv\Scripts\python.exe chrome_launcher.py --reset
```

Lệnh này xoá **cả hai** thứ, và cần cả hai:

| Xoá | Vì sao |
|---|---|
| `%LOCALAPPDATA%\Lumi-Agent\Chrome` | cookie Google + bản ghi extension trong `Secure Preferences` |
| `gemini-auth.json` (+ `.bak*`) | **nếu chỉ xoá profile thì server vẫn xác thực bằng tài khoản cũ** — bạn sẽ tưởng đã đổi acc nhưng thực ra không |

Lệnh **tự đóng Chrome agent** trước, không bắt bạn đóng tay:

1. Gửi `WM_CLOSE` (tức `taskkill /PID` **không** `/F`) — Chrome tự thoát và tự ghi `Secure Preferences`.
2. Chờ tối đa 25 giây.
3. Chỉ khi đã cố hết mà vẫn còn, mới giết cưỡng bức `/F`, kèm cảnh báo.

> Bước 1 **không được bỏ**. Chrome tắt cưỡng bức sẽ không ghi `Secure Preferences` — đó chính là nơi lưu extension unpacked. Đã gặp thật: giết cứng xong extension biến mất và phải load lại từ đầu.

Lệnh hỏi xác nhận trước khi xoá. Nếu gặp `PermissionError` (profile đặt ở vị trí bị policy khoá), lệnh tự mở UAC để chạy lại với quyền admin. Với đường dẫn mặc định thì không cần — `%LOCALAPPDATA%` và thư mục code đều thuộc user.

Sau đó làm lại đúng một vòng như lần cài đầu:

```powershell
venv\Scripts\python.exe chrome_launcher.py --setup   # load unpacked + đăng nhập acc mới
run_server.bat
```

Bản ghi extension nằm trong profile nên **mất theo profile** — không tránh được bước load unpacked. Đây là hạn chế của Chrome với extension unpacked, xem mục 3.

---

## 8. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `[Errno 10048]` khi chạy | Port 8000 đã bị chiếm | `netstat -ano | findstr :8000` rồi `taskkill /PID <pid> /F` |
| `--doctor` báo `CHƯA ĐƯỢC / đã prune` | Đang ở `mode: clone` | Không dùng mode clone nữa, đặt `"mode": "agent"` rồi `--setup` |
| `Chưa tìm thấy profile nào đã cài extension` | Chưa `--setup` | Chạy `--setup` |
| `/health` → `UNAUTHENTICATED` | Chưa đăng nhập Google trong profile agent | Mở profile agent → gemini.google.com → đăng nhập |
| `Cookies DB: 0 bytes` | Profile agent chưa từng mở trang nào | Mở Chrome với profile agent vài giây |
| `502` / `SSL_ERROR_SYSCALL` | Lỗi mạng/proxy | Đổi `config.json → "proxy"` |
| `ImportError gemini_webapi` | Chưa cài editable | `pip install -e .` |
| `ModuleNotFoundError: win32com` | Thiếu pywin32 | `pip install pywin32` |

**Lưu ý vận hành:** chỉ mở Chrome agent qua server. Đừng mở tay — nếu mở tay rồi đóng, server sẽ mở lại instance ẩn (đó là `keep_alive`, đúng thiết kế). Nếu muốn kiểm tra thủ công thì dùng `chrome_launcher.py --setup`.

---

## 9. Nâng cấp dự án sau này

Thay thư mục, chạy lại 2 lệnh:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m pip install -e .
```

Profile agent nằm ở `%LOCALAPPDATA%\Lumi-Agent` (ngoài thư mục code) nên **không mất** phiên đăng nhập khi cập nhật.
