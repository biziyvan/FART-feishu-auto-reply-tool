"""
发送能力专项测试（不发真实内容）
================================
只验证 send_reply 的 CDP 输入链路是否可用，以及是否只会发一次。

安全设计：
  - 发送内容为极短的自检文本
  - 发送后立即统计"我发出的消息数"，确认只 +1

运行：
  .venv\\Scripts\\python.exe test_send.py
"""

import sys
import time
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("sendtest")

import step2_auto_reply as S

CFG = S.CFG


def main():
    log.info("=" * 60)
    log.info("  发送能力专项测试")
    log.info("=" * 60)

    with sync_playwright() as p:
        ctx = S.launch_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(CFG["feishu"]["base_url"], wait_until="domcontentloaded", timeout=60000)
        if not S.wait_for_chat_list(page):
            log.error("会话列表加载失败")
            ctx.close()
            sys.exit(1)
        S.open_target_chat(page, CFG["feishu"]["target_chat_name"])
        time.sleep(3)

        # 记录发送前的"我发出的消息数"
        before = S._count_my_messages(page)
        log.info(f"[1] 发送前，我发出的消息数 = {before}")

        # 检查输入框定位
        box = page.locator('[contenteditable="true"]').last
        log.info(f"[2] 输入框数量 = {box.count()}")

        # 测试 CDP 写入（不发送）
        log.info("[3] 测试 CDP insertText 写入 ...")
        S._clear_input_box(page)
        time.sleep(0.3)
        try:
            box.click(timeout=5000)
        except Exception:
            pass
        time.sleep(0.3)
        cdp = page.context.new_cdp_session(page)
        cdp.send("Input.insertText", {"text": "【自检】请忽略"})
        time.sleep(0.5)
        typed = S._read_input_box(page)
        log.info(f"    写入后输入框内容: '{typed}'")
        if typed:
            log.info("    ✓ CDP 写入成功")
        else:
            log.error("    ✗ CDP 写入失败")

        # 清空，不发出去
        time.sleep(0.5)
        S._clear_input_box(page)
        time.sleep(0.5)
        after_clear = S._read_input_box(page)
        log.info(f"[4] 清空后输入框内容: '{after_clear}'")

        after = S._count_my_messages(page)
        log.info(f"[5] 发送后，我发出的消息数 = {after}  （应等于 {before}，本次未发送）")

        # ------------------------------------------------------------
        # [6] ★ 清空可靠性自检（本测试的核心价值）
        # 2026-10-13 他机日志实证：`_clear_input_box` 清不干净时，
        # 残留的旧字母会和下一个答案拼在一起（"B" + "C" = "BC"），
        # 平台只读第一个字母 → **答案对却判错**。
        # 这里连写 5 轮，每轮都校验"清空后真的为空、写入后真的等于目标"。
        # ------------------------------------------------------------
        log.info("[6] 清空可靠性自检（连写 5 轮）...")
        bad = 0
        for i, ch in enumerate(["B", "C", "A", "BB", "C. push()"]):
            S._clear_input_box(page)
            time.sleep(0.25)
            left = S._norm_msg(S._read_input_box(page))
            if left:
                bad += 1
                log.error(f"    第 {i+1} 轮：清空后仍有残留 {left!r}  ← 危险")
                continue
            try:
                box.click(timeout=5000)
            except Exception:
                pass
            time.sleep(0.2)
            cdp.send("Input.insertText", {"text": ch})
            time.sleep(0.35)
            got = S._norm_msg(S._read_input_box(page))
            want = S._norm_msg(ch)
            if got != want:
                bad += 1
                log.error(f"    第 {i+1} 轮：写入 {ch!r} 得到 {got!r}  ← 内容被拼接了")
            else:
                log.info(f"    第 {i+1} 轮 ✓ 清空干净、写入准确（{ch!r}）")
        S._clear_input_box(page)
        time.sleep(0.3)
        log.info(f"    清空可靠性：{5 - bad}/5 通过")
        if bad:
            log.error("    ✗ 本机富文本清空不可靠 → 已由 send_reply 的内容校验兜住"
                      "（不一致就重清，仍不一致则放弃发送，绝不发拼接内容）")
        else:
            log.info("    ✓ 本机清空稳定")

        log.info("=" * 60)
        log.info("测试完成（未发送任何真实消息）")
        ctx.close()


if __name__ == "__main__":
    main()
