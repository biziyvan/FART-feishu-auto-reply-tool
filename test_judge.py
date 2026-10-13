"""判断题专项自检

A. 单元测试 normalize_true_false（不联网）——确认中文/符号/大小写都能归一
B. 实模型测试——用真实卡片格式问一道判断题，确认输出被归一为 true / false

运行：.venv\\Scripts\\python.exe test_judge.py
"""

import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import step2_auto_reply as S  # noqa: E402

# (输入, is_judge, 期望输出)
UNIT = [
    ("错误", False, "false"),
    ("正确", False, "true"),
    ("错误。", False, "false"),
    ("答案：错误", False, "false"),
    ("False", False, "false"),
    ("True", False, "true"),
    ("false", False, "false"),
    ("no", False, "false"),
    ("yes", False, "true"),
    ("对", True, "true"),
    ("错", True, "false"),
    ("F", True, "false"),
    ("T", True, "true"),
    ("√", True, "true"),
    ("×", True, "false"),
    ("F。", True, "false"),
    ("对", False, "对"),
    ("错", False, "错"),
    ("正确性很高", False, "正确性很高"),
    ("这是一大段解释，不是判断题。", False, "这是一大段解释，不是判断题。"),
    ("", False, ""),
]

# (题干, 期望答案, 平台给的考点提示)
JUDGE_CASES = [
    ("判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果", "false", "F"),
    ("判断：Solidity 中的状态变量会被永久保存在区块链上，函数执行结束后依然存在", "true", "T"),
]


def build_card(question, hint="", course="智能合约技术"):
    body = question + (f"\n🪜 考点提示：{hint}" if hint else "")
    return (
        f"以下是我和教学机器人「AI教学平台」的最近对话：\n\n"
        f"[机器人] AI 自适应问答\n📚 正在学习：{course}\n第 1 题（掌握度 0%）\n"
        f"{body}\n💡 直接回复你的答案\n\n"
        f"↑↑↑ 上面这一条是机器人【最新】发来的消息，需要回复它。\n\n"
        f"请针对上面【最新】的这道题，给出我下一条应该发送给机器人的回复内容。"
    )


def main():
    print("=" * 74)
    print("A. 单元测试：normalize_true_false（不联网）")
    print("=" * 74)
    bad = 0
    for src, is_j, want in UNIT:
        got = S.normalize_true_false(src, is_judge=is_j)
        ok = got == want
        bad += (not ok)
        print(f"  {'✓' if ok else '✗'} {src!r:<34} is_judge={str(is_j):<5} → {got!r}"
              + ("" if ok else f"   期望 {want!r}"))
    print(f"  —— 单元测试: {'全部通过' if not bad else f'{bad} 项失败'}")

    print("\n" + "=" * 74)
    print(f"B. 实模型测试（模型: {S.LLM['model']}）")
    print("=" * 74)
    ok = 0
    for q, want, hint in JUDGE_CASES:
        t = time.time()
        raw = (S.ask_llm(build_card(q, hint)) or "").strip()
        dt = time.time() - t
        final = S.normalize_true_false(raw, is_judge=True)
        good = final == want
        ok += good
        print(f"\n  题: {q[:52]}…  期望 {want}")
        print(f"    模型原始输出: {raw!r}")
        print(f"    归一化后发送: {final!r}   ({dt:.1f}s)  {'✓' if good else '✗ 不符'}")
    print("\n" + "=" * 74)
    print(f"实模型: 归一化正确 {ok}/{len(JUDGE_CASES)}")
    print("=" * 74)


if __name__ == "__main__":
    main()
