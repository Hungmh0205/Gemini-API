# KaTeX (vendored)

Bản sao nguyên bản từ npm package `katex@0.19.0`, chỉ giữ `dist/katex.min.js`,
`dist/katex.min.css` và `dist/fonts/` — tức là đúng những gì `chat.html` nạp.

Vì sao vendor thay vì CDN: dự án này chạy offline (xem README chính). Nếu
`chat.html` gọi `https://cdn.jsdelivr.net/.../katex.min.js` thì mất mạng là mất
toàn bộ khả năng hiển thị công thức, đúng lúc bạn đang debug.

Không dùng `contrib/auto-render.min.js`: `chat.html` tự tách `$...$` / `$$...$$`
trước khi escape HTML rồi gọi `katex.render()` cho từng đoạn. Nếu bật
auto-render thêm lần nữa thì công thức bị render hai lần.

Nâng cấp:

    npm pack katex@<version>
    tar -xzf katex-<version>.tgz
    copy package\dist\katex.min.js package\dist\katex.min.css <repo>\vendor\katex\
    xóa thư mục <repo>\vendor\katex\fonts rồi copy lại package\dist\fonts

Giữ nguyên file upstream, không sửa tay, để diff với npm không có nhiễu.

License: MIT — xem `LICENSE`.