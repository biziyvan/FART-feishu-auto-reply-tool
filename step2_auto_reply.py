"""
飞书 AI 教学平台 · 自动回复主程序
==================================
设计原则（解决翻页/卡死/费token问题）：

  【不反复滚动】
   - 启动时：从本地 history.json 加载历史上下文（由 fetch_history.py 预先抓好）
   - 运行中：只读取当前可见的最新消息（不滚动），据此判断有无新消息
   - 需要更新历史时，手动跑 fetch_history.py 即可

  【不再卡死】
   - 所有浏览器操作加超时保护
   - 单轮执行时间有上限

  【省 token】
   - 用 denoise.py 去噪、压缩上下文

运行：
  .venv\\Scripts\\python.exe step2_auto_reply.py                # 一直运行
  .venv\\Scripts\\python.exe step2_auto_reply.py --max-replies 10  # 跑10次停
  .venv\\Scripts\\python.exe step2_auto_reply.py --dry-run --max-replies 5  # 只生成不发送
停止：Ctrl+C
"""

import sys
import json
import time
import os
import io
import re
import shutil
import random
import logging
import signal
import argparse
import subprocess
try:
    from logging.handlers import RotatingFileHandler
except Exception:  # pragma: no cover - 极端环境兜底（见下方说明）
    # ── 为什么要有这个兜底 ─────────────────────────────────────────
    # 打包时 PyInstaller 只静态分析 launcher.py；本脚本是**以数据文件形式**
    # 打进包、运行期才 exec 的，它的 import 对 PyInstaller 完全不可见。
    # 因此"只有本脚本用到"的模块不会被自动收集 → 运行期直接
    #   ModuleNotFoundError: No module named 'logging.handlers'
    # 而本脚本的 try/except（日志段）发生在 import 之后，救不了。
    # （v1.0.9 绿色版真实故障：点「启动监控」即崩，见 live.log。）
    # 根因已在 build.spec 里用「自动扫描 APP_SCRIPTS 的 import」修掉；
    # 这里再兜一层，保证「包内漏收」时程序仍能启动，只是日志轮转降级。
    class RotatingFileHandler(logging.FileHandler):
        """按大小轮转的 FileHandler，行为对齐 logging.handlers 版本。"""

        def __init__(self, filename, mode="a", maxBytes=0, backupCount=0,
                     encoding=None, delay=False):
            self.maxBytes = maxBytes or 0
            self.backupCount = backupCount or 0
            super().__init__(filename, mode=mode, encoding=encoding, delay=delay)

        def shouldRollover(self, record):
            if self.maxBytes <= 0:
                return False
            if self.stream is None:
                self.stream = self._open()
            self.stream.seek(0, os.SEEK_END)
            return self.stream.tell() + len("%s\n" % self.format(record)) >= self.maxBytes

        def doRollover(self):
            if self.stream:
                self.stream.close()
                self.stream = None
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = "%s.%d" % (self.baseFilename, i)
                    dfn = "%s.%d" % (self.baseFilename, i + 1)
                    if os.path.exists(sfn):
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.rename(sfn, dfn)
                dfn = self.baseFilename + ".1"
                if os.path.exists(dfn):
                    os.remove(dfn)
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, dfn)
            self.stream = self._open()

        def emit(self, record):
            try:
                if self.shouldRollover(record):
                    self.doRollover()
                logging.FileHandler.emit(self, record)
            except Exception:
                self.handleError(record)

from pathlib import Path

import yaml
import requests
from playwright.sync_api import sync_playwright

import runtime

from denoise import (
    denoise,
    build_context,
    classify,
    clean_text,
    extract_current_course,
    sort_by_time,
    find_pending_question,
    question_fingerprint,
    extract_pending_question,
    fp_to_str,
    snowflake_ts,
    is_course_prompt,
)


from runtime import DATA_DIR

BASE_DIR = DATA_DIR
STATE_FILE = BASE_DIR / "answered.json"
HISTORY_FILE = BASE_DIR / "history.json"
LOG_FILE = BASE_DIR / "auto_reply.log"
STATS_FILE = BASE_DIR / "reply_stats.json"
SHOT_DIR = BASE_DIR / "debug_shots"
# parents=True + 容错：数据目录被整体删除/只读时，也不能让 import 阶段直接崩
try:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

STATS_MAX = 2000        # reply_stats.json 最多保留多少条
POOL_HARD_CAP = 1200    # 消息池硬上限（防止连续运行数小时后全量排序越来越慢）
MAX_SEND_FAIL = 3       # 同一题连续发送失败多少次后熔断（跳过并等新题）


