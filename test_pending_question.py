# -*- coding: utf-8 -*-
"""选题逻辑回归测试（不需要网络/模型/浏览器）

覆盖「学习反馈之后拿不到新题」这一线上故障：
  根因是 denoise._fp_match 里一条 "core[:16] 相同即同题" 的模糊规则，
  把**题号相同、开头 16 字相同但内容完全不同**的两道题误判为同一题，
  于是 find_pending_question 在 latest_only 模式下返回"无新题"，程序持续待命。

运行：
  .venv\\Scripts\\python.exe test_pending_question.py
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

import denoise as D  # noqa: E402

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


# ---------------------------------------------------------------
# 真实原文（取自线上飞书会话，未做任何改写）
# ---------------------------------------------------------------
# 触发故障的那条反馈卡：夹带「第 10 题」
FEEDBACK_Q10 = """学习反馈
📚 正在学习：智能合约技术

✅ 答对了

💡 回答完全正确！你准确指出了用户定义值类型的核心特性：基于现有类型创建新类型以增强类型安全（编译期严格检查），并且强调了它是零成本抽象（无额外运行时开销）。理解非常到位！

掌握度: 42%

第 10 题

在 Solidity 智能合约中，若要实现“通过用户的以太坊地址查询其拥有的僵尸数量”的功能，以下哪种变量声明方式是正确的？

A. A. mapping (uint => address) public ownerZombieCount;
B. B. mapping (address => uint) public ownerZombieCount;
C. C. address[] public ownerZombieCount;
D. D. struct ZombieCount { address owner; uint count; }

💡 直接回复选项字母作答

⚠️ 需切换课程，直接发新的课程名（如「密码学基础」）"""

# 历史上已答过的**另一道**第 10 题（开头 16 字与上面完全相同！）
OLD_Q10_FP = "10|在Solidity智能合约中若要限制用户创建僵尸时必须同时满足当前没有僵尸own"

# 另一道题号相同、开头也相同的旧题
OLD_Q10_FP2 = "10|在Solidity中关于整型如uint和int的特性与使用以下描述正确的是哪一项"

# 判断题：反馈卡形态（带 🪜 考点提示）
FB_JUDGE = """学习反馈
📚 正在学习：智能合约技术

✅ 答对了

💡 解析略

掌握度: 12%

第 5 题

判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果

🪜 考点提示：F

💡 直接回复选项字母作答"""

# 判断题：提问卡形态（同一题，没有尾注）
Q_JUDGE = """AI 自适应问答
📚 正在学习：智能合约技术

第 5 题

判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果

💡 直接回复选项字母作答"""

# 第③种推送形态：无题号，形如「请直接回复选项作答 / 当前题目：…」（线上原文）
FORM_C = """请直接回复选项作答
当前题目：
判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果

