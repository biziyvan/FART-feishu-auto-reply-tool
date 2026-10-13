"""
抓取并预处理历史对话
====================
用途：
  偶尔手动运行一次，把「AI教学平台」的完整对话历史抓下来，
  去噪后保存为 history.json，供主程序 step2 作为上下文使用。
  这样主程序日常运行**无需反复滚动翻页**。

运行：
  .venv\\Scripts\\python.exe fetch_history.py

参数：
  --scroll 30   向上滚动的轮数（默认 25，越大抓得越多）
"""

import io
import os
import sys
import time
import json
import shutil
import argparse
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from denoise import clean_text, classify, denoise, sort_by_time
from runtime import DATA_DIR


def _init_stdout():
    """控制台编码兜底。

    本脚本的输出含 ⚠ / ❌ 等符号，而 Windows 控制台默认是 cp936（GBK），
    这些字符不在 GBK 码表里 → print 直接抛 UnicodeEncodeError 崩掉。
    打包成窗口程序时 stdout 还可能是 None。统一换成 UTF-8 + 容错流。
    """
    try:
        out = sys.stdout
        if out is None:
            out = open(os.devnull, "w", encoding="utf-8")
        enc = (getattr(out, "encoding", "") or "").lower()
        if enc not in ("utf-8", "utf8"):
            out = io.TextIOWrapper(out.buffer, encoding="utf-8",
                                   errors="replace", line_buffering=True)
        sys.stdout = out
    except Exception:
        pass


_init_stdout()


BASE_DIR = DATA_DIR
HISTORY_FILE = BASE_DIR / "history.json"
RAW_FILE = BASE_DIR / "history_raw.json"

# 消息滚动容器候选选择器（页面改版时按顺序试）
SCROLL_SELECTORS = (".messageList", ".chatMessages", ".messageContainer")