def _atomic_write_json(path, obj):
    """原子写 JSON：.tmp → 备份 .bak → 原子替换。

    崩溃/断电时若正处于"先截断再写"的中间态，会留下 0 字节文件；
    而读取侧解析失败会静默回退成空值 → 表现为"所有历史题都被重新认作新题"，
    系统性重复作答。改用临时文件 + os.replace 可彻底避免半截文件。
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


def _read_json_with_fallback(path):
    """读 JSON；主文件损坏时回退到 .bak。返回 (数据, 来源说明) 或 (None, 原因)。"""
    for cand, tag in ((Path(path), "主文件"),
                      (Path(str(path) + ".bak"), "备份(.bak)")):
        try:
            if cand.exists():
                return json.loads(cand.read_text(encoding="utf-8")), tag
        except Exception:
            continue
    return None, "均不可用"


def _save_fail_shot(page, note=""):
    """发送失败时落一张截图，便于事后定位（日志里给出文件名）。"""
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        p = SHOT_DIR / f"send_fail_{time.strftime('%H%M%S')}.png"
        page.screenshot(path=str(p))
        log(f"  已保存失败截图: {p.name} {note}".rstrip(), "warning")
    except Exception:
        pass


def send_fail_action(fail_count, limit=MAX_SEND_FAIL):
    """熔断判定：连续失败达到上限 → 跳过该题，否则 → 继续重试。

    纯函数（无副作用），便于单测。
    """
    return "skip" if int(fail_count or 0) >= int(limit) else "retry"


def is_superseded(old_id, newest_id, window_ms=120_000):
    """旧题是否已被新题顶掉（超过冷却窗口）——用于解除熔断。

    机器人推了新题之后，被熔断的旧题不该被永久禁答。
    """
    try:
        return snowflake_ts(newest_id) - snowflake_ts(old_id) > window_ms
    except Exception:
        return False


_running = True


def _stop(sig, frame):
    global _running
    _running = False


signal.signal(signal.SIGINT, _stop)


def parse_args():
    ap = argparse.ArgumentParser(description="飞书自动回复")
    ap.add_argument("--max-replies", type=int, default=0, help="最多回复次数（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只生成答案不发送")
    return ap.parse_args()


ARGS = parse_args()


# ---------------- 日志 ----------------
logger = logging.getLogger("auto")
logger.setLevel(logging.DEBUG)
# 带上日期：日志常跨多个日期段，只靠 %H:%M:%S 无法定序
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
# 只在没有 handler 时添加，避免被 import 两次导致日志重复
if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
    # 长期运行无限追加 → 按 5MB × 5 份轮转（保留 auto_reply.log.1 ~ .5）
    try:
        _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    except Exception:
        _fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)

# 打包运行时，GUI 通过「实时日志文件」读取输出（窗口程序无 stdout）
# 注意：这个文件每次点「启动监控」都会被清空，所以**不做轮转**
# （轮转会让 GUI 的增量读取错位）。
_LIVE_LOG = os.environ.get("FEISHU_LIVE_LOG")
if _LIVE_LOG and not any(
    isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == _LIVE_LOG
    for h in logger.handlers
):
    try:
        _lh = logging.FileHandler(_LIVE_LOG, mode="a", encoding="utf-8")
        _lh.setFormatter(_fmt)
        logger.addHandler(_lh)
    except Exception:
        pass

if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in logger.handlers):
    # 关键：打包成窗口程序后，stdout 可能是 None，或是 GBK 编码的控制台代理。
    # 日志里含 Emoji（📚✅💡），用 GBK 编码会直接抛 UnicodeEncodeError，
    # 因此统一包一层 UTF-8 + 容错的输出流。
    try:
        _raw_out = sys.stdout if sys.stdout is not None else open(os.devnull, "w")
    except Exception:
        _raw_out = open(os.devnull, "w")

    try:
        _safe_out = io.TextIOWrapper(
            _raw_out.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    except Exception:
        # 没有 .buffer（如已被包装过 / 特殊流）→ 退化为容错包装
        class _SafeStream:
            def __init__(self, s):
                self._s = s

            def write(self, data):
                try:
                    self._s.write(data)
                except Exception:
                    try:
                        self._s.write(data.encode("utf-8", "replace").decode("utf-8", "replace"))
                    except Exception:
                        pass

            def flush(self):
                try:
                    self._s.flush()
                except Exception:
                    pass

            def isatty(self):
                return False

        _safe_out = _SafeStream(_raw_out)

    try:
        _sh = logging.StreamHandler(_safe_out)
        _sh.setFormatter(_fmt)
        logger.addHandler(_sh)
    except Exception:
        pass
logger.propagate = False


def log(msg, level="info"):
    getattr(logger, level)(msg)


# ---------------- 配置 ----------------
from settings import (  # noqa: E402
    PROVIDER_LABELS,
    apply_thinking,
    detect_provider,
    load_config,
    provider_conflict,
    resolve_thinking,
)


CFG = load_config()
FS = CFG["feishu"]
BR = CFG["browser"]
POLL = CFG["polling"]
LLM = CFG["llm"]
REPLY = CFG["reply"]
CTX = CFG.get("context", {})
RSTRAT = CFG.get("reply_strategy", {})

# 「请输入课程名」提示 → 自动回复的课程名（默认「智能合约」；设为 "" 可关闭该功能）
AUTO_COURSE = str(RSTRAT.get("auto_reply_course", "智能合约") or "").strip()
# 同一条课程名提示在冷却期内不重复回复（秒），避免机器人重复推送时刷屏
COURSE_COOLDOWN = float(RSTRAT.get("auto_reply_course_cooldown", 120) or 120)

# 大模型接口自适应：按 base_url 判断是硅基流动还是 DeepSeek 官方，
# 从而决定用 enable_thinking 还是 thinking:{type:disabled}（见 settings.py）
PROVIDER = detect_provider(
    LLM.get("base_url", ""), LLM.get("model", ""), LLM.get("provider", "")
)
THINKING = resolve_thinking(PROVIDER, LLM)


def provider_desc():
    """启动日志用的一句话描述：接口 + 思考模式。"""
    label = PROVIDER_LABELS.get(PROVIDER, PROVIDER)
    if THINKING is True:
        think = "开启"
    elif THINKING is False:
        think = "关闭"
    else:
        think = "接口默认"
    return f"{label}（思考模式: {think}）"


# ---------------- 状态 ----------------
def load_state():
    data, src = _read_json_with_fallback(STATE_FILE)
    if isinstance(data, dict):
        if src != "主文件":
            log(f"answered.json 主文件不可用，已从{src}恢复", "warning")
        return data
    if data is None and STATE_FILE.exists():
        log("answered.json 与备份均无法解析 —— 已按空状态启动"
            "（本轮可能重复作答历史题，请检查磁盘）", "error")
    return {"answered": []}


def save_state(state):
    state.setdefault("answered", [])
    state["answered"] = state["answered"][-800:]
    if "answered_fps" in state:
        state["answered_fps"] = state["answered_fps"][-800:]
    _atomic_write_json(STATE_FILE, state)


def load_history():
    """加载本地历史（由 fetch_history.py 生成）。"""
    if not HISTORY_FILE.exists():
        return []
    data, src = _read_json_with_fallback(HISTORY_FILE)
    if isinstance(data, list):
        if src != "主文件":
            log(f"history.json 主文件不可用，已从{src}恢复", "warning")
        return data
    log("读取 history.json 失败（主文件与备份均不可解析）", "warning")
    return []


# ---------------- 大模型 ----------------
_FENCE_LINE_RE = re.compile(r"^[ \t]*```.*$", re.M)


def strip_code_fences(text):
    """剥掉三反引号代码围栏（含 ```lang 与结尾 ```）。

    原因：飞书会把 ``` 渲染成「代码块」，教学平台会据此把**整条消息**判成
    "资料"而不是答案，回一句"这看起来是一段资料，不是答案" —— 这题就白答了。
    prompt 里已要求不要用围栏，这里再做一层兜底，确保发出去的永远是纯文本。
    """
    if not text or "```" not in text:
        return text
    cleaned = _FENCE_LINE_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


# 判断题：老师端机器人只认英文 true / false，中文「正确 / 错误」会被判错，
# 所以这里在发送前把各种写法统一成 true / false 兜底。
_JUDGE_RE = re.compile(
    r"^\s*(?:答案|回答|判断|回复)?\s*[:：]?\s*"
    r"(true|false|正确|错误|对|错|是|否|真|假|t|f|√|✓|×|✗|yes|no)"
    r"\s*[。．.!！?？,，、;；]*\s*$",
    re.IGNORECASE,
)
# 无歧义：整条回复就是这些词时，任何题型都归一
_JUDGE_STRONG = {
    "正确": "true", "错误": "false", "true": "true", "false": "false",
    "yes": "true", "no": "false",
}
# 有歧义（单字/符号）：只在判断题语境下才归一
_JUDGE_WEAK = {
    "对": "true", "是": "true", "真": "true", "t": "true", "√": "true", "✓": "true",
    "错": "false", "否": "false", "假": "false", "f": "false", "×": "false", "✗": "false",
}


def normalize_true_false(text, is_judge=False):
    """把判断题回复统一成 true / false。

    仅在**整条回复**恰好是一个判断词（可带"答案："前缀与句末标点）时才替换，
    避免误伤简答题（如"正确性..."、整段解释）。
    """
    if not text:
        return text
    m = _JUDGE_RE.match(text)
    if not m:
        return text
    tok = m.group(1).strip().lower()
    if tok in _JUDGE_STRONG:
        return _JUDGE_STRONG[tok]
    if is_judge and tok in _JUDGE_WEAK:
        return _JUDGE_WEAK[tok]
    return text


_UNSET = object()


