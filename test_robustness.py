# -*- coding: utf-8 -*-
"""稳健性回归测试（不需要网络 / 模型 / 浏览器）

覆盖本轮针对《优化清单》所做的修复：
  B1  history 全量保留（去重方向与下游一致，不再静默丢题）
  M1  fetch_history / 状态文件原子写 + .bak
  M2  answered.json 原子写 + 损坏时回退 .bak
  M10 reply_stats 有上界
  M11 判重跳过可观测（find_pending_question 的 on_skip 回调）
  M13 日志带日期 + 与 launcher 的 LOG_RE 兼容
  S1  发送失败按题熔断（纯判定函数）
  S6  step1_check_login 模块级不再 NameError（静态检查）

运行：
  .venv\\Scripts\\python.exe test_robustness.py
"""

import os
import sys
import json
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

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


def ok(name, cond, detail=""):
    """断言为真；失败时打印 detail 便于定位。"""
    global PASS, FAIL
    if bool(cond):
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      ↳ {detail}" if detail else ""))


import denoise as D            # noqa: E402
import fetch_history as FH     # noqa: E402
import step2_auto_reply as S   # noqa: E402


# ===============================================================
# 1. 原子写 + .bak 回退
# ===============================================================
print("\n[1] 原子写 / 备份回退")

tmp = Path(tempfile.mkdtemp(prefix="feishu_rob_"))
p = tmp / "state.json"

S._atomic_write_json(p, {"v": 1})
check("首次写入内容正确", json.loads(p.read_text(encoding="utf-8")), {"v": 1})
check("首次写入不产生 .bak", (tmp / "state.json.bak").exists(), False)

S._atomic_write_json(p, {"v": 2})
check("二次写入内容正确", json.loads(p.read_text(encoding="utf-8")), {"v": 2})
check(".bak 保存上一版", json.loads((tmp / "state.json.bak").read_text(encoding="utf-8")),
      {"v": 1})
check("不残留 .tmp 临时文件", (tmp / "state.json.tmp").exists(), False)

data, src = S._read_json_with_fallback(p)
check("正常读取来源为主文件", src, "主文件")
check("正常读取数据", data, {"v": 2})

# 主文件损坏 → 回退 .bak
p.write_text("{ 这不是合法 JSON", encoding="utf-8")
data, src = S._read_json_with_fallback(p)
check("主文件损坏时回退 .bak", src, "备份(.bak)")
check("回退后拿到上一版数据", data, {"v": 1})

# 主文件与备份都坏
(tmp / "state.json.bak").write_text("???", encoding="utf-8")
data, src = S._read_json_with_fallback(p)
check("双双损坏时返回 None", data, None)

# fetch_history 用的是同一套语义
p2 = tmp / "history.json"
FH._atomic_write_json(p2, [{"id": "1"}])
check("fetch_history 原子写可用", json.loads(p2.read_text(encoding="utf-8")), [{"id": "1"}])
FH._atomic_write_json(p2, [{"id": "2"}])
check("fetch_history 生成 .bak",
      json.loads((tmp / "history.json.bak").read_text(encoding="utf-8")), [{"id": "1"}])


# ===============================================================
# 2. B1：history 全量保留（去重方向）
# ===============================================================
print("\n[2] B1 去重方向（不丢题）")

Q_A = ("AI 自适应问答\n第 1 题\n在Solidity中，下面哪个变量声明是正确的？\n"
       "A. uint a;\nB. int a;")
Q_B = ("AI 自适应问答\n第 2 题\n下面哪个是以太坊地址类型？\n"
       "A. uint\nB. address")
Q_C = ("AI 自适应问答\n第 3 题\n下面哪个是布尔类型？\n"
       "A. bool\nB. bytes32")

MSGS = [
    {"id": "1001", "is_from_other": True, "text": Q_A},
    {"id": "1002", "is_from_other": True, "text": Q_A},     # 机器人重复推送同一题
    {"id": "1003", "is_from_other": True, "text": Q_B},
    {"id": "1004", "is_from_other": False, "text": "B"},    # 我发的
    {"id": "1005", "is_from_other": True, "text": "B"},     # 机器人回的（同文本）
    {"id": "1006", "is_from_other": True, "text": Q_C},
]

raw_q = sum(1 for m in MSGS if "自适应问答" in m["text"])