def _atomic_write_json(path, obj):
    """原子写 JSON：先写 .tmp → 备份旧文件 .bak → 原子替换。

    非原子写（先截断再写）在断电/崩溃时会留下 0 字节或半截 JSON，
    而读取侧解析失败会静默回退成"空历史" → 上下文整体失真且不可自愈。
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        try:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        except Exception:
            pass
    os.replace(tmp, path)


def log(*a):
    print(*a, flush=True)


from settings import load_config  # noqa: E402

CFG = load_config()
FS = CFG["feishu"]
BR = CFG["browser"]


def snapshot(page):
    """抓当前 DOM 里的全部消息。"""
    out = {}
    try:
        items = page.locator(".js-message-item")
        for i in range(items.count()):
            el = items.nth(i)
            try:
                mid = el.get_attribute("id") or ""
                cls = el.get_attribute("class") or ""
                text = el.inner_text().strip()
                if mid and text:
                    out[mid] = {
                        "id": mid,
                        "is_from_other": "message-not-self" in cls,
                        "text": text,
                    }
            except Exception:
                continue
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scroll", type=int, default=25, help="向上滚动轮数")
    args = ap.parse_args()

    log("=" * 62)
    log("  抓取历史对话（去噪后供主程序使用）")
    log("=" * 62)

    acc = {}
    container_ok = False
    ok = True

    with sync_playwright() as p:
        ctx = None
        try:
            kwargs = dict(
                user_data_dir=str(BASE_DIR / BR["user_data_dir"]),
                headless=BR["headless"],
                slow_mo=BR["slow_mo"],
                viewport={"width": BR["viewport_width"], "height": BR["viewport_height"]},
                # Edge 155+ 必须带"跳过兼容层重启"，否则启动即秒退（TargetClosedError）
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                      "--edge-skip-compat-layer-relaunch"],
            )
            if BR.get("channel"):
                kwargs["channel"] = BR["channel"]
            ctx = p.chromium.launch_persistent_context(**kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.goto(FS["base_url"], wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                time.sleep(2)
                try:
                    loc = page.locator(".list_items")
                    if loc.count() > 0 and len(loc.first.inner_text()) > 20:
                        break
                except Exception:
                    pass
            log("会话列表已加载")

            page.locator(f'.list_items >> text="{FS["target_chat_name"]}"').first.click(timeout=8000)
            time.sleep(4)
            log("已进入会话")

            acc.update(snapshot(page))
            log(f"当前可见 {len(acc)} 条")

            try:
                page.mouse.move(BR["viewport_width"] // 2, BR["viewport_height"] // 2)
            except Exception:
                pass

            # 终止判据用「条目数不再增加」+「最新一条 id 不再变化」双条件，
            # 避免页面在滚动时空转（DOM 条数溢出回收）被误判为"到底"。
            stable = 0
            last_newest = 0
            for i in range(args.scroll):
                try:
                    found = page.evaluate(
                        """(sels) => {
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (el) return s;
                            }
                            return '';
                        }""",
                        list(SCROLL_SELECTORS),
                    )
                    if found:
                        container_ok = True
                        page.evaluate(
                            "(s) => { const el = document.querySelector(s); if (el) el.scrollTop = 0; }",
                            found,
                        )
                    page.mouse.wheel(0, -2500)
                except Exception:
                    pass
                time.sleep(0.8)
                before = len(acc)
                acc.update(snapshot(page))
                newest = max((int(k) for k in acc if str(k).isdigit()), default=0)
                if len(acc) == before and newest == last_newest:
                    stable += 1
                    if stable >= 3:
                        log(f"  第 {i+1} 轮无新增，停止")
                        break
                else:
                    stable = 0
                    log(f"  第 {i+1} 轮: 累计 {len(acc)} 条")
                last_newest = newest

            # 滚回底部
            try:
                page.evaluate(
                    """(sels) => {
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (el) { el.scrollTop = el.scrollHeight; return; }
                        }
                    }""",
                    list(SCROLL_SELECTORS),
                )
                time.sleep(1)
            except Exception:
                pass
        except Exception as e:
            ok = False
            log(f"抓取过程出错：{str(e).splitlines()[0][:200]}")
        finally:
            # 关闭失败绝不能让控制流跳过落盘（原实现里 ctx.close() 一抛就全丢）
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass

    if not ok:
        log("本次抓取未完成 —— 已保留原有 history.json 不变（避免用半截数据覆盖好数据）。")
        sys.exit(2)

    if not container_ok:
        log("⚠ 未找到消息滚动容器（试过 .messageList/.chatMessages/.messageContainer）。")
        log("  页面结构可能已改版，本轮可能只抓到当前可见的少量消息。")

    # 统一用项目自己的时间序（雪花 ID），避免非数字 id 被排到最前
    msgs = sort_by_time(acc.values())
    log(f"\n共抓取 {len(msgs)} 条原始消息")

    if not msgs:
        log("没有抓到任何消息，终止（保留原有 history.json）。")
        sys.exit(3)

    # 保存原始
    _atomic_write_json(RAW_FILE, msgs)

    # 去噪
    # ⚠️ 关键：**不做文本去重**（dedup=False）。
    #    原实现 dedup=True 会"保留首次出现"，而 `key` 只按文本、不含方向 ——
    #    我发的 "B" 与机器人发的 "B" 会被判成同一条而丢掉一条；
    #    同一道题被机器人重复推送时也会只留最早那条，导致题目链断裂。
    #    而下游 build_context 用的是 dedup_keep_last（保留最新），方向恰好相反。
    #    历史文件应"全量保留"，去重交给下游按需做。
    cleaned = denoise(msgs, drop_reminders=False, dedup=False)

    # 写入自检：题目数不得因清洗而减少
    n_q_raw = sum(1 for m in msgs if "自适应问答" in (m.get("text") or ""))
    n_q_out = sum(1 for m in cleaned if "自适应问答" in (m.get("text") or ""))
    if n_q_out < n_q_raw:
        log(f"⚠ 清洗后题目数减少：{n_q_raw} → {n_q_out}（可能被去重折叠，请反馈）")

    # 统计类型
    from collections import Counter
    types = Counter(m["type"] for m in cleaned)
    log("消息类型统计:")
    for t, c in types.most_common():
        log(f"  {t}: {c}")

    _atomic_write_json(HISTORY_FILE, cleaned)
    log(f"\n去噪后 {len(cleaned)} 条（题目 {n_q_out} 道），已保存到 {HISTORY_FILE.name}")
    log(f"原始数据保存在 {RAW_FILE.name}（各自另存有 .bak 备份）")

    # 展示最近几条
    log("\n最近 8 条（去噪后）:")
    for m in cleaned[-8:]:
        who = "机器人" if m["is_from_other"] else "我"
        log(f"  [{who}][{m['type']}] {m['text'][:80].replace(chr(10), ' / ')}")

    log("\n完成。现在可以运行 step2_auto_reply.py")
    if not container_ok:
        sys.exit(2)


if __name__ == "__main__":
    main()
