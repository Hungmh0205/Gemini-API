"""Launcher EXE wrapper cho Gemini-API.bat.

Chạy file .bat ẩn cửa sổ (invisible), bất đồng bộ (asynchronous):
tiến trình bat được spawn rồi launcher thoát ngay, không chờ.
"""

import os
import sys
import subprocess

def main():
    # Xác định thư mục chứa exe (hoặc script nếu chạy trực tiếp)
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    bat_path = os.path.join(base_dir, "Gemini-API.bat")

    if not os.path.isfile(bat_path):
        # Nếu không tìm thấy bat, hiện thông báo lỗi
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Không tìm thấy file:\n{bat_path}",
                "Gemini-API - Lỗi",
                0x10  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)

    # Tạo STARTUPINFO để ẩn hoàn toàn cửa sổ console
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE

    # Spawn bất đồng bộ: CREATE_NO_WINDOW + không chờ process kết thúc
    subprocess.Popen(
        [bat_path],
        cwd=base_dir,
        startupinfo=si,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    # Launcher thoát ngay, bat chạy nền


if __name__ == "__main__":
    main()
