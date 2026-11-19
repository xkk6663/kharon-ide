"""把 src/*.html 生成 src/l1/pages.h（C 字符串数组）。

为什么不再手写 C 里的 HTML：一个页面里同时有 HTML 属性引号、JS 字符串引号、
JS 里再用引号拼 HTML，三层叠在一起时 C 的 \" 转义极易写错（实测把 JS 字符串
截断成半句，编译还照样过）。这里让机器做转义，人只维护真 HTML。

输出位置跟代码分层走：`pages.h` 是 l1（接口层）的资源头，`page.c` 用 `#include "pages.h"`
取它。**不要把输出放回 src 根**（`src/`）——根目录也在 `-I` 里，会在那里留一份同名旧文件，
把 l1 里的正式副本遮住，表现是"改了页面却不生效"。

用法：
  python tools/gen_pages.py            # 生成 src/l1/pages.h
  python tools/gen_pages.py --check    # 只校验，不写文件（CI/构建前用）
"""
from __future__ import annotations

import argparse
import pathlib
import sys

SRC_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps" / "esp32s3" / "s3_wifi_portal" / "src"
OUT = SRC_DIR / "l1" / "pages.h"

PAGES = [("user_html", "user_page.html")]


def c_literal(text: str) -> str:
    """把一段文本变成 C 字符串字面量序列（每行一个，便于 diff）。"""
    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        esc = line.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")
        esc = esc.replace("\n", "\\n").replace("\t", "\\t")
        out.append('\t"%s"' % esc)
    return "\n".join(out)


def build() -> str:
    parts = [
        "/* 由 tools/gen_pages.py 从 src/*.html 生成，不要手改这里。 */",
        "#ifndef PORTAL_PAGES_H_",
        "#define PORTAL_PAGES_H_",
        "",
    ]
    for name, fname in PAGES:
        text = (SRC_DIR / fname).read_text(encoding="utf-8")
        parts.append("/* %s（%d 字节） */" % (fname, len(text.encode("utf-8"))))
        parts.append("static const char %s[] =" % name)
        parts.append(c_literal(text))
        parts.append(";")
        parts.append("")
    parts.append("#endif /* PORTAL_PAGES_H_ */")
    parts.append("")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("pages.h is stale, run: python tools/gen_pages.py", file=sys.stderr)
            return 1
        print("pages.h up to date")
        return 0

    OUT.write_text(text, encoding="utf-8")
    print("wrote %s (%d lines)" % (OUT, text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
