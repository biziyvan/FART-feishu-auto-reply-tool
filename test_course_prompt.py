# -*- coding: utf-8 -*-
"""「请输入课程名」自动开课 回归测试（不需要网络/浏览器）

覆盖 step2_auto_reply._maybe_reply_course：
  · 识别"请你输入课程名"的提示（无题目）→ 自动回复课程名开课
  · 同一条提示重复推送 / 冷却期内 不重复回复
  · 答完一题后重新武装（下一轮仍可回复）
  · 「学习反馈」尾部那句"需切换课程，直接发新的课程名"**绝不能**触发
  · auto_reply_course 置空 = 关闭该功能

运行：
  .venv\\Scripts\\python.exe test_course_prompt.py
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.argv = ["test_course_prompt"]

sys.path.insert(0, ".")

import step2_auto_reply as S  # noqa: E402

# ---- 隔离：不真正发送、不写真实状态文件 ----
SENT = []
S.send_reply = lambda page, text: (SENT.append(text) or True)
S.save_state = lambda state: None      # 关键：绝不动真实 answered.json
S.ARGS.dry_run = False                 # 走"真实发送"分支，但 send_reply 已被替换
S.REPLY["send_delay_min"] = 0          # 免等待
S.REPLY["send_delay_max"] = 0

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


REMINDER = """学习提醒
⏰ 你已经 8 小时没有答题啦！
💕 学累了记得休息，休息好了先来做道题好不好？
坚持每天练习巩固知识点哦～
直接回复课程名（如「智能合约」）开始练习。
（回复「暂停推送」可关闭每日提醒）"""

HINT_USER = """如要开始学习，请输入课程名称，如[智能合约]"""

FEEDBACK = """学习反馈
📚 正在学习：智能合约技术

✅ 答对了

💡 略

掌握度: 42%

第 10 题

在 Solidity 智能合约中，若要实现查询僵尸数量，以下哪种声明正确？

A. a
B. b

💡 直接回复选项字母作答

⚠️ 需切换课程，直接发新的课程名（如「密码学基础」）"""

FORM_C = """请直接回复选项作答
当前题目：
判断：「智能合约基本概念」在实际应用中可以随意省略，不影响整体效果

回复 A / B / C / D（或选项内容）即可作答。"""


def mk(mid, text):
    return {"id": str(mid), "is_from_other": True, "text": text}


def fresh_state():
    return {"answered": []}


def main():
    global PASS, FAIL
    print("=" * 62)
    print("  「请输入课程名」自动开课 回归测试")
    print("=" * 62)
    print(f"  auto_reply_course = {S.AUTO_COURSE!r}  cooldown = {S.COURSE_COOLDOWN}s")

    original_course = S.AUTO_COURSE

    print("\n[1] 首次遇到课程名提示 → 自动回复课程名")
    st = fresh_state()
    m1 = [mk(100, REMINDER)]
    SENT.clear()
    check("返回 True（已处理）", S._maybe_reply_course(m1, st, None), True)
    check("发送内容 = 智能合约", SENT[-1] if SENT else None, "智能合约")
    check("已记录去重指纹", len(st.get("course_replied_fps", [])), 1)
    check("已记录时间戳", bool(st.get("course_last_ts")), True)

    print("\n[2] 重复推送 / 冷却期 → 不重复回复")
    check("同一条再推 → False", S._maybe_reply_course(m1, st, None), False)
    check("同内容新 id → False（内容去重）",
          S._maybe_reply_course([mk(101, REMINDER)], st, None), False)
    st2 = {"answered": [], "course_last_ts": time.time()}
    check("冷却期内遇到新措辞 → False",
          S._maybe_reply_course([mk(200, HINT_USER)], st2, None), False)

    print("\n[3] 答完一题后重新武装 → 同一提示仍可回复")
    st3 = fresh_state()
    check("第一次 → True", S._maybe_reply_course([mk(300, REMINDER)], st3, None), True)
    check("未重新武装前 → False", S._maybe_reply_course([mk(300, REMINDER)], st3, None), False)
    st3["course_replied_fps"] = []      # 模拟"答完一题"后的重新武装
    st3["course_last_ts"] = 0
    check("重新武装后 → True",
          S._maybe_reply_course([mk(300, REMINDER)], st3, None), True)

    print("\n[4] 不该触发的情况")
    check("学习反馈尾部「需切换课程…」→ False",
          S._maybe_reply_course([mk(400, FEEDBACK)], fresh_state(), None), False)
    check("第③形态的题目 → False",
          S._maybe_reply_course([mk(401, FORM_C)], fresh_state(), None), False)
    check("没有机器人新消息（最新是我发的）→ False",
          S._maybe_reply_course([{"id": "402", "is_from_other": False, "text": "B"}],
                                fresh_state(), None), False)
    check("消息为空 → False", S._maybe_reply_course([], fresh_state(), None), False)

    print("\n[5] 用户自填的另一种措辞也能识别")
    check("「如要开始学习，请输入课程名称，如[智能合约]」→ True",
          S._maybe_reply_course([mk(500, HINT_USER)], fresh_state(), None), True)

    print("\n[6] auto_reply_course 置空 = 关闭功能")
    S.AUTO_COURSE = ""
    check("置空后 → False", S._maybe_reply_course([mk(600, REMINDER)], fresh_state(), None), False)
    S.AUTO_COURSE = original_course

    print("\n" + "=" * 62)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