def build_payload(context_str, provider=None, thinking=_UNSET):
    """构造大模型请求体（纯函数，便于单测）。

    关键点：思考开关的参数名按接口类型自动切换 ——
      硅基流动 → enable_thinking
      DeepSeek → thinking:{type:disabled}（且开启思考时不能带 temperature）
    """
    provider = provider or PROVIDER
    if thinking is _UNSET:
        thinking = THINKING if provider == PROVIDER else resolve_thinking(provider, LLM)

    payload = {
        "model": LLM["model"],
        "messages": [
            {"role": "system", "content": LLM["system_prompt"].strip()},
            {"role": "user", "content": context_str},
        ],
        "max_tokens": LLM.get("max_tokens", 500),
        "temperature": LLM.get("temperature", 0.3),
    }
    return apply_thinking(payload, provider, thinking)


def ask_llm(context_str):
    """把已构造好的精简上下文发给大模型。"""
    api_key = LLM.get("api_key", "").strip()
    if not api_key:
        log("未配置 API Key", "error")
        return ""

    payload = build_payload(context_str)

    url = LLM["base_url"].rstrip("/") + "/chat/completions"
    timeout = LLM.get("timeout", 45)
    max_retries = LLM.get("max_retries", 3)
    last_err = ""

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            last_err = f"HTTP {e} | {getattr(e.response, 'text', '')[:150]}"
            code = getattr(e.response, "status_code", 0)
            if 400 <= code < 500 and code != 429:
                log(f"  请求被拒: {last_err}", "error")
                return ""
        except Exception as e:
            last_err = str(e)[:150]

        if attempt < max_retries:
            wait = attempt * 2
            log(f"  第 {attempt} 次失败({last_err[:50]})，{wait}s 后重试", "warning")
            time.sleep(wait)

    log(f"  大模型最终失败: {last_err}", "error")
    return ""


# ---------------- 浏览器 ----------------
# 反"后台冻结"启动参数：
#   Chromium 在窗口被遮挡/最小化/失去焦点时，会把标签页降级为后台状态，
#   停止渲染、停止定时器、降低 CPU —— 导致页面 DOM 不再更新（读不到新消息），
#   CDP 通信也可能超时。下面这些开关强制浏览器始终保持"前台活跃"语义。
ANTI_THROTTLE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    # 关闭后台标签页节流（最关键）
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # 关闭页面可见性降级 / 焦点抢占限制
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
    "--disable-ipc-flooding-protection",
    # 允许窗口在失焦时仍正常渲染
    "--disable-backgrounding-occluded-windows",
    "--disable-hang-monitor",
    # 不做窗口遮挡检测
    "--disable-window-occlusion-tracking",
]

# Edge 155+ 引入了"兼容层重启"：以自动化参数启动时，Edge 会先把自己重启一遍
# 再接管，Playwright 刚连上就发现原进程已退出 → TargetClosedError
# （表现为"启动即秒退"，退出码 0、无任何报错输出）。
# 该开关让 Edge 跳过这次重启。仅 Edge 内核需要，其余 Chromium 会忽略未知开关。
EDGE_COMPAT_ARGS = ["--edge-skip-compat-layer-relaunch"]


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 让页面认为自己始终"可见"（拦截 Page Visibility API）
VISIBILITY_SCRIPT = """
Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
window.addEventListener('blur', e => {}, true);
window.addEventListener('focus', e => {}, true);
"""


def profile_dir():
    """浏览器用户数据目录（绝对路径）。"""
    return (BASE_DIR / BR["user_data_dir"]).resolve()


def clean_profile_locks(ud=None):
    """删除 Chromium 的单实例锁文件。

    程序被强杀（任务管理器结束进程 / 断电 / 崩溃）后，user_data_dir 里会残留
    SingletonLock 等锁文件。下次启动时浏览器会误判"已有实例在运行"，把命令
    转交给那个不存在的实例后**立即自行退出**，playwright 于是报
    TargetClosedError: Target page, context or browser has been closed。
    """
    ud = Path(ud or profile_dir())
    removed = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            for f in ud.glob(name):
                try:
                    f.unlink()
                    removed.append(f.name)
                except Exception:
                    pass
        except Exception:
            pass
    return removed


def find_browser_pids_using(ud=None):
    """找出命令行中使用了指定配置目录的 Edge/Chrome 进程 PID。

    仅匹配**我们自己的配置目录**，不会误伤用户正在使用的浏览器。
    """
    ud = Path(ud or profile_dir())
    key = str(ud).lower().replace("'", "''")
    ps = (
        "$k = '" + key + "';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($k) } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    pids = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=25, creationflags=_NO_WINDOW,
        )
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except Exception:
        pass
    return pids


def kill_pids(pids):
    """结束进程树（/T 连带子进程），避免残留浏览器锁住配置目录。"""
    pids = [p for p in pids if isinstance(p, int) and p > 0]
    if not pids:
        return
    cmd = ["taskkill", "/F", "/T"]
    for p in pids:
        cmd += ["/PID", str(p)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=40, creationflags=_NO_WINDOW)
    except Exception:
        # 批量失败则逐个来
        for p in pids:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p)],
                               capture_output=True, timeout=20, creationflags=_NO_WINDOW)
            except Exception:
                pass


def recover_profile(ud=None):
    """清锁 + 结束仍占用该配置目录的残留浏览器进程。"""
    ud = Path(ud or profile_dir())
    locks = clean_profile_locks(ud)
    if locks:
        log(f"  已清理残留锁文件: {', '.join(locks)}")
    pids = find_browser_pids_using(ud)
    if pids:
        log(f"  发现 {len(pids)} 个残留浏览器进程占用该配置，正在结束 …")
        kill_pids(pids)
        time.sleep(2)
    return bool(locks or pids)


def _attach_visibility_script(ctx):
    try:
        ctx.add_init_script(VISIBILITY_SCRIPT)
    except Exception:
        pass