new = D.denoise(MSGS, drop_reminders=False, dedup=False)      # ✅ 修复后
old = D.denoise(MSGS, drop_reminders=False)                   # ❌ 修复前（dedup 默认 True）

new_q = sum(1 for m in new if "自适应问答" in m["text"])
old_q = sum(1 for m in old if "自适应问答" in m["text"])

check("原始题目数", raw_q, 4)
check("修复后：题目数不减少", new_q, raw_q)
check("修复后：消息全量保留", len(new), 6)
ok("复现旧 bug：修复前题目数确实会减少", old_q < raw_q, f"old_q={old_q}")
ok("复现旧 bug：修复前 'B' 被折叠（我发的与机器人发的同文本）",
   len(old) == 4, f"old={len(old)}")

# 下游仍按"保留最新"去重（方向一致）
deduped = D.dedup_keep_last(new)
ok("dedup_keep_last 保留的是最后一次出现",
   [m["id"] for m in deduped if m["text"] == Q_A] == ["1002"],
   str([m["id"] for m in deduped if m["text"] == Q_A]))


# ===============================================================
# 3. M11：判重跳过可观测（on_skip）
# ===============================================================
print("\n[3] M11 判重跳过可观测")

FEEDBACK_Q10 = (
    "学习反馈\n📚 正在学习：智能合约技术\n\n✅ 答对了\n\n掌握度: 42%\n\n第 10 题\n\n"
    "在 Solidity 智能合约中，若要实现“通过用户的以太坊地址查询其拥有的僵尸数量”"
    "的功能，以下哪种变量声明方式是正确的？\n\n"
    "A. mapping (uint => address) public ownerZombieCount;\n"
    "B. mapping (address => uint) public ownerZombieCount;\n"
    "C. address[] public ownerZombieCount;\n"
    "D. struct ZombieCount { address owner; uint count; }\n\n"
    "💡 直接回复选项字母作答\n\n"
    "⚠️ 需切换课程，直接发新的课程名（如「密码学基础」）"
)

msgs = [{"id": "2001", "is_from_other": True, "text": FEEDBACK_Q10}]
fp = D.question_fingerprint(FEEDBACK_Q10)
ok("能从反馈卡里抽出第 10 题的指纹", fp is not None and fp[0] == 10, str(fp))

# (a) 未答过 → 正常返回，回调不该触发
calls = []
m, q, b = D.find_pending_question(msgs, set(), latest_only=True,
                                 on_skip=lambda qn, f: calls.append((qn, f)))
ok("未答过时返回该题", m is not None and q == 10, f"m={bool(m)} q={q}")
check("未答过时不触发 on_skip", calls, [])

# (b) 已答过 → 返回 None，且回调记录"跳过了第几题"
calls = []
m, q, b = D.find_pending_question(msgs, {D.fp_to_str(fp)}, latest_only=True,
                                 on_skip=lambda qn, f: calls.append((qn, f)))
ok("已答过时不再返回题目", m is None, f"m={m!r}")
ok("on_skip 记录了被跳过的是第 10 题", calls and calls[0][0] == 10, str(calls))

# (c) 回调抛异常不能影响主流程
m, q, b = D.find_pending_question(
    msgs, {D.fp_to_str(fp)}, latest_only=True,
    on_skip=lambda qn, f: (_ for _ in ()).throw(RuntimeError("boom")))
ok("on_skip 抛异常被安全吞掉", m is None, f"m={m!r}")


# ===============================================================
# 4. S1：发送失败熔断（纯判定）
# ===============================================================
print("\n[4] S1 发送失败熔断")

check("0 次失败 → 继续重试", S.send_fail_action(0), "retry")
check("2 次失败 → 继续重试", S.send_fail_action(2), "retry")
check("3 次失败 → 熔断跳过", S.send_fail_action(3), "skip")
check("4 次失败 → 仍熔断", S.send_fail_action(4), "skip")
check("自定义阈值", S.send_fail_action(1, limit=1), "skip")
check("None 视为 0", S.send_fail_action(None), "retry")
check("熔断阈值常量", S.MAX_SEND_FAIL, 3)

# 雪花 ID：id >> 22 才是毫秒时间戳
def sid(ts_ms):
    return str((ts_ms << 22) | 1)


