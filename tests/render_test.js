#!/usr/bin/env node
/*
 * Test cho bộ render Markdown + toán học của chat.html.
 *
 * Cố ý đọc chat.html thay vì test một bản sao: nếu copy code ra file riêng thì
 * chỉ cần sửa chat.html là test cũ vẫn xanh trên một thứ không còn tồn tại.
 *
 * Chạy: node tests/render_test.js   (không cần mạng, không cần trình duyệt)
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "chat.html"), "utf8");

/* Chỉ lấy script NỘI BỘ, bỏ <script src> (KaTeX) — cái đó là file vendor. */
const inline = [];
for (const m of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
  inline.push(m[1]);
}
if (!inline.length) throw new Error("khong tim thay script noi bo trong chat.html");
const js = inline.join("\n");

const start = js.indexOf("function esc(");
const end = js.indexOf("function addMsg");
if (start < 0 || end < 0) {
  throw new Error("khong tim thay khoi renderer (esc..addMsg) — chat.html da doi cau truc?");
}
const rendererSrc = js.slice(start, end);

const sandbox = {
  module: { exports: {} },
  katex: require(path.join(ROOT, "vendor", "katex", "katex.min.js")),
  console,
};
vm.createContext(sandbox);
vm.runInContext(
  rendererSrc + "\nmodule.exports = { renderMarkdown, inlineMd, esc, safeUrl };",
  sandbox
);
const { renderMarkdown } = sandbox.module.exports;

let failed = 0;
function ok(cond, name, extra) {
  if (cond) {
    console.log("  ok   " + name);
  } else {
    failed++;
    console.log("  FAIL " + name + (extra ? "  ->  " + extra : ""));
  }
}
function reject(re, name, src) {
  ok(!re.test(src), name, src.slice(0, 200));
}
const strip = (h) =>
  h.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, " ").replace(/\s+/g, " ").trim();

console.log("markdown nen");
const markdown = [
  ["bold", "**dam**", /<strong>dam<\/strong>/],
  ["italic", "*nghieng*", /<em>nghieng<\/em>/],
  ["bold+italic", "***ca hai***", /<strong><em>ca hai<\/em><\/strong>/],
  // `_` KHONG duoc dung lam italic, neu khong `some_name` se bi nghieng.
  ["snake_case giu nguyen", "x some_name y", /some_name/],
  ["ul", "- a\n- b", /<ul><li>a<\/li><li>b<\/li><\/ul>/],
  ["ul long nhau", "- a\n  - a1\n- b", /<li>a<ul><li>a1<\/li><\/ul><\/li>/],
  ["ol", "1. a\n2. b", /<ol><li>a<\/li><li>b<\/li><\/ol>/],
  ["heading", "# H1\n## H2", /<h1>H1<\/h1><h2>H2<\/h2>/],
  ["fenced code", "```py\nx = 1\n```", /<pre class="md-pre" data-lang="py"><code>x = 1/],
  ["inline code", "dung `npm i`", /<code>npm i<\/code>/],
  ["table canh phai", "| A | B |\n|---|--:|\n| 1 | 2 |", /<th style="text-align:right">B<\/th>/],
  ["blockquote", "> a\n> b", /<blockquote><p>a<br>b<\/p><\/blockquote>/],
  ["link", "[G](https://g.com)", /<a href="https:\/\/g.com" target="_blank"/],
  ["hr", "---", /<hr>/],
  ["del", "~~x~~", /<del>x<\/del>/],
  ["img", "![a](https://e.com/a.png)", /<img class="md-img" alt="a" src="https:\/\/e.com\/a.png">/],
  // Gemini viet moi dong la mot y, khong ghep newline don thanh khoang trang.
  ["giu xuong dong giua doan", "B1: a\nB2: b", /B1: a<br>B2: b/],
];
for (const [name, src, re] of markdown) {
  const out = renderMarkdown(src);
  ok(re.test(out), name, strip(out));
}

