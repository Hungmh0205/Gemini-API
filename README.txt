OpenAI-compatible wrapper around the Gemini API. ("Lumi-Agent")

server.py bien mot phien dang nhap Google thanh endpoint OpenAI-compatible
(/v1/chat/completions, /v1/responses, /v1/models) de moi client OpenAI / Agents
SDK troi thang vao Gemini ma khong can SDK chinh thuc hay trinh duyet.

Cai dat
========
  SETUP.md co huong dan day du cho may moi (venv, requirements, --setup,
  --doctor, cu the loi).
  Day la ban tom tat:

  venv\Scripts\python.exe -m pip install -r requirements.txt
  venv\Scripts\python.exe -m pip install -e .
  venv\Scripts\python.exe chrome_launcher.py --setup     <- MOT LAN
  run_server.bat

Server mac dinh o 127.0.0.1:8000, API key lay trong config.json ("api_key").
Client cu dien:

  base_url = "http://127.0.0.1:8000/v1"
  api_key  = "sk-lumi"          # trong config.json

  GET  /health                  # trang thai phien + tuoi cua cookie
  GET  /                         # giao dien chat web
  GET  /docs                    # Swagger


Vong doi phien hoan toan tu dong
=================================
Khong can mo trinh duyet, khong can bam popup, khong can copy cookie. Chi can:

  run_server.bat

Khi config.json bat "browser.enabled" va "browser.mode" = "agent":

  1. Server mo mot profile Chrome rieng "Lumi-Agent" nam trong User Data rieng
     (%LOCALAPPDATA%\Lumi-Agent\Chrome), o che do an ("browser.hidden":
     "offscreen" -> dua cua so ra ngoai manh hinh roi ShowWindow(SW_HIDE) +
     WS_EX_TOOLWINDOW, nen khong co nut taskbar), tai thang gemini.google.com.

  2. Extension trong profile do:
       - GET  /v1/auth/bootstrap  -> lay base_url + API key tu server
       - POST /v1/auth/sync      -> day cookie, server ghi gemini-auth.json
     va tu dong reload client. Khong can nhap key lan dau: key luon lay tu
     server, nen doi key trong config.json cung khong can xoa storage.

  3. Server giu Chrome song (keep_alive): Chrome chet la mo lai. Neu phien
     chet, extension cu 1-3 phut day cookie moi, server tu reload.

  4. Bam Ctrl+C (hay tat cua so) thi server TAT LUON Chrome agent. Dieu nay
     mac dinh bat ("browser.close_on_exit": true) vi cua so Chrome da bi an
     nen khong dong duoc bang tay. Chrome duoc dong lich su (WM_CLOSE) truoc
     de no kip ghi "Secure Preferences" - giet cuong buc se lam Chrome prune
     entry extension unpacked va phai load lai extension.
     Muon giu Chrome chay tiep: "browser": {"close_on_exit": false}.
     "browser.close_timeout" (mac dinh 25 giay) la luc server doi roi moi
     chuyen sang taskkill /F.

     Server CHI dong Chrome mang dung co --user-data-dir cua agent, so khop
     duong dan chuan hoa chinh xac. Chrome ca nhan (User Data mac dinh) va
     Chrome profile khac khong bi dung tay. Neu "browser.user_data_dir" bi
     tro nham vao User Data mac dinh cua Chrome, server bao loi va BO QUA
     viec dong thay vi tat nham Chrome ca nhan.

Agent CO USER DATA RIENG, khong dung chung voi Chrome ca nhan:
  1. Chrome chi cho 1 instance tren moi user-data-dir. Dung chung se bi mo
     Chrome tay gianh quyen instance, va viec giau cua so cua agent co the
     dung vao cua so ca nhan cua ban.
  2. "Secure Preferences" duoc MAC hoa. Chrome prune sach entry cua moi
     extension unpacked khi khoi dong tren user-data-dir da copy (do that:
     8 entry -> 4). Extension unpacked chi song duoc o dung choi profile goc.
  3. Tranh token __Secure-1PSIDTS khi hai ben cung dung mot phien Google.

KHONG duoc dung "mode": "clone" (ban sao profile) - da bo, vi ly do 2.


LAN DAU PHAI LAM CHAY TAY MOT LAN
==================================
Google Chrome >= 137 hard-block --load-extension (da kiem tra tren 152.0.7977.65:
binary chua literal "--load-extension is not allowed in Google Chrome, ignoring."
va khong co feature flag nao bypass duoc).

  python chrome_launcher.py --setup

Lenh nay mo chrome://extensions trong DUNG User Data agent, roi:
  - bat "Developer mode"
  - "Load unpacked" -> chon thu muc gemini-cookie-sync-extension
  - mo gemini.google.com va dang nhap Google

Tu do tat ca tu dong. Kiem tra bang:

  python chrome_launcher.py --doctor


/v1/auth/bootstrap
==================
Khong co xac thuc, vi extension can key de goi /v1/auth/sync nen phai lay key
truoc. Chi tra loi khi:
  - request co header Origin: chrome-extension://...
  - config.json bat "extension": {"bootstrap": true} (mac dinh)
Server chi bind 127.0.0.1 nen day la kenh cuc bo. Tat hoac doi API key:
  - "extension": {"bootstrap": false}, va nhap key tay trong popup extension.
Khac phien
=========
Server KHONG doc cookie tu dia (Chrome >= 127 ma hoa DB cookie bang
App-Bound Encryption). Dung nguon cookie la rieng:
  "cookie_source": "extension"
Extension doc cookie qua Chrome API nen hoat dong ke ca khi ban dung proxy/VPN
cho Gemini. Loi 401 nghia la key trong extension lech voi server - ban da them
tu lay lai key, nen thuong tu khoi phuc.

Loi 401 / Canh bao
==================
  401 tu extension          key sai -> extension tu lay key moi va thu lai
  Chua co ban clone          chay: python chrome_launcher.py --setup
  "giu khoa Network\Cookies"  dong Chrome dang mo profile mau
  "khong tim thay profile"   chay: python chrome_launcher.py --setup
  502 / SSL_ERROR_SYSCALL    loi mang/proxy, thu lai hoac doi config "proxy"

Bao mat
=======
gemini-auth.json la phien Google that, co cam nhat doc, khong in ra, khong
commit len Git (da nam trong .gitignore). Chi chay server tren 127.0.0.1.
