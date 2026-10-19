"""Test của Lumi-Agent.

Có `__init__.py` để `python -m unittest discover -s tests -t .` chạy được: không có
nó, `discover` báo "Start directory is not importable" dù các test đều import được
qua namespace package khi gọi trực tiếp `tests.test_x`.

Đồng thời thêm chính thư mục này vào `sys.path` vì `net.py` là helper dùng chung,
được `test_save_image.py` import bằng `import net`. Cách đó chỉ chạy được khi
thư mục tests nằm trên `sys.path` — mà `discover -t .` thì không làm vậy. Ở đây
đảm bảo mọi cách gọi (discover, `tests.test_x`, hay chạy file trực tiếp) đều
tương đương nhau.
"""

import sys as _sys
from pathlib import Path as _Path

_here = str(_Path(__file__).resolve().parent)
if _here not in _sys.path:
    _sys.path.insert(0, _here)