console.log("toan hoc (KaTeX)");
{
  // Dung nguyen van ban that Gemini tra ve cho cong thuc khoang cach duong thang.
  const real =
    "Cho diem $M_0(x_0; y_0)$ va duong thang $\\Delta: ax + by + c = 0$[cite: 1]\n\n" +
    "$$d(M_0, \\Delta) = \\frac{|ax_0 + by_0 + c|}{\\sqrt{a^2 + b^2}}$$[cite: 1]";
  const out = renderMarkdown(real);
  ok(/class="katex"/.test(out), "cong thuc that render bang KaTeX");
  reject(/[$]/, "khong con dau $", out);
  reject(/\\frac|\\sqrt/, "khong con lenh LaTeX tho", out);
  reject(/\[cite:/, "khong con [cite: ]", out);
  ok(/class="md-cite"/.test(out), "[cite: 1] thanh chip superscript");
}
{
  const math = [
    ["inline", "$x^2$", true],
    ["display mot dong", "$$E=mc^2$$", true],
    ["display nhieu dong", "$$\n\\frac{a}{b}\n$$", true],
    ["display sau doan van", "Tr:\n$$\nx+1\n$$\nHet", true],
    ["cong thuc trong table", "| $x$ |\n|---|\n| $a_1$ |", true],
    ["cong thuc trong heading", "## $d(M,\\Delta)$", true],
    ["cong thuc trong list", "- $f(x)$", true],
    // Tien te: KHONG duoc coi la cong thuc.
    ["tien te $5 va $10", "Gia la $5 va $10", false],
    ["'a $ b'", "a $ b", false],
    ["duoi dong '100$'", "chi phi 100$", false],
    // dau $ nam trong backtick la text.
    ["code span chua $", "dung `a $ b $ c`", false],
  ];
  for (const [name, src, want] of math) {
    const out = renderMarkdown(src);
    ok(/katex/.test(out) === want, name, strip(out));
  }
}
{
  // LaTeX co & va < (cases/array). Neu escape truoc khi tach math, hai ky tu
  // nay bi bien thanh &amp;/&lt; va KaTeX in ra chuoi do thay vi canh chinh.
  for (const [name, src] of [
    ["cases", "$$\\begin{cases} a & b \\\\ c & d \\end{cases}$$"],
    ["array", "$$\\begin{array}{cc} 1 & 2 \\end{array}$$"],
  ]) {
    const out = renderMarkdown(src);
    ok(/katex/.test(out) && !out.includes("&amp;"), name + ": giu nguyen &", out.slice(0, 120));
  }
}
{
  const bad = renderMarkdown("$$x = \\frac{1}$$");
  ok(bad.length > 0, "LaTeX sai cu phap -> fallback, khong nem exception");
}

console.log("bao mat");
{
  const tagRe = /<([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^<>]*?)?)\/?>/g;
  const attacks = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[a](javascript:alert(1))",
    "![a](javascript:alert(1))",
    "[a](vbscript:x)",
    "[a](JaVaScRiPt:alert(1))",
    "[a](data:text/html,<script>alert(1)</script>)",
    "<iframe src=e>",
    "<style>b{}</style>",
    "**<svg onload=alert(1)>**",
    "> <b onclick=x>hi</b>",
    "| <script>x</script> | y |\n|---|---|\n| z | w |",
    "$\\href{javascript:alert(1)}{x}$",
    "$\\text{<img src=x onerror=alert(1)>}$",
  ];
  for (const a of attacks) {
    const out = renderMarkdown(a);
    const bad2 = [];
    let m;
    tagRe.lastIndex = 0;
    while ((m = tagRe.exec(out))) {
      const tag = m[1].toLowerCase();
      if (/^(script|iframe|object|embed|svg|style|form|input|button|link)$/.test(tag)) {
        bad2.push("tag " + tag);
      }
      if (/\son[a-z]+\s*=/i.test(m[2] || "")) bad2.push("event handler " + tag);
      const href = /\b(?:href|src)\s*=\s*"([^"]*)"/i.exec(m[2] || "");
      if (href && !/^(https?:\/\/|mailto:)/i.test(href[1])) bad2.push("scheme " + href[1]);
    }
    ok(bad2.length === 0, "an toan: " + a.slice(0, 44), bad2.join(", "));
  }
}

console.log("khong treo / khong tran");
{
  const stress = [
    ["ul long sau 6 cap", "- a\n  - b\n    - c\n      - d\n        - e\n          - f"],
    ["moi dong deu thut", "    - x\n    - y\n    - z"],
    ["text 60k ky tu", "a".repeat(60000)],
    ["code block 4000 dong", "```\n" + "x\n".repeat(4000) + "```"],
    ["code span 40k", "`" + "a".repeat(40000) + "`"],
    ["table 60 cot", "|" + "a|".repeat(60) + "\n|" + "-:".repeat(60) + "\n|" + "b|".repeat(60) + "|"],
    ["math 40k", "$" + "a".repeat(40000) + "$"],
    ["display math 2000 dong", "$$" + "\n".repeat(2000) + "$$"],
  ];
  for (const [name, src] of stress) {
    const t0 = Date.now();
    const out = renderMarkdown(src);
    const ms = Date.now() - t0;
    ok(ms < 5000 && typeof out === "string", name, ms + "ms, " + out.length + " chars");
  }
}

console.log("");
if (failed) {
  console.error(failed + " FAIL");
  process.exit(1);
}
console.log("PASS het");