t0 = 1_700_000_000_000
ok("同一题：未被顶掉", S.is_superseded(sid(t0), sid(t0)) is False)
ok("相隔 60 秒：未超冷却窗口", S.is_superseded(sid(t0), sid(t0 + 60_000)) is False)
ok("相隔 130 秒：判定已被顶掉", S.is_superseded(sid(t0), sid(t0 + 130_000)) is True)
ok("非数字 id 不误判", S.is_superseded("abc", "def") is False)


# ===============================================================
# 5. M13：日志带日期 + 与 GUI 的 LOG_RE 兼容
# ===============================================================
print("\n[5] M13 日志格式")

fmt = None
for h in S.logger.handlers:
    f = getattr(h, "formatter", None)
    if f is not None and hasattr(f, "datefmt"):
        fmt = f
        break
ok("找到日志 formatter", fmt is not None,
   f"handlers={[type(h).__name__ for h in S.logger.handlers]}")
check("日期格式含年份（跨天可定序）", getattr(fmt, "datefmt", ""), "%Y-%m-%d %H:%M:%S")
ok("长日志按大小轮转（不会无限增长）",
   any(isinstance(h, __import__("logging").handlers.RotatingFileHandler)
       for h in S.logger.handlers),
   str([type(h).__name__ for h in S.logger.handlers]))

try:
    import launcher as L  # 只读类属性，不实例化窗口
    sample = "2026-10-13 22:51:49 [INFO] 启动监控…"
    m = L.MainWindow.LOG_RE.match(sample)
    ok("launcher LOG_RE 能解析带日期的日志", m is not None)
    check("解析出的时间部分", m.group(1) if m else None, "22:51:49")
    check("解析出的等级", m.group(2) if m else None, "INFO")
    check("解析出的正文", m.group(3) if m else None, "启动监控…")
    m2 = L.MainWindow.LOG_RE.match("22:51:49 [INFO] 旧格式也兼容")
    ok("launcher LOG_RE 仍兼容旧格式（纯时间）", m2 is not None)
    ok("launcher 已装进程级异常兜底", callable(L.install_global_hooks))
    ok("launcher 死代码 _base_python 已移除", not hasattr(L, "_base_python"))
except Exception as e:            # 无显示环境下跳过（如 venv 未装 tkinter）
    L = None
    print(f"  - 跳过 launcher 检查：{e}")


# ===============================================================
# 6. 配置 / 常量一致性
# ===============================================================
print("\n[6] 配置一致性")

try:
    import yaml
    ex = yaml.safe_load((Path(".") / "config.example.yaml").read_text(encoding="utf-8"))
    check("config.example.yaml 有 context.pool_max",
          (ex.get("context") or {}).get("pool_max"), S.POOL_HARD_CAP)
    emb = yaml.safe_load(L.EMBEDDED_EXAMPLE)
    check("内嵌示例的 context.pool_max 与示例文件一致",
          (emb.get("context") or {}).get("pool_max"), S.POOL_HARD_CAP)
except Exception as e:
    print(f"  - 跳过配置检查：{e}")


# ===============================================================
# 7. S6：step1_check_login 模块级引用（静态检查）
# ===============================================================
print("\n[7] S6 step1_check_login 导入顺序")

# 只看代码行（跳过注释），否则注释里的示例文本会干扰匹配
code_lines = [ln.strip() for ln in
              Path("step1_check_login.py").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]


def first_idx(pred):
    for i, ln in enumerate(code_lines):
        if pred(ln):
            return i
    return -1


i_imp = first_idx(lambda l: l == "from runtime import DATA_DIR")
i_use = first_idx(lambda l: l == "BASE_DIR = DATA_DIR")
ok("DATA_DIR 的 import 出现在首次使用之前", 0 <= i_imp < i_use,
   f"imp@{i_imp} use@{i_use}")

i_cfg = first_idx(lambda l: l.startswith("from settings import load_config"))
i_cfg_use = first_idx(lambda l: l == "CFG = load_config()")
ok("load_config 的 import 出现在首次调用之前", 0 <= i_cfg < i_cfg_use,
   f"imp@{i_cfg} use@{i_cfg_use}")


# ===============================================================
# 8. S3：_tail_live 字节/字符一致 + 半行缓冲 + 收缩重置
# ===============================================================
print("\n[8] S3 实时日志增量读取（打包模式）")


class _FakeProc:
    def __init__(self, rc=None):
        self.pid = 4242
        self._rc = rc

    def poll(self):
        return self._rc