def launch_browser(p):
    """启动浏览器。带"自愈重试"：

    实测飞书/Edge 环境下浏览器启动失败（TargetClosedError）最常见的原因，
    是配置目录被上一次残留的浏览器进程占用（程序被强杀、点了停止又有残留、
    或 Edge 正在后台自动更新）。这类故障是**瞬时**的，清掉锁 + 结束残留进程
    后重试即可成功，不该让用户看到崩溃。

    依次尝试：① 抗节流参数 → ② 最小参数 → ③ 完全默认参数
    """
    ud = profile_dir()
    try:
        ud.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    base = dict(
        user_data_dir=str(ud),
        headless=BR["headless"],
        slow_mo=BR["slow_mo"],
        viewport={"width": BR["viewport_width"], "height": BR["viewport_height"]},
    )
    if BR.get("channel"):
        base["channel"] = BR["channel"]

    # (说明, 附加参数, 本尝试前是否先自愈, 重试前等待秒数)
    # 注意：自愈成功后要**先用原来的抗节流参数重试**，只有确实起不来才降级，
    # 否则会白白丢掉"抗浏览器遮挡"这个关键能力。
    # 中间穿插的等待，是为了扛住 Edge 后台自动更新之类的瞬时故障。
    # Edge 155+ 必须带"跳过兼容层重启"开关，否则每种参数组合都会秒退，
    # 因此把它并进**每一条**尝试路径（含降级的最小/默认参数），不能只在抗节流里。
    _chan = str(BR.get("channel") or "")
    compat = list(EDGE_COMPAT_ARGS) if _chan.startswith("msedge") else []

    attempts = [
        ("抗节流参数", ANTI_THROTTLE_ARGS + compat, False, 0),
        ("抗节流参数", ANTI_THROTTLE_ARGS + compat, True, 2),
        ("抗节流参数", ANTI_THROTTLE_ARGS + compat, True, 10),
        ("最小参数", ["--disable-blink-features=AutomationControlled"] + compat, False, 2),
        ("默认参数", compat or None, False, 10),
    ]

    # 启动前的信息性体检：只在发现"上次浏览器可能没退干净"的迹象时才做
    # 昂贵的进程扫描（否则每次启动白白多花几秒）。
    try:
        if any(ud.glob("Singleton*")) or (ud / "lockfile").exists():
            leftovers = find_browser_pids_using(ud)
            if leftovers:
                log(f"  提示：检测到 {len(leftovers)} 个残留浏览器进程占用该配置，"
                    f"若启动失败将自动清理", "warning")
    except Exception:
        pass

    last_err = None
    t_start = time.time()
    for idx, (label, extra_args, do_recover, delay) in enumerate(attempts, 1):
        if delay:
            log(f"  等待 {delay}s 后重试（{idx}/{len(attempts)}）…", "warning")
            time.sleep(delay)
        if do_recover:
            log(f"  正在自愈后重试（{idx}/{len(attempts)}）…", "warning")
            recover_profile(ud)

        kw = dict(base)
        if extra_args:
            kw["args"] = extra_args

        try:
            ctx = p.chromium.launch_persistent_context(**kw)
            if idx > 1:
                log(f"  浏览器已启动（第 {idx} 次尝试，{label}）")
            else:
                log(f"  浏览器已就绪（{time.time() - t_start:.1f}s）")
            _attach_visibility_script(ctx)
            return ctx
        except Exception as e:
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            last_err = e
            if not is_lost_error(e):
                # 不是"启动即退出"类的问题（如配置写错），直接抛
                raise
            log(f"  浏览器启动失败（{label}）：{msg[:110]}", "warning")

    raise RuntimeError(f"浏览器多次启动失败：{str(last_err).splitlines()[0][:200]}")


