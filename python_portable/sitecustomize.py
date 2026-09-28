"""Lam ban portable that su khong phu thuoc may nao da tao ra no.

Khong sua file ._pth truc tiep, vi CPython chi chap nhan MOT dong "import site"
va se tu choi cac dong import khac (loi "unsupported 'import' line").
Thay vao do, file nay chay tu dong o cuoi site.main() va bo di cac duong dan
site-packages cua user.

Vi sao can: mot lan chay tren may nay, sys.path chua
  C:\\Users\\<ten>\\AppData\\Roaming\\Python\\Python313\\site-packages
va cac .pth trong do (pywin32.pth, __editable__.gemini_webapi.pth) keo theo
pywin32 va ca thu muc src cua may dua. Copy folder nay sang may khac la cac
duong dan do bien mat -> thieu pywin32, hoac worse: no noi sang bo package cua
user khac va lam lech version.
"""

import os
import sys

_BAD = os.path.normcase(os.path.join("AppData", "Roaming", "Python"))


def _is_user_site(path: str) -> bool:
    try:
        return _BAD in os.path.normcase(os.path.abspath(path))
    except (TypeError, ValueError):
        return False


_filtered = [p for p in sys.path if not _is_user_site(p)]
if len(_filtered) != len(sys.path):
    sys.path[:] = _filtered
