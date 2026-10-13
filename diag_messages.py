# -*- coding: utf-8 -*-
"""只读诊断：导出「AI教学平台」当前 DOM 里的全部消息原文。

用途
----
排查「拿不到题目 / 答非所问」类问题：把聊天里**真实的消息原文**（含换行、选项、
卡片标记）完整落盘，再用 denoise 的函数逐条复算，就能定位是"提取失败"还是
"被判重跳过"。

**全程只读，不发送任何内容。**

运行：
  .venv\\Scripts\\python.exe diag_messages.py
输出：
  messages_dump.json（全部消息）+ 终端打印最后 10 条完整原文
"""

import io
import json
import logging
import sys
from pathlib import Path

sys.argv = ["diag_messages"]          # 避免被 step2 的 argparse 解析
sys.path.insert(0, ".")

# 诊断运行不要写进项目日志
_lg = logging.getLogger("auto")
_lg.handlers = []
_h = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace"))
_h.setFormatter(logging.Formatter("    [step2] %(message)s"))
_lg.addHandler(_h)

from playwright.sync_api import sync_playwright  # noqa: E402
import step2_auto_reply as S  # noqa: E402

OUT = Path("messages_dump.json")


def main():
    with sync_playwright() as p:
        print("启动浏览器 ...", flush=True)
        ctx = S.launch_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("打开飞书 ...", flush=True)
        page.goto(S.FS["base_url"], wait_until="domcontentloaded", timeout=60000)
        if "accounts.feishu" in page.url or "login" in page.url.lower():
            import time as _t
            print("需要登录（请扫码）...", flush=True)
            deadline = _t.time() + 180
            while _t.time() < deadline:
                if "accounts.feishu" not in page.url and "login" not in page.url.lower():
                    break
                _t.sleep(2)
            if "messenger" not in page.url:
                page.goto(S.FS["base_url"], wait_until="domcontentloaded", timeout=60000)

        print("等待会话列表 ...", flush=True)
        if not S.wait_for_chat_list(page):
            print("会话列表加载失败", flush=True)
            ctx.close()
            return 2
        if not S.open_target_chat(page, S.FS["target_chat_name"]):
            print("无法进入目标会话", flush=True)
            ctx.close()
            return 3

        vis = S.snapshot_visible(page, limit=200)
        print(f"\nDOM 条数 = {S.snapshot_visible.last_dom_count} / 读到 {len(vis)} 条", flush=True)

        data = [{"id": m["id"], "is_from_other": m["is_from_other"], "text": m["text"]}
                for m in vis]
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "=" * 72)
        print(f"最后 10 条完整原文（全量已写入 {OUT.name}）")
        print("=" * 72)
        for m in vis[-10:]:
            who = "机器人" if m["is_from_other"] else "我"
            print("-" * 72)
            print(f"[{who}] id=...{m['id'][-8:]}")
            print(m["text"])
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
