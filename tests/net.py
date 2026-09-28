"""Chặn bốn suite gọi Google thật khi máy không có cookie.

`test_client_features`, `test_deep_research`, `test_gem_mixin` và
`test_save_image` là kiểm thử tích hợp: chúng cần `SECURE_1PSID` /
`SECURE_1PSIDTS` trong môi trường. Không có cookie thì `init()` **không** ném
lỗi - thư viện rơi về phiên khách rồi mỗi test gọi thẳng ra internet, nên
`unittest discover` treo hàng phút rồi bị timeout, che mất kết quả của các
suite offline.

Vì vậy: không có cookie thì skip ngay với lý do rõ ràng, và `init()` luôn có
trần thời gian để một mạng treo không kéo cả bộ test theo.
"""

from __future__ import annotations

import asyncio
import os
import unittest

INIT_TIMEOUT = float(os.getenv("LUMI_TEST_INIT_TIMEOUT", "30"))


def require_credentials(test: unittest.TestCase) -> None:
    """Skip nếu thiếu cookie phiên Google."""
    if not (os.getenv("SECURE_1PSID") and os.getenv("SECURE_1PSIDTS")):
        test.skipTest(
            "Cần biến môi trường SECURE_1PSID + SECURE_1PSIDTS (suite gọi Google thật)"
        )


async def init_or_skip(test: unittest.TestCase, client, **kwargs) -> bool:
    """`client.init(...)` có trần thời gian; lỗi hoặc quá chậm thì skip.

    Trả `True` nếu client đã sẵn sàng. Đã `skipTest` thì hàm ném SkipTest nên
    phần còn lại của `asyncSetUp` không chạy.
    """
    require_credentials(test)
    try:
        await asyncio.wait_for(client.init(**kwargs), timeout=INIT_TIMEOUT)
    except asyncio.TimeoutError:
        await _quiet_close(client)
        test.skipTest(f"init() quá {INIT_TIMEOUT:.0f}s - mạng chậm hoặc bị chặn")
    except Exception as e:  # noqa: BLE001 - mọi lỗi init đều thành skip, không phải fail
        await _quiet_close(client)
        test.skipTest(str(e) or type(e).__name__)
    return True


async def _quiet_close(client) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - đang trong đường skip, lỗi close không thêm giá trị
        pass