class _TailStub:
    """只实现 _tail_live 需要的最小接口（不需要真窗口）。"""

    def __init__(self, q):
        self.proc = None
        self.log_queue = q
        self._live_pos = 0
        self._tail_buf = ""
        self.after_calls = []
        self._live_fh = None
        self._tail_live = lambda: None      # 供 self.after(..., self._tail_live) 取值

    def after(self, ms, fn=None):
        self.after_calls.append(ms)


try:
    import queue as _queue

    L.LIVE_LOG.write_text("", encoding="utf-8")
    q = _queue.Queue()
    st = _TailStub(q)
    st.proc = _FakeProc(None)                     # 视为"仍在运行"

    # 第 1 段：中文 + Emoji + **故意不带换行**（半行必须留到下次）
    L.LIVE_LOG.write_text("22:10:01 [INFO] 中文测试 ✓📚", encoding="utf-8")
    L.MainWindow._tail_live(st)
    check("半行不输出（等待换行）", q.qsize(), 0)
    ok("位置按**字节**推进（=文件大小）",
       st._live_pos == L.LIVE_LOG.stat().st_size,
       f"pos={st._live_pos} size={L.LIVE_LOG.stat().st_size}")

    # 第 2 段：补上换行 + 第二行
    with open(L.LIVE_LOG, "a", encoding="utf-8") as f:
        f.write(" 继续\n22:10:02 [INFO] 第二行\n")
    L.MainWindow._tail_live(st)
    got = []
    while not q.empty():
        got.append(q.get())
    check("两次调用都读到增量（共 2 行）", len(got), 2)
    ok("中文内容完整（未被字节/字符错位截断）",
       "中文测试 ✓📚 继续" in got[0], repr(got[0]) if got else "空")
    ok("第二行内容正确", got[1].strip().endswith("第二行"),
       repr(got[1]) if len(got) > 1 else "空")

    # 第 3 段：位置被"外部清空"打乱（轮转/重写）→ 必须自动重置而不是永久卡死
    st._live_pos = L.LIVE_LOG.stat().st_size + 500
    L.MainWindow._tail_live(st)
    got = []
    while not q.empty():
        got.append(q.get())
    ok("文件变小（轮转/清空）后能自动重置并重读", len(got) >= 2, f"got={len(got)}")
    ok("重置后位置与文件大小一致",
       st._live_pos == L.LIVE_LOG.stat().st_size,
       f"pos={st._live_pos} size={L.LIVE_LOG.stat().st_size}")

    # 第 4 段：进程退出 → 收尾读完剩余 + 给出"进程已结束"
    st.proc._rc = 0
    with open(L.LIVE_LOG, "a", encoding="utf-8") as f:
        f.write("22:10:03 [INFO] 最后一行\n")
    L.MainWindow._tail_live(st)
    got = []
    while not q.empty():
        got.append(q.get())
    ok("退出时读完剩余内容", any("最后一行" in s for s in got), str(got))
    ok("退出时给出结束提示", any("进程已结束" in s for s in got), str(got))
except Exception as e:
    print(f"  - 跳过 _tail_live 检查：{e}")


# ===============================================================
# 9. S4：子进程退出码上报 + 句柄复位
# ===============================================================
print("\n[9] S4 退出码上报")


class _WinStub:
    def __init__(self, rc=None, stopping=False):
        self.proc = _FakeProc(rc)
        self._stopping = stopping
        self._live_fh = None
        self.logs = []
        self.states = []
        self.after_calls = 0

    # ⚠️ _close_live_fh / _watch 不能写在类体里直接从 L 取：
    #    launcher 不可导入（缺 tkinter / 无显示环境）时，类体求值会直接
    #    NameError 中断**整个脚本**，后面的用例全部静默不跑。
    #    改到下面 try 里绑定。

    def _log(self, line, tag=None):
        self.logs.append(line)

    def _set_state(self, key, detail=None):
        self.states.append((key, detail))

    def after(self, ms, fn=None):
        self.after_calls += 1