回复 A / B / C / D（或选项内容）即可作答。
不明白题目意思？回复「提示」看考点；想结束练习回复「退出」。"""

# 「请输入课程名」提示：线上真实的学习提醒（含同样话术）
COURSE_REMINDER = """学习提醒
⏰ 你已经 8 小时没有答题啦！
💕 学累了记得休息，休息好了先来做道题好不好？
坚持每天练习才能巩固知识点哦～
直接回复课程名（如「智能合约」）开始练习。
（回复「暂停推送」可关闭每日提醒）"""

# 用户描述的另一种课程名提示措辞
COURSE_HINT_USER = """如要开始学习，请输入课程名称，如[智能合约]"""


def mk(mid, text, other=True):
    return {"id": str(mid), "is_from_other": other, "text": text}


def main():
    print("=" * 62)
    print("  选题逻辑回归测试（学习反馈 → 新题）")
    print("=" * 62)

    # ---------- 1. 提取能力：反馈卡必须能抽出夹带的新题 ----------
    print("\n[1] 反馈卡提取新题")
    qnum, body = D.extract_pending_question(D.clean_text(FEEDBACK_Q10))
    check("题号 = 10", qnum, 10)
    check("题干含题干文字", "以太坊地址查询其拥有的僵尸数量" in (body or ""), True)
    check("题干保留选项 A", "A. mapping (uint => address)" in (body or ""), True)
    check("题干保留选项 D", "D. struct ZombieCount" in (body or ""), True)
    check("选项双前缀已规范化(A. A. → A.)", "A. A." not in (body or ""), True)

    # ---------- 2. ★回归：开头相同的不同题**不能**被判为同题 ----------
    print("\n[2] ★回归：同题号 + 同开头 16 字，但内容不同 → 必须视为【新题】")
    fp_new = D.question_fingerprint(D.clean_text(FEEDBACK_Q10))
    check("新题指纹题号", fp_new[0], 10)
    check("新题 core 前 16 字与旧题相同（这是触发条件）",
          fp_new[1][:16] == "在Solidity智能合约中若要", True)
    check("_fp_match vs 旧的同开头第10题 → False（关键修复点）",
          D._fp_match(fp_new, {OLD_Q10_FP}), False)
    check("_fp_match vs 另一旧的同题号题 → False",
          D._fp_match(fp_new, {OLD_Q10_FP2}), False)
    check("_fp_match vs 两者的并集 → False",
          D._fp_match(fp_new, {OLD_Q10_FP, OLD_Q10_FP2}), False)

    # ---------- 3. 判重能力不能被削弱 ----------
    print("\n[3] 判重仍有效：已答过的原题 → True")
    check("完全相同的指纹 → True",
          D._fp_match(fp_new, {D.fp_to_str(fp_new)}), True)
    fp_q = D.question_fingerprint(D.clean_text(Q_JUDGE))
    fp_fb = D.question_fingerprint(D.clean_text(FB_JUDGE))
    check("判断题：提问卡 vs 反馈卡（带考点提示）→ 同题",
          D._fp_match(fp_q, {D.fp_to_str(fp_fb)}), True)
    check("判断题：反馈卡 vs 提问卡 → 同题（反向）",
          D._fp_match(fp_fb, {D.fp_to_str(fp_q)}), True)
    check("题号不同则不算同题",
          D._fp_match((9, fp_q[1]), {D.fp_to_str(fp_fb)}), False)

    # ---------- 4. 端到端：find_pending_question 必须选中新题 ----------
    print("\n[4] 端到端：find_pending_question 应选中第 10 题")
    msgs = [
        mk(7001, "学习反馈\n📚 正在学习：智能合约技术\n\n✅ 答对了\n\n💡 略\n\n掌握度: 12%\n\n第 8 题\n\n在Solidity中，`keccak256` 函数会将字符串转换为一个 256 位的哈希值。\n\nA. bytes16\nB. bytes32\nC. bytes\nD. string\n\n💡 直接回复选项字母作答"),
        mk(7002, "B", other=False),
        mk(7003, "学习反馈\n📚 正在学习：智能合约技术\n\n✅ 答对了\n\n💡 略\n\n掌握度: 27%\n\n第 9 题\n\n在资料中，我们使用 `uint` 类型来存储僵尸的 DNA，关于用户定义值类型以下理解正确的是？\n\nA. 甲\nB. 乙\nC. 丙\nD. 丁\n\n💡 直接回复选项字母作答"),
        mk(7004, "C", other=False),
        mk(7005, FEEDBACK_Q10),
    ]
    answered = {
        "8|在Solidity中keccak256函数会将字符串转换为一个256位的哈希值",
        "9|在资料中我们使用uint类型来存储僵尸的DNA关于用户定义值类型以下理解正确的是",
        OLD_Q10_FP,      # ← 正是这条历史记录把新第 10 题挡住了
    }
    trig, qn, bd = D.find_pending_question(msgs, answered, latest_only=True)
    check("找到触发消息", trig is not None, True)
    check("触发的是最新那条", (trig or {}).get("id"), "7005")
    check("题号 = 10", qn, 10)
    check("题干正确", "以太坊地址查询其拥有的僵尸数量" in (bd or ""), True)

    # ---------- 5. latest_only 语义保持 ----------
    print("\n[5] latest_only：最新那条确实已答过 → 不回头补答")
    trig2, qn2, _ = D.find_pending_question(
        msgs, answered | {D.fp_to_str(fp_new)}, latest_only=True
    )
    check("返回 None（等待机器人推新题）", trig2, None)

    # ---------- 6. 第③种推送形态「当前题目：」（无题号）----------
    print("\n[6] 第③种推送形态「请直接回复选项作答 / 当前题目：」（无题号）")
    check("classify → question", D.classify(D.clean_text(FORM_C)), "question")
    q3, b3 = D.extract_pending_question(D.clean_text(FORM_C))
    check("题号 = None（该形态不带题号）", q3, None)
    check("题干正确", b3 == "判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果", True)
    check("尾部操作提示已剔除", "回复 A / B / C / D" not in (b3 or ""), True)
    fp_c = D.question_fingerprint(D.clean_text(FORM_C))
    check("指纹题号 = None", fp_c[0], None)
    check("重复推送同一题 → 判为已答（关键：避免无限重复作答）",
          D._fp_match(fp_c, {D.fp_to_str(fp_c)}), True)
    check("跨形态：已答①形态(第5题) → 再遇③形态 → 判为已答",
          D._fp_match(fp_c, {D.fp_to_str(fp_q)}), True)
    check("跨形态：已答③形态 → 再遇①形态 → 判为已答",
          D._fp_match(fp_q, {D.fp_to_str(fp_c)}), True)
    check("负例：③形态的另一道判断题 → 不算同题",
          D._fp_match(D.question_fingerprint(
              "请直接回复选项作答\n当前题目：\n判断：`storage` 变量在函数结束后会被清除\n\n回复 A / B / C / D（或选项内容）即可作答。"
          ), {D.fp_to_str(fp_c)}), False)

    print("\n[6b] 端到端：序列里只有③形态的题 → 必须选中")
    msgs_c = [
        mk(8001, "F", other=False),
        mk(8002, FORM_C),
    ]
    trig3, qn3, bd3 = D.find_pending_question(msgs_c, set(), latest_only=True)
    check("找到触发消息", trig3 is not None, True)
    check("触发的是③形态那条", (trig3 or {}).get("id"), "8002")
    check("题号 = None", qn3, None)
    check("题干正确", (bd3 or "").startswith("判断："), True)
    trig4, _, _ = D.find_pending_question(msgs_c, {D.fp_to_str(fp_c)}, latest_only=True)
    check("答过之后 → 回到待命", trig4, None)

    # ---------- 7. 「请输入课程名」提示识别 ----------
    print("\n[7] 「请输入课程名」提示识别（用于自动回复课程名开课）")
    check("学习提醒（含「直接回复课程名…开始练习」）→ True",
          D.is_course_prompt(COURSE_REMINDER), True)
    check("用户描述的形态「如要开始学习…输入课程名称…」→ True",
          D.is_course_prompt(COURSE_HINT_USER), True)
    check("classify(学习提醒) → reminder", D.classify(D.clean_text(COURSE_REMINDER)), "reminder")
    check("学习反馈尾部「需切换课程，直接发新的课程名」→ False（关键：不能误回课程名）",
          D.is_course_prompt(FEEDBACK_Q10), False)
    check("第③形态的题目 → False", D.is_course_prompt(FORM_C), False)
    check("普通选择题反馈 → False", D.is_course_prompt(Q_JUDGE), False)

    print("\n" + "=" * 62)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