def wait_for_chat_list(page, timeout=90):
    """等待会话列表出现。页面失联不在这里处理，只返回 False 由上层恢复。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            loc = page.locator(".list_items")
            if loc.count() > 0 and len(loc.first.inner_text()) > 20:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def is_lost_error(e):
    """判断异常是否为"页面/浏览器失联"类（被遮挡冻结时常见）。"""
    msg = str(e)
    return any(k in msg for k in (
        "has been closed", "Target closed", "Target page",
        "Browser has been closed", "Connection closed",
        "Protocol error", "Execution context was destroyed",
        "Target crashed", "Session closed",
    ))


def open_target_chat(page, name):
    try:
        loc = page.locator(f'.list_items >> text="{name}"')
        if loc.count() > 0:
            loc.first.click(timeout=8000)
            time.sleep(2)
            return True
    except Exception as e:
        if is_lost_error(e):
            raise  # 交给上层恢复
        log(f"  打开会话失败: {str(e)[:80]}", "warning")
    return False


def snapshot_visible(page, limit=80):
    """
    只读取当前 DOM 可见的消息（不滚动）。
    返回按【真实时间序】（雪花 ID 时间分量）升序的列表。

    注意：页面失联类错误【不吞掉】，向上抛出让主循环触发自动恢复；
           只有单条消息读取失败才忽略。
    """
    out = {}
    items = page.locator(".js-message-item")
    n = items.count()
    start = max(0, n - limit)
    for i in range(start, n):
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
            # 单条消息读取失败（可能正被虚拟滚动回收），忽略
            continue
    # 记录 DOM 实际条数，便于诊断
    snapshot_visible.last_dom_count = n
    return sort_by_time(out.values())


snapshot_visible.last_dom_count = 0


def _count_my_messages(page):
    """统计聊天记录里"我发出的"消息条数，用于发送后校验。"""
    try:
        return page.evaluate(
            """() => document.querySelectorAll(
                '.js-message-item:not(.message-not-self)'
            ).length"""
        )
    except Exception:
        return -1


def _read_input_box(page):
    """读取输入框当前文本。"""
    try:
        return page.evaluate(
            """() => {
                const el = document.querySelectorAll('[contenteditable="true"]');
                const box = el[el.length - 1];
                return box ? box.innerText.trim() : '';
            }"""
        )
    except Exception:
        return ""


def _clear_input_box(page, tries=3):
    """
    清空输入框残留内容。

    飞书是 Lark 富文本编辑器，单次 execCommand('delete') 常常清不干净
    （会残留部分文本 / 空段落节点）。这里循环多次，并用 CDP 键盘事件
    做一次 Ctrl+A + Delete 兜底。
    """
    for i in range(tries):
        try:
            # 方式 1：全选 + 删除
            page.evaluate(
                """() => {
                    const el = document.querySelectorAll('[contenteditable="true"]');
                    const box = el[el.length - 1];
                    if (!box) return;
                    box.focus();
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    const r = document.createRange();
                    r.selectNodeContents(box);
                    sel.addRange(r);
                    document.execCommand('delete', false);
                    box.dispatchEvent(new InputEvent('input', {bubbles: true}));
                }"""
            )
            time.sleep(0.25)
            if not _read_input_box(page):
                return True
        except Exception:
            pass

        # 方式 2：CDP 派发 Ctrl+A 然后 Delete（更贴近真实操作）
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "a", "code": "KeyA",
                "modifiers": 2,  # Ctrl
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
            })
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA",
                "modifiers": 2,
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
            })
            time.sleep(0.15)
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
            })
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
            })
            time.sleep(0.3)
            if not _read_input_box(page):
                return True
        except Exception:
            pass

    return not _read_input_box(page)


def _norm_msg(s):
    """归一化消息文本，用于"输入框内容 == 要发的内容"的比对。

    只去掉空白与零宽字符（Lark 会在行尾塞 \u200b，富文本换行方式也不固定），
    字母/代码/中文一律保留，避免把不同内容误判成相同。
    """
    return re.sub(r"[\s\u200b]+", "", s or "")


def _last_my_message(page):
    """我发出的最后一条消息 → (id, text)。读不到时返回 ('', '')。

    比"数我方消息条数"可靠得多：
      · 虚拟滚动只保留 37~64 条，**条数可能不变**（旧的滚出、新的滚入），
        导致明明发出去了却被判成"发送失败" → 触发重发（重复回答的根源）；
      · 最后一条我方消息的 **id 变了**，就说明确实产生了新的我方消息。
    """
    try:
        d = page.evaluate(
            """() => {
                const items = document.querySelectorAll(
                    '.js-message-item:not(.message-not-self)'
                );
                const el = items[items.length - 1];
                if (!el) return {id: '', text: ''};
                return {id: el.id || '', text: (el.innerText || '').trim()};
            }"""
        )
        d = d or {}
        return str(d.get("id") or ""), str(d.get("text") or "")
    except Exception:
        return "", ""


def _after_send_action(box_text, want, sent_confirmed):
    """发送后的决策（纯函数，便于回归测试）。

    sent_confirmed 为 False（即无法确认发出）时：
      · 输入框已空      → 'ok'     —— 回车已生效，Lark 会把输入框清空，
                                     说明消息已经出去了，**绝不能重打**（否则重复）
      · 输入框 == 要发的 → 'resend' —— 内容还躺在框里，确实没发出去，可重试
      · 输入框是别的内容 → 'abort'  —— 残留 + 新内容拼在一起了（如 "BC"），
                                     平台只会读到第一个字母 → 系统性判错，必须放弃
    """
    if sent_confirmed:
        return "ok"
    got = _norm_msg(box_text)
    want = _norm_msg(want)          # 容错：调用方传原文也能正确比对
    if not got:
        return "ok"
    if got == want:
        return "resend"
    return "abort"


def send_reply(page, text):
    """
    发送回复（严格保证：要么只发一条正确内容，要么不发）。

    飞书输入框是 Lark 富文本编辑器（`.zone-container.editor-kit-container`），
    直接改 DOM 不会同步编辑器内部状态，因此用 **CDP Input.insertText**
    （浏览器原生输入层插入），再用 CDP 派发 Enter。
    不依赖系统窗口焦点 → 浏览器被遮挡时也能工作。

    【为什么原来的实现会出错】（2026-10-13 他机日志实证）
      · 成功判据用"我方消息条数 +1"：虚拟滚动时**条数可能不变**（旧消息滚出、
        新消息滚入），于是真发出去了也被判成"发送失败" → 走兜底重打 → 重复。
      · 兜底路径"清空输入框 → 重打一遍 → 回车"：Lark 的富文本**清不干净**，
        残留的旧字母会和新的拼在一起（如残留 "B" + 新答案 "C" = "BC"）→
        平台只取第一个字母 → **答案对却判错**（日志里第15/23/24/27/28题全中此坑）。
      · 插入后只校验输入框"非空"，**从不校验内容是否等于要发的内容**。

    【现在的设计】
      1. 内容对齐：清空 → insertText → **校验输入框内容 == 要发的内容**（不一致就重来）
      2. 发出去：内容确认无误才派发 Enter
      3. 判定发出：最后一条我方消息的 id 变了 / 条数增加（内容相等兜底）
      4. 未确认发出时按 _after_send_action 决策：**输入框已空就不重打**（防重复），
         内容仍是原文才重试，内容可疑直接放弃（防拼接发错）
    """
    box = page.locator('[contenteditable="true"]').last
    if box.count() == 0:
        log("  找不到输入框", "error")
        return False

    want = _norm_msg(text)
    before_id, _ = _last_my_message(page)
    before_cnt = _count_my_messages(page)

    def _sent_now():
        """消息是否确实发出。"""
        after_id, after_last = _last_my_message(page)
        if after_id and after_id != before_id:
            return True                       # 我发出的最后一条换新了
        after_cnt = _count_my_messages(page)
        if after_cnt > before_cnt:
            return True                       # 条数增加
        if after_cnt == before_cnt == -1:
            return _norm_msg(after_last) == want   # 计数读不到 → 按内容判
        return False

    # ---- 路径 1：CDP 原生输入 + Enter（首选，后台可用）----
    try:
        aligned = False
        for attempt in range(3):
            _clear_input_box(page)
            time.sleep(0.2)
            if _norm_msg(_read_input_box(page)):
                continue                      # 仍有残留 → 再清一轮
            try:
                box.click(timeout=5000)
            except Exception:
                pass
            time.sleep(0.3)

            cdp = page.context.new_cdp_session(page)
            cdp.send("Input.insertText", {"text": text})
            time.sleep(0.4)

            got = _norm_msg(_read_input_box(page))
            if got == want:
                aligned = True
                break
            log(f"  输入框内容与预期不一致（第 {attempt + 1} 次: {got[:40]!r}），清空重试",
                "warning")

        if not aligned:
            # 绝不在"内容对不上"的情况下发送：发出去的可能是拼接内容
            log("  ⚠ 输入框内容无法与预期对齐，已放弃本次发送", "error")
            _clear_input_box(page)
            return False

        # 内容确认无误 → 派发 Enter
        cdp = page.context.new_cdp_session(page)
        for ev_type in ("keyDown", "keyUp"):
            cdp.send("Input.dispatchKeyEvent", {
                "type": ev_type,
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            })
        time.sleep(1.2)

        if _sent_now():
            return True
        action = _after_send_action(_read_input_box(page), want, False)
        if action == "ok":
            log("  条数/id 未变，但输入框已清空 → 判定已发出", "warning")
            return True
        if action == "abort":
            log("  ⚠ 输入框残留异常内容，放弃发送（避免发出拼接内容）", "error")
            _clear_input_box(page)
            return False
        log("  CDP 方式未发出，尝试点击发送按钮", "warning")
    except Exception as e:
        log(f"  CDP 方式异常: {str(e)[:70]}", "warning")

    # ---- 路径 2：点击发送按钮（仅当输入框里仍是原文时才执行）----
    try:
        if _sent_now():
            return True
        if _norm_msg(_read_input_box(page)) != want:
            log("  输入框内容已不匹配，跳过点击发送", "warning")
            _clear_input_box(page)
            return False
        btn = page.locator(
            'button[class*="send"]:not([disabled]), [class*="send-button"], [aria-label*="发送"]'
        ).last
        if btn.count() > 0:
            btn.click(timeout=3000)
            time.sleep(1.2)
            if _sent_now():
                return True
            if _after_send_action(_read_input_box(page), want, False) == "ok":
                return True
    except Exception as e:
        log(f"  点击发送按钮失败: {str(e)[:70]}", "warning")

    # ---- 路径 3：键盘模拟（最后的兜底）----
    # ⚠️ 只有"输入框确认为空"时才敢重打；否则重打会把残留内容拼进去
    try:
        if _sent_now():
            return True
        if _norm_msg(_read_input_box(page)):
            log("  ⚠ 输入框仍有内容，放弃兜底重打（避免发出拼接内容）", "error")
            _clear_input_box(page)
            return False

        _clear_input_box(page)
        time.sleep(0.2)
        box.click(timeout=5000)
        time.sleep(0.3)
        page.keyboard.type(text, delay=25)
        time.sleep(0.4)
        if _norm_msg(_read_input_box(page)) != want:
            log("  ⚠ 兜底输入内容不符，放弃发送（避免发出拼接内容）", "error")
            _clear_input_box(page)
            return False
        page.keyboard.press("Enter")
        time.sleep(1.2)
        if _sent_now():
            return True
        if _after_send_action(_read_input_box(page), want, False) == "ok":
            return True
    except Exception as e:
        log(f"  键盘方式失败: {str(e)[:70]}", "error")

    return False


def page_alive(page):
    """检查页面是否仍然可用（未被冻结/关闭）。"""
    try:
        page.evaluate("() => document.readyState")
        return True
    except Exception:
        return False


def ensure_page_ready(page, target_name, ctx=None, retries=3):
    """
    确保页面处于可用状态，尽最大努力恢复：
      1. 页面可用 → 直接返回
      2. 页面对象失效（is_closed）→ 用 context 新建一个页面
      3. 页面活着但 CDP 卡住 → 尝试 goto 重载
    恢复后重新进入目标会话。
    返回可用的 page（可能与传入的不同）。
    """
    for attempt in range(1, retries + 1):
        # --- 情况 A：页面对象已彻底关闭，必须新建 ---
        try:
            closed = page.is_closed()
        except Exception:
            closed = True  # 连 is_closed 都调不了，视作已关闭

        if closed:
            if ctx is None:
                log("  页面已关闭且无 context 可新建，无法恢复", "error")
                return page
            log(f"  页面已关闭，新建页面（第 {attempt} 次）", "warning")
            try:
                page = ctx.new_page()
                page.goto(FS["base_url"], wait_until="domcontentloaded", timeout=40000)
                if wait_for_chat_list(page, timeout=60):
                    try:
                        open_target_chat(page, target_name)
                    except Exception as e:
                        if not is_lost_error(e):
                            raise
                    if page_alive(page):
                        time.sleep(2)
                        return page
            except Exception as e:
                log(f"  新建页面失败: {str(e)[:80]}", "warning")
                time.sleep(3)
                continue

        # --- 情况 B：页面没关但失联（CDP 卡住），尝试重载 ---
        if not page_alive(page):
            log(f"  页面失联，尝试重载（第 {attempt} 次）", "warning")
            try:
                page.goto(FS["base_url"], wait_until="domcontentloaded", timeout=40000)
                if wait_for_chat_list(page, timeout=60):
                    try:
                        open_target_chat(page, target_name)
                    except Exception as e:
                        if not is_lost_error(e):
                            raise
                    if page_alive(page):
                        time.sleep(2)
                        return page
            except Exception as e:
                log(f"  重载失败: {str(e)[:80]}", "warning")
                # 重载也失败，下一轮会走"新建页面"
                try:
                    page.close()
                except Exception:
                    pass
            time.sleep(3)
            continue

        # --- 情况 C：页面活着，确认还在目标会话 ---
        try:
            if page.locator(".js-message-item").count() > 0:
                return page
        except Exception:
            pass
        log(f"  页面无消息区，尝试重进会话（第 {attempt} 次）", "warning")
        try:
            if wait_for_chat_list(page, timeout=45):
                open_target_chat(page, target_name)
                time.sleep(3)
                return page
        except Exception as e:
            log(f"  重进会话失败: {str(e)[:80]}", "warning")
        time.sleep(2)

    return page


def bring_to_front(page):
    """尝试把窗口提到前台（失败也无所谓，只是锦上添花）。"""
    try:
        page.bring_to_front()
    except Exception:
        pass


# ---------------- 「请输入课程名」自动开课 ----------------
def _course_prompt_fp(text):
    """课程名提示的内容指纹（用于去重，避免同一条提示反复回复）。"""
    t = re.sub(r"\s+", "", clean_text(text))
    return t[:80] if t else None


def _maybe_reply_course(all_msgs, state, page):
    """若最新一条机器人消息是"课程名提示"，自动回复课程名以开始下一轮作答。

    触发条件（全部满足）：
      · 配置了 auto_reply_course（非空）
      · 最新一条是机器人消息，且被判定为"请你输入课程名"的提示（无题目）
      · 该提示的内容指纹不在已回复记录里、且不在冷却期内

    返回 True 表示本轮已处理（已发送 / 干跑计数）。
    仅在"没有待答题目"时调用，以免抢答真正的题目。
    """
    if not AUTO_COURSE:
        return False
    newest = all_msgs[-1] if all_msgs else None
    if not newest or not newest.get("is_from_other"):
        return False
    nt = clean_text(newest.get("text", ""))
    if not is_course_prompt(nt):
        return False

    # 去重：同一内容已回复过，或处于冷却期内 → 不重复回复
    fp = _course_prompt_fp(nt)
    if fp and fp in set(state.get("course_replied_fps", [])):
        return False
    now = time.time()
    if now - float(state.get("course_last_ts", 0) or 0) < COURSE_COOLDOWN:
        return False

    log(f"[开课] 检测到课程名提示，自动回复「{AUTO_COURSE}」开始下一轮")
    log(f"  触发消息: {nt[:100].replace(chr(10), ' / ')}")

    if ARGS.dry_run:
        log("  [干跑] 不发送")
        sent = True
    else:
        delay = random.uniform(REPLY["send_delay_min"], REPLY["send_delay_max"])
        log(f"  等待 {delay:.1f}s 后发送 ...")
        time.sleep(delay)
        sent = send_reply(page, AUTO_COURSE)
        log("  已发送 ✓" if sent else "  发送失败", "info" if sent else "error")

    if sent:
        if fp:
            state.setdefault("course_replied_fps", []).append(fp)
            state["course_replied_fps"] = state["course_replied_fps"][-50:]
        state["course_last_ts"] = now
        save_state(state)
    return sent


# ---------------- 主流程 ----------------
def main():
    log("=" * 62)
    log("  飞书 AI 教学平台 · 自动回复程序 启动")
    log("=" * 62)

    if not LLM.get("api_key", "").strip():
        log("请先在 config.yaml 填入 llm.api_key！", "error")
        _shutdown_logging()
        sys.exit(1)

    state = load_state()
    answered = set(state.get("answered", []))
    log(f"模型: {LLM['model']}  |  接口: {provider_desc()}")
    _conflict = provider_conflict(LLM.get("base_url", ""), LLM.get("provider", ""))
    if _conflict:
        log(
            f"配置提醒: config.yaml 里写的 provider 是「{_conflict[0]}」，"
            f"但接口地址其实是「{PROVIDER_LABELS.get(_conflict[1], _conflict[1])}」。"
            f"已按接口地址自动处理（参数按正确的一套发）；"
            f"建议把 llm.provider 改成 \"auto\" 以免混淆。",
            "warning",
        )
    log(f"已回复记录: {len(answered)} 条")

    # 运行环境自述：同一份 exe 可能跑着不同版本的逻辑脚本（增量更新通道），
    # 日志里没有版本号与生效参数，线上问题就无法定性。
    try:
        log(f"程序版本: {runtime.app_version()}  |  逻辑版本: {runtime.logic_version_note()}")
        log(f"数据目录: {BASE_DIR}")
        log(f"配置文件: {runtime.config_path()}")
        log(f"生效参数: timeout={LLM.get('timeout', 45)}s  "
            f"max_retries={LLM.get('max_retries', 3)}  "
            f"context_size={CTX.get('context_size', 30)}  "
            f"max_chars={CTX.get('max_chars', 3000)}  "
            f"pool_max={CTX.get('pool_max', POOL_HARD_CAP)}")
    except Exception:
        pass

    # 加载历史上下文
    base_history = load_history()
    if base_history:
        log(f"已加载历史上下文 {len(base_history)} 条（来自 history.json）")
    else:
        log("未找到 history.json，将只基于当前可见消息作答", "warning")
        log("  建议先运行 fetch_history.py 抓取历史", "warning")

    context_size = CTX.get("context_size", 30)
    max_chars = CTX.get("max_chars", 3000)

    with sync_playwright() as p:
        log("启动浏览器 ...")
        try:
            ctx = launch_browser(p)
        except Exception as e:
            log("浏览器启动失败。", "error")
            log(f"  原因: {str(e).splitlines()[0][:180]}", "error")
            log("", "error")
            log("  请按顺序尝试：", "error")
            log("   1) 关闭所有 Edge 窗口，再点「启动监控」重试", "error")
            log("   2) 仍失败 → 删除程序目录下的 .browser_data 文件夹，"
                "再启动（需重新扫码登录）", "error")
            log("   3) 仍失败 → 重启电脑后重试", "error")
            log("", "error")
            log("  （提示：Edge 正在后台自动更新时也会出现此现象，等 1 分钟再试通常就好）",
                "error")
            _shutdown_logging()
            sys.exit(2)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(FS["base_url"], wait_until="domcontentloaded", timeout=60000)

        if "accounts.feishu" in page.url or "login" in page.url.lower():
            log("需要登录，请扫码（最多等 3 分钟）...")
            deadline = time.time() + 180
            while time.time() < deadline:
                if "accounts.feishu" not in page.url and "login" not in page.url.lower():
                    break
                time.sleep(2)
            if "messenger" not in page.url:
                page.goto(FS["base_url"], wait_until="domcontentloaded", timeout=60000)

        log("等待会话列表 ...")
        if not wait_for_chat_list(page):
            log("会话列表加载失败", "error")
            ctx.close()
            _shutdown_logging()
            sys.exit(1)

        target = FS["target_chat_name"]
        log(f"进入会话「{target}」...")
        if not open_target_chat(page, target):
            log("无法进入目标会话", "error")
            ctx.close()
            _shutdown_logging()
            sys.exit(1)

        log(f"就绪。轮询 {POLL['interval']}s（不滚动，只读最新消息）")
        if ARGS.max_replies > 0:
            log(f"限制：最多回复 {ARGS.max_replies} 次")
        if ARGS.dry_run:
            log("干跑模式：不发送")
        log("-" * 62)

        interval = POLL["interval"]
        jitter = POLL.get("jitter", 0)
        reply_count = 0
        stats = []
        # 内存中的完整消息池（历史 + 新读到的最新消息），用于构造上下文
        pool = {m["id"]: m for m in base_history if m.get("id")}
        # 已答过的"题目指纹"（而不是消息 id）——避免机器人重复推送同一题时重复作答
        answered_fps = set(state.get("answered_fps", []))
        log(f"已记录已答题指纹: {len(answered_fps)} 个")
        err_streak = 0
        idle_rounds = 0
        pool_max = int(CTX.get("pool_max", POOL_HARD_CAP) or POOL_HARD_CAP)

        # 发送失败熔断 + 重试复用同一答案。
        # 原实现里"失败"只是打一行日志：该题仍属"未答"，下一轮会被再次选中
        # → 再次调用模型（输出可能不同）→ 再发一次，**无上界**；
        # 且失败不计入 reply_count，连"最多回复 N 次"也拦不住。
        send_fail = {}        # trigger_id -> 连续失败次数
        first_answer = {}     # trigger_id -> 首次生成的答案（重试时复用，避免输出漂移）
        trip_logged = [0]     # 熔断提示的节流计数（避免每轮刷屏）

        def _save_stats():
            if len(stats) > STATS_MAX:
                del stats[:-STATS_MAX]
            _atomic_write_json(STATS_FILE, stats)

        def _on_fp_skip(qn, fp):
            tag = f"第 {qn} 题" if qn is not None else "（无题号）"
            log(f"[判重] 最新题 {tag} 命中已答集合 → 跳过"
                f"（若机器人确实刚推了这道新题，请把此日志反馈）", "debug")

        while _running:
            try:
                # 每轮先确保页面可用（被遮挡/冻结时自动恢复）
                if not page_alive(page):
                    log("检测到页面失联（可能被遮挡/冻结），正在恢复 ...", "warning")
                    page = ensure_page_ready(page, FS["target_chat_name"], ctx)
                    if not page_alive(page):
                        log("恢复失败，等待下一轮重试", "warning")
                        time.sleep(interval)
                        continue

                # 周期性把窗口提到前台，降低被冻结的概率
                bring_to_front(page)

                visible = snapshot_visible(page)
                err_streak = 0  # 本轮读取成功，清零
                # 合并到池
                for m in visible:
                    pool[m["id"]] = m

                if visible:
                    # 按真实时间序整理池
                    all_msgs = sort_by_time(pool.values())

                    # 池只增不减 → 连续运行数小时后"每轮全量排序 + 扫描"会越来越慢，
                    # 且容易把数小时前的旧消息混进上下文。超限即裁剪到最近 N 条。
                    if len(all_msgs) > pool_max:
                        all_msgs = all_msgs[-pool_max:]
                        pool = {m["id"]: m for m in all_msgs}
                        log(f"  （消息池已裁剪至 {len(pool)} 条）", "debug")

                    # ★ 关键修复：找出"最新一道尚未回答的题"
                    #   不再用 DOM 顺序，也不再用"消息 id 未记录"，
                    #   而是用【题目指纹】判重 + 【雪花时间】定序
                    latest_only = RSTRAT.get("latest_only", True)
                    trigger, qnum, body = find_pending_question(
                        all_msgs, answered_fps, latest_only=latest_only,
                        on_skip=_on_fp_skip,
                    )

                    if not trigger:
                        # 没有待答题。有两种可能：
                        #   a) 已答完最新题，等机器人推新题 —— 正常待命；
                        #   b) 机器人只发来"请输入课程名"的提示（无题目）
                        #      → 自动回复课程名，开始下一轮作答。
                        if _maybe_reply_course(all_msgs, state, page):
                            idle_rounds = 0
                            reply_count += 1
                            stats.append({
                                "qnum": None,
                                "question": "[课程名提示]",
                                "trigger": clean_text((all_msgs[-1] or {}).get("text", ""))[:200],
                                "answer": AUTO_COURSE,
                                "answer_full": AUTO_COURSE,
                                "lines": 1,
                            })
                            _save_stats()
                            if ARGS.max_replies > 0 and reply_count >= ARGS.max_replies:
                                log("=" * 62)
                                log(f"已达到 {ARGS.max_replies} 次回复上限，停止。")
                                log("=" * 62)
                                break
                        else:
                            idle_rounds += 1
                            if idle_rounds == 1 or idle_rounds % 12 == 0:
                                newest = all_msgs[-1] if all_msgs else None
                                newest_desc = ""
                                if newest:
                                    newest_desc = clean_text(newest["text"])[:50].replace("\n", " / ")
                                log(f"[待命] 暂无新题，持续监控中… "
                                    f"(已轮询 {idle_rounds} 轮 / DOM {snapshot_visible.last_dom_count} 条 "
                                    f"/ 池 {len(all_msgs)} 条 / 最新: {newest_desc})")
                                # 诊断：把最新的几条 id 打出来，便于对比页面实际内容
                                tail_ids = [f"...{m['id'][-6:]}" for m in all_msgs[-5:]]
                                log(f"       最新5条id: {tail_ids}")
                    else:
                        idle_rounds = 0

                    if trigger:
                        tid = trigger["id"]
                        # 该题已被新题顶掉（超过冷却窗口）→ 重新武装，避免永久熔断
                        if tid in send_fail:
                            newest_id = all_msgs[-1]["id"] if all_msgs else ""
                            if is_superseded(tid, newest_id):
                                send_fail.pop(tid, None)
                                first_answer.pop(tid, None)

                        # 熔断：同一题连续失败达上限就不再重试（等机器人推新题）。
                        # 否则该题永远"未答"→ 每轮重复调模型、重复发送，无上界。
                        if send_fail_action(send_fail.get(tid, 0)) == "skip":
                            trip_logged[0] += 1
                            if trip_logged[0] == 1 or trip_logged[0] % 12 == 0:
                                tag = f"第 {qnum} 题" if qnum is not None else "（无题号）"
                                log(f"  [熔断] 该题（{tag}）已连续 "
                                    f"{send_fail[tid]} 次发送失败，暂停重试，"
                                    f"等待机器人推送新题…", "warning")
                            time.sleep(interval + random.uniform(0, jitter))
                            continue

                        # 上下文窗口：触发消息之前的一段（含触发消息）
                        idx = next(
                            (i for i, x in enumerate(all_msgs) if x["id"] == trigger["id"]),
                            len(all_msgs) - 1,
                        )
                        start = max(0, idx - context_size + 1)
                        window = all_msgs[start:idx + 1]

                        # 去噪（丢弃催答题提醒等干扰）
                        window_clean = denoise(window, drop_reminders=True)
                        # 触发消息本身若未被去噪保留（如被当作提醒），补回
                        if not any(x["id"] == trigger["id"] for x in window_clean):
                            window_clean.append({
                                **trigger,
                                "text": clean_text(trigger["text"]),
                                "type": classify(clean_text(trigger["text"])),
                            })

                        current = {"id": trigger["id"], "text": trigger["text"],
                                   "qnum": qnum, "body": body}
                        context_str = build_context(
                            window_clean, max_chars=max_chars, current=current
                        )
                        _qtag = f"第 {qnum} 题" if qnum is not None else "（无题号）"
                        log(f"[第 {reply_count + 1} 次] 待答题: {_qtag} | "
                            f"{(body or '')[:60]}")
                        log(f"  触发消息: {clean_text(trigger['text'])[:90].replace(chr(10),' / ')}")
                        log(f"  上下文 {len(window_clean)} 条 / {len(context_str)} 字符（已去噪压缩）")

                        # 上次没发出去 → 复用同一份答案重试（不再调模型）。
                        # 否则重试时模型可能给出不同答案，同一题反复发送不同内容。
                        _prev = first_answer.get(tid)
                        if _prev:
                            log("  （该题上次未发出，复用同一答案重试，不再调用模型）", "warning")
                            answer = _prev
                        else:
                            answer = ask_llm(context_str)

                        if not answer:
                            log("  生成失败，下轮重试", "warning")
                            time.sleep(interval)
                            continue

                        # 兜底：剥掉 ``` 围栏（会被平台判成"资料"而不是答案）
                        if "```" in answer:
                            stripped = strip_code_fences(answer)
                            if stripped:
                                log("  （已自动剥离 ``` 代码围栏，避免被判为资料）", "warning")
                                answer = stripped

                        # 兜底：判断题统一成 true / false（老师端只认英文，中文会被判错）
                        _q = (body or "") or trigger["text"]
                        is_judge = bool(re.search(r"判断\s*[：:]", _q)) or _q.strip().startswith("判断")
                        norm = normalize_true_false(answer, is_judge=is_judge)
                        if norm != answer:
                            log(f"  （判断题已归一化: {answer!r} → {norm!r}）", "warning")
                            answer = norm
                        elif is_judge and re.search(r"正确|错误", answer) and "。" not in answer:
                            log("  ⚠ 判断题回复疑似非英文，请检查", "warning")

                        # 多行回复（如代码题）按行打印，便于在日志里核对格式
                        if "\n" in answer:
                            lines_ = answer.splitlines()
                            log(f"  答案（{len(lines_)} 行，{len(answer)} 字符）:")
                            for ln in lines_[:14]:
                                log(f"    | {ln}")
                            if len(lines_) > 14:
                                log(f"    | ...（共 {len(lines_)} 行）")
                        else:
                            log(f"  答案: {answer[:120]}")

                        # 记住本题首次生成的答案：万一发送失败，重试时复用（防漂移）
                        first_answer.setdefault(tid, answer)

                        fp = question_fingerprint(trigger["text"])
                        fp_s = fp_to_str(fp)
                        if ARGS.dry_run:
                            log("  [干跑] 不发送")
                            if fp_s:
                                answered_fps.add(fp_s)
                                state.setdefault("answered_fps", []).append(fp_s)
                            state["answered"].append(trigger["id"])
                            # 答完一题 → 重新武装"课程名提示"去重（下次提示仍可回复）
                            state["course_replied_fps"] = []
                            save_state(state)
                            reply_count += 1
                        else:
                            delay = random.uniform(REPLY["send_delay_min"], REPLY["send_delay_max"])
                            log(f"  等待 {delay:.1f}s 后发送 ...")
                            time.sleep(delay)
                            if send_reply(page, answer):
                                log("  已发送 ✓")
                                send_fail.pop(tid, None)
                                first_answer.pop(tid, None)
                                if fp_s:
                                    answered_fps.add(fp_s)
                                    state.setdefault("answered_fps", []).append(fp_s)
                                state["answered"].append(trigger["id"])
                                # 答完一题 → 重新武装"课程名提示"去重
                                state["course_replied_fps"] = []
                                save_state(state)
                                reply_count += 1
                            else:
                                send_fail[tid] = send_fail.get(tid, 0) + 1
                                log(f"  发送失败（第 {send_fail[tid]}/{MAX_SEND_FAIL} 次，"
                                    f"重试将复用同一答案）", "error")
                                _save_fail_shot(page, f"(第 {send_fail[tid]} 次失败)")

                        stats.append({
                            "qnum": qnum,
                            "question": (body or "")[:200],
                            "trigger": clean_text(trigger["text"])[:200],
                            "answer": answer[:200],
                            # 完整回复（含多行代码），便于事后核对格式
                            "answer_full": answer[:3000],
                            "lines": len(answer.splitlines()),
                        })
                        _save_stats()

                        if ARGS.max_replies > 0 and reply_count >= ARGS.max_replies:
                            log("=" * 62)
                            log(f"已达到 {ARGS.max_replies} 次回复上限，停止。")
                            log(f"问答记录: {STATS_FILE.name}")
                            log("=" * 62)
                            break

            except Exception as e:
                if is_lost_error(e):
                    log(f"检测到页面失联: {str(e)[:90]}，尝试自动恢复 ...", "warning")
                    err_streak += 1
                    if err_streak > 10:
                        log("连续恢复失败过多，请检查浏览器窗口是否被关闭", "error")
                    try:
                        page = ensure_page_ready(page, FS["target_chat_name"], ctx)
                    except Exception:
                        pass
                else:
                    log(f"循环出错: {str(e)[:120]}", "error")
                    # 异常可能恰好发生在发送流程中途（清框 / 插入 / 回车之间），
                    # 此时输入框里可能残留半截内容；下一轮会读到"残留 + 新内容"
                    # （例如 "BC"），与期望内容一直对不上 → 持续无法作答。
                    # 这里无条件清一次（安全侧动作，不会造成错答）。
                    try:
                        _clear_input_box(page)
                        log("  已清空输入框残留内容", "info")
                    except Exception:
                        pass

            time.sleep(interval + random.uniform(0, jitter))

        log("收到停止信号，关闭 ...")
        try:
            ctx.close()
        except Exception:
            pass
        log("已退出。")
        _shutdown_logging()


def _shutdown_logging():
    """
    退出前刷写并关闭日志 handler。

    Windows 上若不解绑，解释器清理阶段仍可能有日志写入，
    而此时文件流已被关闭 —— 会打印 "--- Logging error ---"。
    """
    try:
        for h in list(logger.handlers):
            try:
                h.flush()
            except Exception:
                pass
            try:
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)

        # 同时清理根 logger 上可能存在的 handler，避免残留写入
        root = logging.getLogger()
        for h in list(root.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            root.removeHandler(h)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        _shutdown_logging()
