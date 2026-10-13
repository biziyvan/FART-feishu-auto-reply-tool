# -*- coding: utf-8 -*-
"""发送链路防错回归测试（不需要网络/浏览器）

背景（2026-10-13 他机日志实证）：
  1) 成功判据"我方消息条数 +1"在虚拟滚动下会误判 → 明明发出去了却当"发送失败"
     → 触发兜底重打 → **同一题重复回答**；
  2) 兜底重打前靠 `_clear_input_box` 清空 Lark 富文本，**清不干净**时
     残留的旧字母会和新答案拼在一起（残留 "B" + 新答案 "C" = "BC"）
     → 平台只读第一个字母 → **答案是对的却判错**。

覆盖：
  · `_norm_msg`   文本归一（去空白/零宽字符）
  · `_after_send_action`  未确认发出时的三分支决策（ok / resend / abort）

运行：
  .venv\\Scripts\\python.exe test_send_guard.py
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.argv = ["test_send_guard"]

sys.path.insert(0, ".")

import step2_auto_reply as S  # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}\n      期望 {want!r}\n      实际 {got!r}")


CODE = "contract A {\n    uint x;\n}\n这行存储 x。"


def main():
    global PASS, FAIL
    print("=" * 62)
    print("  发送链路防错 回归测试")
    print("=" * 62)

    print("\n[1] _norm_msg：只去空白/零宽字符，不动内容")
    check("普通字母", S._norm_msg("B"), "B")
    check("含首尾空白与换行", S._norm_msg("  B\n"), "B")
    check("含 Lark 零宽空格", S._norm_msg("B\u200b"), "B")
    check("多行代码题", S._norm_msg(CODE), "contractA{uintx;}这行存储x。")
    check("空/None 安全", (S._norm_msg(""), S._norm_msg(None)), ("", ""))
    check("不同内容不会被压成相同",
          S._norm_msg("BC") == S._norm_msg("CB"), False)

    print("\n[2] 已确认发出 → 一律 ok（不再走任何重发路径）")
    check("已确认 + 框里有内容", S._after_send_action("B", "B", True), "ok")
    check("已确认 + 框是空的", S._after_send_action("", "B", True), "ok")

    print("\n[3] ★ 未确认发出 + 输入框已清空 → ok（绝不重打，防重复回答）")
    check("框空", S._after_send_action("", "B", False), "ok")
    check("框里只剩零宽空格", S._after_send_action("\u200b", "B", False), "ok")
    check("框空（多行代码题）", S._after_send_action("", CODE, False), "ok")

    print("\n[4] 未确认发出 + 框里还是原文 → resend（确实没发出去，可重试）")
    check("单字母", S._after_send_action("C", "C", False), "resend")
    check("带换行的原文", S._after_send_action(CODE, CODE, False), "resend")

    print("\n[5] ★ 未确认发出 + 框里是拼接/异常内容 → abort（宁可漏发，绝不发错）")
    # 他机实测：残留 "B" + 新答案 "C" = "BC"，平台读成 B
    check("残留 B + 新答案 C", S._after_send_action("BC", "C", False), "abort")
    # 反向：残留 C + 新答案 B
    check("残留 C + 新答案 B", S._after_send_action("CB", "B", False), "abort")
    # 残留 B + 新答案 A（第27题实测："BA" 被判成 B，"回答部分正确"）
    check("残留 B + 新答案 A", S._after_send_action("BA", "A", False), "abort")
    # 内容被截断
    check("框里内容比预期短", S._after_send_action("A", "AB", False), "abort")
    # 完全无关的内容
    check("框里是别的题的内容",
          S._after_send_action("zombies.push(x);", "B", False), "abort")

    print("\n[6] 真机日志回归：他机 17:39 那次的三种情形")
    check("第35题发 B 后框空（判定已发出）",
          S._after_send_action("", "B", False), "ok")
    check("第15题想发 C、框里却是 BC",
          S._after_send_action("BC", "C", False), "abort")
    check("重试时框里仍是 C",
          S._after_send_action("C", "C", False), "resend")

    print("\n" + "=" * 62)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