try:
    if L is None:
        raise RuntimeError("launcher 不可导入（需带 tkinter 的解释器）")
    # 直接复用被测的真实实现（不复制逻辑）
    _WinStub._close_live_fh = L.MainWindow._close_live_fh
    _WinStub._watch = L.MainWindow._watch

    L._clean_profile_locks = lambda: []

    w = _WinStub(rc=7)
    L.MainWindow._on_proc_exit(w, 7)
    ok("异常退出后复位 self.proc", w.proc is None)
    ok("异常退出 → 状态 error", w.states and w.states[0][0] == "error", str(w.states))
    ok("日志里带退出码", any("代码 7" in s for s in w.logs), str(w.logs))

    w2 = _WinStub(rc=0, stopping=True)
    L.MainWindow._on_proc_exit(w2, 1)
    ok("用户主动停止 → 不报错，状态 stopped",
       w2.states and w2.states[0][0] == "stopped", str(w2.states))

    w3 = _WinStub(rc=0)
    L.MainWindow._on_proc_exit(w3, 0)
    ok("正常退出（0）→ 状态 stopped",
       w3.states and w3.states[0][0] == "stopped", str(w3.states))

    w4 = _WinStub(rc=None)
    L.MainWindow._watch(w4)
    ok("进程仍在运行 → 继续轮询", getattr(w4, "after_calls", 0) == 1)

    w5 = _WinStub(rc=None)
    w5.proc = None
    L.MainWindow._watch(w5)
    ok("proc 已复位后 _watch 直接返回（不再空转）",
       getattr(w5, "after_calls", 0) == 0)
except Exception as e:
    print(f"  - 跳过退出码检查：{e}")


# ===============================================================
# 10. 打包漏收 stdlib 子模块时的日志兜底（v1.0.9 线上故障回归）
# ===============================================================
print("\n[10] 日志轮转导入兜底（防打包漏收 logging.handlers）")

try:
    import ast
    import importlib
    import logging as _lg

    # 从**真实文件**里抠出那段 try/except 源码来执行，避免复制逻辑导致测试失真
    _src = Path("step2_auto_reply.py").read_text(encoding="utf-8")
    _seg = None
    for _node in ast.parse(_src).body:
        if isinstance(_node, ast.Try) and any(
            isinstance(s, ast.ImportFrom) and s.module == "logging.handlers"
            for s in ast.walk(_node)
        ):
            _seg = ast.get_source_segment(_src, _node)
            break
    ok("step2 里 logging.handlers 的 import 有 try/except 保护", _seg is not None)

    if _seg:
        # 模拟"包里根本没有 logging.handlers"：sys.modules 置 None 会让后续
        # import 立刻抛 ModuleNotFoundError（比塞 meta_path finder 更确定）
        _had = "logging.handlers" in sys.modules
        _saved = sys.modules.get("logging.handlers")
        sys.modules["logging.handlers"] = None
        _ns = {"__name__": "_fallback_probe", "os": os, "logging": _lg}
        try:
            exec(compile(_seg, "step2_auto_reply.py:<import block>", "exec"), _ns)
        finally:
            if _had:
                sys.modules["logging.handlers"] = _saved
            else:
                sys.modules.pop("logging.handlers", None)

        _RFH = _ns.get("RotatingFileHandler")
        ok("模块缺失时仍拿到了 RotatingFileHandler（走的兜底类）",
           isinstance(_RFH, type) and _RFH.__module__ == "_fallback_probe",
           f"{_RFH}")

        if isinstance(_RFH, type):
            _d = Path(tempfile.mkdtemp(prefix="fb_log_"))
            _f = _d / "t.log"
            _h = _RFH(str(_f), maxBytes=200, backupCount=2, encoding="utf-8")
            _probe = _lg.getLogger("_fbprobe")
            _probe.propagate = False
            _probe.setLevel(_lg.DEBUG)
            _probe.addHandler(_h)
            for _i in range(80):
                _probe.info("x" * 40)
            _probe.removeHandler(_h)
            _h.close()
            ok("兜底 handler 确实轮转了（生成 t.log.1）", (_d / "t.log.1").exists())
            ok("兜底 handler 遵守 backupCount（不生成 t.log.3）",
               not (_d / "t.log.3").exists())
            ok("轮转后仍在写主文件", _f.exists() and _f.stat().st_size > 0)
except Exception as e:
    print(f"  - 跳过日志兜底检查：{e}")


# ===============================================================
print("\n" + "=" * 56)
print(f"  通过 {PASS} / 失败 {FAIL}")
print("=" * 56)

try:
    S._shutdown_logging()
except Exception:
    pass

sys.exit(1 if FAIL else 0)
