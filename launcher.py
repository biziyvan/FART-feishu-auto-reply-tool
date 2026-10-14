"""
飞书自动回复助手 · 图形启动器
==============================
面向不熟悉命令行的使用者：
  - 首次运行：弹出配置向导，填写飞书地址 / 会话名 / API Key
  - 之后运行：一个主控窗口，点按钮即可启动、停止监控，查看日志

打包后作为 exe 入口（见 build.spec）。
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk

import yaml

import runtime
from runtime import APP_DIR, DATA_DIR, IS_FROZEN, config_path

try:
    # 接口自适应（settings.py）。用 try 包住是为了兼容
    # "新版引擎 + 旧版外部逻辑目录"：旧 settings.py 里还没有这两个名字，
    # 缺了只影响向导里的一行提示，不能让整个启动器起不来。
    from settings import PROVIDER_LABELS, detect_provider, provider_from_url
except Exception:  # pragma: no cover
    PROVIDER_LABELS = {}
    def detect_provider(*_a, **_k):
        return ""
    def provider_from_url(*_a, **_k):
        return ""

APP_TITLE = "飞书自动回复助手"
CONFIG_FILE = DATA_DIR / "config.yaml"
EXAMPLE_FILE = APP_DIR / "config.example.yaml"
PID_FILE = DATA_DIR / ".launcher.pid"
LIVE_LOG = DATA_DIR / "live.log"   # 打包模式下子进程的实时输出
ERROR_LOG = DATA_DIR / "launcher_error.log"

# ============================================================
# 内嵌默认配置（最终兜底）
# ------------------------------------------------------------
# 首次配置时需要一份"完整"的配置基底（含 system_prompt、轮询、
# 上下文等全部默认项），否则写出的 config.yaml 会缺键，主程序行为
# 异常。正常情况下读外部的 config.example.yaml；一旦它丢失
# （例如打包时被漏掉），这里保证仍能写出完整可用的配置。
# 修改默认值时，请与 config.example.yaml 保持同步。
# ============================================================
EMBEDDED_EXAMPLE = """
feishu:
  base_url: "https://YOUR_TENANT.feishu.cn/next/messenger/"
  target_chat_name: "AI教学平台"

browser:
  channel: "msedge"
  user_data_dir: "./.browser_data"
  headless: false
  slow_mo: 200
  viewport_width: 1440
  viewport_height: 900

polling:
  interval: 5
  jitter: 2

context:
  context_size: 30
  max_chars: 3000
  pool_max: 1200

reply_strategy:
  latest_only: true
  auto_reply_course: "智能合约"
  auto_reply_course_cooldown: 120

llm:
  provider: "auto"
  base_url: "https://api.siliconflow.cn/v1"
  api_key: "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  model: "Qwen/Qwen3.5-35B-A3B"
  thinking: "disabled"
  enable_thinking: false
  system_prompt: |
    你是答题助手，替学生回复「AI教学平台」教学机器人。

    我会把我和机器人的最近对话粘贴给你。请根据对话，判断学生下一条应该发送什么。

    【判断规则】
    - 对话中最后一条是机器人消息。要根据它判断该回什么。
    - 「AI 自适应问答」或「当前题目：」+ 题目（可能带 A/B/C/D 选项）→ 这是正式题目，给出答案。
    - 题干以「判断：」开头（要判断某个说法对/错）→ 判断题，**只回英文 true 或 false**。
    - 「学习反馈」+「答对了」→ 说明答对了，按其中的新要求作答（如"说说...""解释..."）。
    - 「学习反馈」+「答错了」+ 追问 → 针对追问给出正确答案，不要重复老答案。
    - ⚠️ 卡片里的「⚠️ 需切换课程，直接发新的课程名（如「密码学基础」）」
      只是平台的固定提示，**不要回复课程名**，只回答题目本身。
    - ⚠️ 最重要：题目后面列出的 A、B、C、D 就是**可选项**，选择题必须从这些字母里选一个。
      「学习反馈」里那句"你的回答……/正确做法……"是在评价**上一道旧题**，
      **不要照着它作答**；评价之后标着"第 N 题"的那个才是这次要答的新题。

    【代码题】（题目要求"编写代码""写一段代码""实现某逻辑"等）
    - 目的只是过评分，不追求写出可运行、完整、规范的工程代码。
    - ⚠️ 直接给出代码本身，**绝对不要用 ``` 代码围栏**。飞书会把 ``` 渲染成
      「代码块」，教学平台会因此把整条消息判成"资料"而不是答案（会回一句
      "这看起来是一段资料，不是答案"），等于这题白答。就把代码当普通文字发出去。
    - 代码里一行注释都不要写。
    - 省略一切非必要的东西：不要异常处理、不要参数校验、不要日志、
      不要拆辅助函数、不要设计模式、不要 main 示例；变量名简短直白，
      能看懂就行。写得像学生随手交的作业，而不是正式项目代码或教程。
    - 语言用题目或上下文里出现的那门语言，不要引入题目没提到的库。
    - 代码后面必须紧跟一句话，说明关键几行的作用
      （机器人明确要求"说明关键行的作用"，缺了会被判无效作答）；
      不要逐行讲解，不要写成小作文。
    - 篇幅不设硬限制，以"刚好把题目要求交代清楚"为准，能短不长。
      格式示例（仅示范精简程度 + "不要围栏"，代码内容不要照搬）：
      function transfer(address to, uint amount) public {
          require(balances[msg.sender] >= amount);
          balances[msg.sender] -= amount;
          balances[to] += amount;
      }
      require 校验余额是否够，后两行分别扣减付款方、增加收款方。

    【输出格式】（必须严格遵守）
    - 选择题：**只输出一个选项字母**（A、B、C、D 中的一个）。
      不要输出选项的文字、不要写成"B. xxx"、不要加任何解释或标点。
      例：✅ 回复 `C` ／ ❌ 回复 `C. 默认情况下 uint 是 uint256`
      即使上下文里没看清选项，也要根据题目按知识判断后**只回一个字母**。
    - 判断题：**只输出英文 true 或 false（全小写）**，绝对不要输出中文。
      平台只认 true / false，中文「正确 / 错误」会被判成错误答案。
      例：✅ 回复 `false` ／ ❌ 回复 `错误`、`正确`
    - 简答题：用一两句话准确作答
    - 代码题：按上面【代码题】规则，**直接给纯文本代码（不要 ``` 围栏）** + 一句话说明
    - 只输出要发送的内容本身，不要解释、不要客套、不要复述题目。
  max_tokens: 1000
  temperature: 0.3
  timeout: 45
  max_retries: 3

reply:
  send_delay_min: 2
  send_delay_max: 6
  dedup: true
"""


# ============================================================
# 全局异常兜底
# ------------------------------------------------------------
# 窗口程序（console=False）没有可用的 stderr，Tk 回调里抛出的异常
# 会被完全吞掉 —— 用户看到的现象就是"点了按钮毫无反应"。
# 这里接管回调异常：写入 launcher_error.log 并弹窗提示。
# ============================================================
def _native_msgbox(title, text, icon=0x40):
    """用 Windows 原生消息框提示（不依赖 Tk）。

    启动阶段 Tk 可能尚未就绪、或窗口因异常没能显示出来，这时 Tk 的
    messagebox 同样弹不出来，用户就什么都看不到。原生消息框始终可靠。
    icon: 0x10=错误 0x30=警告 0x40=信息；0x40000=置顶
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), icon | 0x40000)
    except Exception:
        pass


def install_exception_hook(root):
    def _hook(exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{text}")
        except Exception:
            pass

        # ⚠️ 本 hook 会被依次装到配置向导（Toplevel）与主窗口上，每次安装都覆盖
        #    report_callback_exception。向导 destroy 之后若其残留 hook 仍被触发，
        #    parent 指向的窗口已不存在 → showerror 抛 TclError → 被 except 吞掉，
        #    于是"最后一次错误"反而什么都看不到。故加存在性判断 + 原生消息框兜底。
        shown = False
        try:
            if root is not None and root.winfo_exists():
                messagebox.showerror(
                    "程序内部错误",
                    f"操作未能完成，发生了一个内部错误：\n\n{type(val).__name__}: {val}\n\n"
                    f"详细信息已写入 launcher_error.log。",
                    parent=root,
                )
                shown = True
        except Exception:
            shown = False
        if not shown:
            _native_msgbox(
                "程序内部错误",
                f"操作未能完成，发生了一个内部错误：\n\n{type(val).__name__}: {val}\n\n"
                f"详细信息已写入：\n{ERROR_LOG}",
                0x10,
            )

    root.report_callback_exception = _hook


def install_global_hooks():
    """把**进程级**未捕获异常也落盘。

    install_exception_hook() 只覆盖 Tk 回调（report_callback_exception）。
    后台线程里未捕获的异常走 threading.excepthook，默认写 stderr ——
    而打包态是窗口程序（console=False），stderr 无处可去，异常就此彻底消失。
    这正是"子进程/线程静默失败"难以定位的根因。
    """
    def _write(kind, exc, val, tb):
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}\n")
                f.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass

    def _sys_hook(exc, val, tb):
        _write("未捕获异常（主线程）", exc, val, tb)
        _native_msgbox(
            "程序内部错误",
            f"{type(val).__name__}: {val}\n\n详细信息已写入：\n{ERROR_LOG}",
            0x10,
        )

    def _thread_hook(args):
        _write(f"后台线程异常（{getattr(args.thread, 'name', '?')}）",
               args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _sys_hook
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass


def load_base_config():
    """读取配置模板作为基底。

    顺序：APP_DIR → DATA_DIR 的 config.example.yaml → 内嵌兜底。
    任何一步出错都静默降级，绝不抛异常（这是首次配置能否成功的关键）。
    返回 dict（可能为空，由调用方补齐）。
    """
    for p in (EXAMPLE_FILE, APP_DIR / "config.example.yaml", DATA_DIR / "config.example.yaml"):
        try:
            if p.exists():
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
        except Exception:
            continue

    # 最终兜底：内嵌配置
    try:
        data = yaml.safe_load(EMBEDDED_EXAMPLE)
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    return {}


# ============================================================
# 工具函数
# ============================================================
def _script_path(name):
    """定位脚本文件。

    顺序：外部逻辑目录（<数据目录>\\app\\，增量更新用）→ 内置资源目录 → 数据目录。
    """
    return runtime.script_path(name)


def _profile_dir():
    """浏览器用户数据目录（与 config.yaml 的 browser.user_data_dir 保持一致）。"""
    rel = "./.browser_data"
    try:
        if CONFIG_FILE.exists():
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            rel = (cfg.get("browser") or {}).get("user_data_dir") or rel
    except Exception:
        pass
    try:
        return (DATA_DIR / rel).resolve()
    except Exception:
        return DATA_DIR / ".browser_data"


def _clean_profile_locks():
    """清理 Chromium 残留的单实例锁文件。

    程序被强杀后这些锁会让下次启动的浏览器认为"已有实例在运行"而立即退出，
    playwright 报 TargetClosedError。
    """
    ud = _profile_dir()
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


def _pid_alive(pid, expect_name=None):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"],
                             capture_output=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = out.stdout.decode("gbk", "replace")
        if str(int(pid)) not in txt:
            return False
        if expect_name:
            return expect_name.lower() in txt.lower()
        return True
    except Exception:
        return False


def _acquire_single_instance():
    """确保同一时间只开一个程序窗口。

    两个窗口同时点「启动监控」会争抢同一个 .browser_data，
    后启动的那个必然报 TargetClosedError。
    返回已在运行的进程号；没有则返回 None 并写入自己的 PID。
    """
    me = os.getpid()
    try:
        if PID_FILE.exists():
            try:
                old = int(PID_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                old = 0
            if old and old != me and _pid_alive(old, Path(sys.executable).name):
                return old
    except Exception:
        pass
    try:
        PID_FILE.write_text(str(me), encoding="utf-8")
    except Exception:
        pass
    return None


def _release_single_instance():
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


# ============================================================
#  设计系统（主题 / 字体 / 自绘控件）
# ============================================================
# 参考 Linear 与 shadcn/ui 的经验：
#   · 画布不用纯白/纯黑；层级靠"表面阶梯"(canvas→surface→surface2) 拉开，
#     而不是靠阴影（Tk 的阴影既贵又不自然）；
#   · 全局只有一个彩色强调色（靛蓝），只出现在主按钮与状态点上；
#   · 1px 细线(hairline)承担所有分隔和描边，比粗边框安静；
#   · 间距走 4px 节奏，正文 10pt、标题 15pt，字重克制。
# 主题**跟随 Windows**（浅色系统→浅色界面），右上角可手动切换；
# 原生标题栏也一起染色，避免顶部出现突兀的白/黑横条。
# ============================================================

PALETTE = {
    "light": {
        "canvas": "#F3F4F7",
        "surface": "#FFFFFF",
        "surface2": "#F7F8FA",
        "surface3": "#EDEFF4",
        "hairline": "#E3E7EE",
        "hairline_strong": "#CBD2DD",
        "ink": "#111319",
        "ink_muted": "#5A6373",
        "ink_subtle": "#8B94A3",
        "accent": "#4F46E5",
        "accent_hover": "#4338CA",
        "accent_press": "#3730A3",
        "accent_soft": "#EEF0FE",
        "accent_ink": "#FFFFFF",
        "success": "#0E9F6E",
        "success_soft": "#E7F6EF",
        "warn": "#B45309",
        "warn_soft": "#FCF2E4",
        "danger": "#D64545",
        "danger_soft": "#FCECEC",
        "log_bg": "#FBFCFE",
        "log_ink": "#2C3342",
        "scroll_track": "#E8EBF1",
        "scroll_thumb": "#C2C9D6",
        "scroll_thumb_hover": "#9FA9BA",
    },
    "dark": {
        "canvas": "#0C0D11",
        "surface": "#131519",
        "surface2": "#191B21",
        "surface3": "#22252C",
        "hairline": "#262A32",
        "hairline_strong": "#363B45",
        "ink": "#F2F3F6",
        "ink_muted": "#A6ADBA",
        "ink_subtle": "#6D7480",
        "accent": "#7C83FF",
        "accent_hover": "#8F95FF",
        "accent_press": "#6A71F0",
        "accent_soft": "#1D2033",
        "accent_ink": "#0B0C10",
        "success": "#3DD68C",
        "success_soft": "#12241C",
        "warn": "#F5A524",
        "warn_soft": "#2A2011",
        "danger": "#F87171",
        "danger_soft": "#2A1517",
        "log_bg": "#0F1115",
        "log_ink": "#C9D1DC",
        "scroll_track": "#0A0B0F",
        "scroll_thumb": "#2E333D",
        "scroll_thumb_hover": "#404755",
    },
}


def _system_prefers_light():
    """读 Windows「应用主题」设置：1=浅色，0=深色；读不到时按浅色。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return bool(val)
    except Exception:
        return True


def _blend(c1, c2, t):
    """两个 #rrggbb 线性插值（t=0 取 c1，t=1 取 c2）；用于模拟透明度。"""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(
            max(0, min(255, int(a[i] + (b[i] - a[i]) * t))) for i in range(3)
        )
    except Exception:
        return c1


class Theme:
    """全局主题。颜色用属性访问：`T.ink` / `T.surface` / `T.accent`。"""

    def __init__(self):
        mode = str(os.environ.get("FEISHU_UI_THEME", "")).strip().lower()
        if mode not in PALETTE:
            mode = "light" if _system_prefers_light() else "dark"
        self.mode = mode
        self.c = dict(PALETTE[self.mode])
        self._subs = []
        self._roots = []      # 需要跟着换肤的窗口（含所有子控件）
        self._init_default_fonts()

    def register_root(self, win):
        """登记一个窗口：换主题时整棵树按旧色→新色重映射。"""
        self._roots.append(win)

    def __getattr__(self, name):
        c = self.__dict__.get("c") or {}
        if name in c:
            return c[name]
        raise AttributeError(name)

    def _init_default_fonts(self):
        ui = "Microsoft YaHei UI"
        self.font_title = (ui, 15, "bold")
        self.font_h2 = (ui, 12, "bold")
        self.font_body = (ui, 10)
        self.font_btn = (ui, 10)
        self.font_small = (ui, 9)
        self.font_tiny = (ui, 8)
        self.font_mono = ("Consolas", 9)
        self.font_mono_sm = ("Consolas", 8)
        self.font_metric = (ui, 19, "bold")

    def init_fonts(self, root):
        """探测系统字体：Win11 的 Segoe UI Variable / Cascadia Mono 优先。"""
        try:
            fams = set(tkfont.families(root))
        except Exception:
            fams = set()

        def pick(cands, fallback):
            for c in cands:
                if c in fams:
                    return c
            return fallback

        ui = pick(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"],
                  "Microsoft YaHei UI")
        disp = pick(["Segoe UI Variable Display", ui], ui)
        mono = pick(["Cascadia Mono", "Consolas", "Courier New"], "Consolas")
        self.font_title = (disp, 15, "bold")
        self.font_h2 = (ui, 12, "bold")
        self.font_body = (ui, 10)
        self.font_btn = (ui, 10)
        self.font_small = (ui, 9)
        self.font_tiny = (ui, 8)
        self.font_mono = (mono, 9)
        self.font_mono_sm = (mono, 8)
        self.font_metric = (disp, 19, "bold")

    def subscribe(self, fn):
        self._subs.append(fn)

    def unsubscribe(self, fn):
        if fn in self._subs:
            self._subs.remove(fn)

    def toggle(self):
        self.set_mode("dark" if self.mode == "light" else "light")

    def _retheme_widget(self, w, mapping):
        """递归把一棵控件树的颜色从旧主题换成新主题。

        容器（Frame / Toplevel / Text）往往是用 `bg=T.xxx` 直接建的，
        没进过 themed() 登记表；只改"登记过的"控件会留下色块。
        这里按"颜色值"配对，凡是用过调色板的控件都会跟着换。
        """
        try:
            if not w.winfo_exists():
                return
        except Exception:
            return
        for opt in ("background", "foreground", "insertbackground",
                    "highlightbackground", "highlightcolor"):
            try:
                cur = str(w.cget(opt)).lower()
            except Exception:
                continue
            new = mapping.get(cur)
            if new:
                try:
                    w.configure(**{opt: new})
                except Exception:
                    pass
        try:
            for ch in w.winfo_children():
                self._retheme_widget(ch, mapping)
        except Exception:
            pass

    def set_mode(self, mode):
        if mode not in PALETTE:
            return
        old = dict(self.c)
        self.mode = mode
        self.c = dict(PALETTE[mode])
        # ⚠️ 映射是**按颜色值**建的，而浅色下 surface 与 accent_ink 同为 #FFFFFF
        #    —— 谁后写谁生效。若 accent_ink 后写，所有 `bg=surface` 的容器
        #    （面板内工具条 / 配置行 / 滚动画布…）在深色下会被染成 accent_ink 的
        #    #0B0C10，比卡片底色 #131519 更黑，出现一块块脏色。
        #    accent_ink 只用于 Canvas 绘制（从不作为控件 bg/fg），所以让「文字色」
        #    先写、「底色」后写，冲突时按底色还原。
        _INK_ROLES = ("ink", "accent_ink", "ink_muted", "ink_subtle")
        _items = sorted(old.items(), key=lambda kv: 0 if kv[0] in _INK_ROLES else 1)
        mapping = {v.lower(): self.c[k] for k, v in _items
                   if k in self.c and self.c[k] != v}
        alive_roots = []
        for w in self._roots:
            try:
                if w.winfo_exists():
                    self._retheme_widget(w, mapping)
                    alive_roots.append(w)
            except Exception:
                pass
        self._roots = alive_roots
        alive = []
        for fn in list(self._subs):
            try:
                fn()
                alive.append(fn)
            except Exception:
                pass   # 控件已销毁 → 顺手从订阅表里摘掉，避免越积越多
        self._subs = alive


T = Theme()

# 普通 tk 控件的主题登记表：换主题时统一改 bg/fg
_THEMED = []


def _apply_theme_to(widget, roles):
    args = {}
    for i, r in enumerate(roles):
        if not r:
            continue
        val = getattr(T, r, None)
        if val is None:
            continue
        if i == 0:
            args["bg"] = val
        elif i == 1:
            args["fg"] = val
        elif i == 2:
            args["insertbackground"] = val
    if args:
        try:
            widget.configure(**args)
        except Exception:
            pass


def themed(widget, *roles):
    """把普通 Tk 控件登记进主题：`themed(lbl, "surface", "ink_muted")`。"""
    entry = (widget, roles)
    _THEMED.append(entry)
    _apply_theme_to(widget, roles)
    # 控件销毁时自动反注册：_THEMED 里的强引用会阻止 GC，长时间开关窗口会一直涨
    try:
        widget.bind("<Destroy>", lambda _e, en=entry: _unthemed(en), add="+")
    except Exception:
        pass
    return widget


def _unthemed(entry):
    try:
        _THEMED.remove(entry)
    except ValueError:
        pass


def _retheme_all():
    for item in list(_THEMED):
        w, roles = item
        try:
            if not w.winfo_exists():
                _THEMED.remove(item)
                continue
        except Exception:
            continue
        _apply_theme_to(w, roles)


T.subscribe(_retheme_all)


def apply_titlebar(root, mode=None):
    """把原生标题栏染成主题色 + 圆角（Win11）。

    否则浅色画布上面顶着一条灰白标题栏、深色画布顶着黑条，观感割裂。
    老系统没有这些 DWM 属性 —— 静默忽略即可。
    """
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            return
        mode = mode or T.mode
        dark = ctypes.c_int(1 if mode == "dark" else 0)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)   # 深色标题栏
        corner = ctypes.c_int(2)                                     # 圆角窗口
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        cap = T.canvas.lstrip("#")
        bgr = ctypes.c_int(int(cap[4:6] + cap[2:4] + cap[0:2], 16))  # COLORREF=BGR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bgr), 4)    # 标题栏底色
    except Exception:
        pass


# ---------- 高 DPI ----------
# 不声明"DPI 感知"时，Windows 会把整个窗口当位图拉伸（150% 缩放下字会发虚，
# 窗口边框也会比程序自报的大一圈）。所以：启动时按真实 DPI 渲染，
# 再把所有像素尺寸按同一比例放大，保证布局比例与设计稿一致。
UI = {"scale": 1.0}


def px(v):
    """按系统 DPI 缩放一个像素值（字体由 Tk 自己按点缩放，无需处理）。"""
    s = UI["scale"]
    return int(round(v * s))


def enable_dpi_awareness():
    """声明 DPI 感知并记录缩放比。**必须在创建任何 Tk 窗口之前调用**。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 系统 DPI 感知
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(
                ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None), 88)
        if dpi and 48 <= dpi <= 480:
            UI["scale"] = round(dpi / 96.0, 3)
    except Exception:
        UI["scale"] = 1.0
    return UI["scale"]


# ---------- 绘图基元 ----------

def _clip(s, width):
    """按"显示宽度"截断字符串（中文/全角算 2 格），等宽字体下才不会被折行。"""
    out, w = "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E7F else 1
        if w + cw > width:
            return out + "…"
        out += ch
        w += cw
    return out


def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    """Canvas 圆角矩形（用 smooth 样条），返回 item id。"""
    r = max(0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, splinesteps=18, **kw)


# ---------------- 滚动条 ----------------
_SCROLL_STYLE = "Feishu.Vertical.TScrollbar"
_scroll_flat_done = False


def _style_scrollbar():
    """给 ttk 滚动条配一套跟随主题的扁平样式。

    ⚠️ Windows 下 ttk 默认走 vista 原生主题，`background`/`troughcolor` 一律被忽略，
    所以深色模式下滚动条会亮着一块白 —— 必须切到 clam 主题才能自己上色。
    本程序只有这一个 ttk 控件，切主题不会影响别处。
    """
    global _scroll_flat_done
    try:
        st = ttk.Style()
        if str(st.theme_use()) != "clam":
            st.theme_use("clam")
        st.configure(
            _SCROLL_STYLE,
            troughcolor=T.scroll_track, background=T.scroll_thumb,
            darkcolor=T.scroll_thumb, lightcolor=T.scroll_thumb,
            bordercolor=T.scroll_track, arrowcolor=T.scroll_thumb,
            relief="flat", borderwidth=0, arrowsize=px(9), width=px(10),
        )
        st.map(_SCROLL_STYLE, background=[
            ("pressed", T.accent), ("active", T.scroll_thumb_hover)])
        try:
            # 去掉滑块上的"抓手"纹（clam 默认会画三道横线，10px 宽下像噪点）
            st.configure(_SCROLL_STYLE, gripcount=0)
        except Exception:
            pass
        if not _scroll_flat_done:
            # 去掉两端箭头按钮，做成现代扁平滚动条（失败就退回带箭头的版本，仍已上色）
            try:
                st.layout(_SCROLL_STYLE, [
                    ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]}),
                ])
            except Exception:
                pass
            _scroll_flat_done = True
    except Exception:
        pass


class RScrollbar(ttk.Scrollbar):
    """跟随主题的扁平滚动条（替代 ttk.Scrollbar，后者在深色下仍是白的）。"""

    def __init__(self, master, command=None, **kw):
        _style_scrollbar()
        super().__init__(master, orient="vertical", style=_SCROLL_STYLE,
                         command=command, **kw)
        T.subscribe(self._on_theme)

    def _on_theme(self):
        _style_scrollbar()
        # ⚠️ 这里不能用 update_idletasks()：它是在主题回调里跑的，
        # 会重入事件循环（可能把 <Configure> 链条再触发一遍）。
        # 用 after_idle 排到空闲队列里，安全且同样能催出重绘。
        try:
            self.after_idle(lambda: None)
        except Exception:
            pass


def _attach_text_scrollbar(parent, widget, bg_role="log_bg"):
    """给一个 Text 配好滚动条并排好版，返回滚动条控件。"""
    sb = RScrollbar(parent, command=widget.yview)
    widget.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    return sb


class RButton(tk.Canvas):
    """自绘圆角按钮。

    Tk 原生 Button 在 Windows 上改不了圆角与配色，故整体自绘。
    保留 `.config(state=...)` / `.config(text=...)` 接口，
    使原有逻辑代码（`btn_start.config(state="disabled")`）无需改动。

    variant: primary（实心强调） / secondary（描边） / danger（浅红底） / ghost（纯文字）
    """

    def __init__(self, master, text="", command=None, variant="secondary",
                 icon="", width=None, height=34, radius=9, font=None, pad=17):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0,
                         bg=self._pb(), height=px(height))
        self._text = text
        self._icon = icon
        self._command = command
        self._variant = variant
        self._radius = px(radius)
        self._font = font or T.font_btn
        self._pad = px(pad)
        self._state = "normal"
        self._hover = False
        self._press = False
        self._fixed_w = px(width) if width else None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._ev_enter)
        self.bind("<Leave>", self._ev_leave)
        self.bind("<ButtonPress-1>", self._ev_press)
        self.bind("<ButtonRelease-1>", self._ev_release)
        T.subscribe(self._on_theme)
        self._autosize()

    # ---- 主题 ----
    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
        except Exception:
            pass
        self._draw()

    # ---- 事件 ----
    def _ev_enter(self, _e):
        if self._state != "disabled":
            self._hover = True
            self._draw()

    def _ev_leave(self, _e):
        self._hover = False
        self._press = False
        self._draw()

    def _ev_press(self, _e):
        if self._state != "disabled":
            self._press = True
            self._draw()

    def _ev_release(self, e):
        was = self._press
        self._press = False
        self._draw()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if was and inside and self._state != "disabled" and self._command:
            self._command()

    # ---- 外观 ----
    def _label(self):
        return (self._icon + "  " + self._text).strip() if self._icon else self._text

    def _colors(self):
        dis = self._state == "disabled"
        v = self._variant
        if v == "primary":
            if dis:
                return T.surface3, T.ink_subtle, None
            bg = T.accent_press if self._press else (T.accent_hover if self._hover else T.accent)
            return bg, T.accent_ink, None
        if v == "danger":
            if dis:
                return T.surface2, T.ink_subtle, T.hairline
            bg = _blend(T.danger_soft, T.danger, 0.16) if (self._hover or self._press) else T.danger_soft
            return bg, T.danger, None
        if v == "ghost":
            if dis:
                return None, T.ink_subtle, None
            return (T.surface3 if (self._hover or self._press) else None), \
                   (T.ink if self._hover else T.ink_muted), None
        if dis:
            return T.surface2, T.ink_subtle, T.hairline
        return (T.surface3 if (self._hover or self._press) else T.surface2), T.ink, T.hairline

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        bg, fg, outline = self._colors()
        if bg is None:
            bg = self._pb()
        _round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._radius,
                    fill=bg, outline=outline or bg)
        self.create_text(w / 2, h / 2 + 0.5, text=self._label(), fill=fg, font=self._font)

    def _autosize(self):
        try:
            w = self._fixed_w or int(tkfont.Font(font=self._font).measure(self._label()) + self._pad * 2)
        except Exception:
            w = self._fixed_w or 100
        try:
            self.configure(width=w)
        except Exception:
            pass
        self._draw()

    # ---- 兼容 tk.Button 的接口 ----
    def config(self, **kw):
        if hasattr(self, "_state"):
            if "state" in kw:
                self._state = kw.pop("state")
            if "text" in kw:
                self._text = kw.pop("text")
                self._autosize()
            if "command" in kw:
                self._command = kw.pop("command")
            if kw:
                super().config(**kw)
            self._draw()
            return
        super().config(**kw)

    configure = config


class REntry(tk.Canvas):
    """圆角输入框：自绘外框 + 内嵌原生 Entry。

    对外代理 get/insert/delete/bind，用法与 tk.Entry 完全一致，
    所以向导里原有的取值/回填逻辑一行都不用改。
    聚焦时描边变强调色，是最便宜的"被选中"反馈。
    """

    def __init__(self, master, mono=False, show=None, height=38, radius=9, pad=12):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0, bg=self._pb(),
                         height=px(height))
        self._radius = px(radius)
        self._pad = px(pad)
        self._h = px(height)
        self._focus = False
        self.entry = tk.Entry(
            self, bd=0, highlightthickness=0, relief="flat",
            bg=T.surface2, fg=T.ink, insertbackground=T.ink,
            font=(T.font_mono if mono else T.font_body),
            show=show or "",
        )
        self._win = self.create_window(self._pad, self._h / 2, window=self.entry,
                                       anchor="w", width=px(80))
        self.entry.bind("<FocusIn>", self._ev_focus_in)
        self.entry.bind("<FocusOut>", self._ev_focus_out)
        tk.Canvas.bind(self, "<Button-1>", lambda _e: self.entry.focus_set())
        tk.Canvas.bind(self, "<Configure>", lambda _e: self._layout())
        T.subscribe(self._on_theme)
        self._layout()

    # ---- 主题 / 布局 ----
    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
            self.entry.configure(bg=(T.surface if self._focus else T.surface2),
                                 fg=T.ink, insertbackground=T.ink)
        except Exception:
            pass
        self._layout()

    def _ev_focus_in(self, _e):
        self._focus = True
        self._layout()

    def _ev_focus_out(self, _e):
        self._focus = False
        self._layout()

    def _layout(self):
        w = self.winfo_width()
        if w <= 1:
            return
        # ⚠️ 必须显式走 Canvas.delete：本类的 delete() 已代理给内嵌 Entry，
        # 直接 self.delete("frame") 会被解析成 entry.delete("frame") → TclError。
        tk.Canvas.delete(self, "frame")
        fill = T.surface if self._focus else T.surface2
        outline = T.accent if self._focus else T.hairline
        _round_rect(self, 0.5, 0.5, w - 0.5, self._h - 0.5, self._radius,
                    fill=fill, outline=outline, tags="frame")
        try:
            self.entry.configure(bg=fill)
            self.itemconfigure(self._win, width=max(1, w - self._pad * 2))
        except Exception:
            pass

    # ---- 代理给内嵌 Entry ----
    def get(self):
        return self.entry.get()

    def insert(self, index, string):
        return self.entry.insert(index, string)

    def delete(self, first, last=None):
        return self.entry.delete(first, last)

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def focus_set(self):
        try:
            self.entry.focus_set()
        except Exception:
            pass


class Pill(tk.Canvas):
    """胶囊标签：状态、指标都用它。kind: neutral/accent/success/warn/danger。"""

    def __init__(self, master, text="", kind="neutral", dot=False,
                 height=24, radius=12, font=None, pad=11):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0, bg=self._pb(),
                         height=px(height))
        self._text = text
        self._kind = kind
        self._dot = dot
        self._radius = px(radius)
        self._font = font or T.font_small
        self._pad = px(pad)
        self._dot_size = px(7)
        self.bind("<Configure>", lambda _e: self._draw())
        T.subscribe(self._on_theme)
        self._autosize()

    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
        except Exception:
            pass
        self._draw()

    def _colors(self):
        return {
            "neutral": (T.surface3, T.ink_muted),
            "accent": (T.accent_soft, T.accent),
            "success": (T.success_soft, T.success),
            "warn": (T.warn_soft, T.warn),
            "danger": (T.danger_soft, T.danger),
        }.get(self._kind, (T.surface3, T.ink_muted))

    def set(self, text=None, kind=None):
        if text is not None:
            self._text = text
        if kind is not None:
            self._kind = kind
        self._autosize()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        bg, fg = self._colors()
        _round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._radius, fill=bg, outline=bg)
        x = self._pad
        if self._dot:
            self.create_oval(x, h / 2 - self._dot_size / 2, x + self._dot_size,
                             h / 2 + self._dot_size / 2, fill=fg, outline=fg)
            x += self._dot_size + px(6)
        self.create_text(x, h / 2 + 0.5, text=self._text, fill=fg,
                         font=self._font, anchor="w")

    def _autosize(self):
        try:
            tw = tkfont.Font(font=self._font).measure(self._text)
        except Exception:
            tw = 60
        w = int(tw + self._pad * 2 + (self._dot_size + px(6) if self._dot else 0))
        try:
            self.configure(width=w)
        except Exception:
            pass
        self._draw()


class StatusDot(tk.Canvas):
    """状态圆点：运行中做呼吸光晕，静止时不闪。"""

    def __init__(self, master, size=13, color=None, animate=False):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0, bg=self._pb(),
                         width=px(size), height=px(size))
        self._size = px(size)
        self._color = color or T.ink_subtle
        self._animate = animate
        self._phase = 0
        self._cycle = 22
        self.bind("<Configure>", lambda _e: self._paint())
        # 窗口销毁后 70ms 的 after 若继续空转，会一直重绘已不存在的控件
        self.bind("<Destroy>", self._on_destroy, add="+")
        T.subscribe(self._on_theme)
        if animate:
            self._tick()

    def _on_destroy(self, _e=None):
        self._animate = False

    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
        except Exception:
            pass
        self._paint()

    def set(self, color, animate=None):
        self._color = color
        if animate is not None and animate != self._animate:
            self._animate = animate
            if animate:
                self._tick()
        self._paint()

    def _tick(self):
        if not self._animate:
            return
        self._phase = (self._phase + 1) % self._cycle
        self._paint()
        try:
            self.after(70, self._tick)
        except Exception:
            pass

    def _paint(self):
        try:
            self.delete("all")
            size = self._size
            bg = self._pb()
            cx = cy = size / 2.0
            if self._animate:
                t = self._phase / float(self._cycle)
                ring = _blend(self._color, bg, 0.55 + 0.4 * t)
                r = size / 2.0 * (0.62 + 0.38 * t)
                self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ring, outline=ring)
            r0 = size / 2.0 * 0.42
            self.create_oval(cx - r0, cy - r0, cx + r0, cy + r0,
                             fill=self._color, outline=self._color)
        except Exception:
            pass


class SegTabs(tk.Canvas):
    """分段页签（日志 / 流水 / 配置）。外层浅底，选中项是一枚浮起的小白片。"""

    def __init__(self, master, items, command=None, height=36, radius=10,
                 font=None, pad=18, inner=3):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0, bg=self._pb(),
                         height=px(height))
        self._items = list(items)
        self._command = command
        self._radius = px(radius)
        self._font = font or T.font_body
        self._pad = px(pad)
        self._inner = px(inner)
        self._active = 0
        self._hover = -1
        self._widths = []
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._ev_click)
        self.bind("<Motion>", self._ev_motion)
        self.bind("<Leave>", lambda _e: self._set_hover(-1))
        T.subscribe(self._on_theme)
        self._autosize()

    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
        except Exception:
            pass
        self._draw()

    def _measure(self):
        try:
            f = tkfont.Font(font=self._font)
            self._widths = [int(f.measure(t) + self._pad * 2) for t in self._items]
        except Exception:
            self._widths = [90] * len(self._items)

    def _autosize(self):
        self._measure()
        try:
            self.configure(width=sum(self._widths) + self._inner * 2)
        except Exception:
            pass
        self._draw()

    def _hit(self, x):
        pos = self._inner
        for i, w in enumerate(self._widths):
            if pos <= x <= pos + w:
                return i
            pos += w
        return -1

    def _ev_motion(self, e):
        self._set_hover(self._hit(e.x))

    def _set_hover(self, i):
        if i != self._hover:
            self._hover = i
            self._draw()

    def _ev_click(self, e):
        i = self._hit(e.x)
        if i >= 0:
            self.set_active(i, notify=True)

    def set_active(self, idx, notify=False):
        if 0 <= idx < len(self._items):
            self._active = idx
            self._draw()
            if notify and self._command:
                self._command(idx)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1 or not self._widths:
            return
        _round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._radius,
                    fill=T.surface2, outline=T.hairline)
        x = self._inner
        for i, item in enumerate(self._items):
            iw = self._widths[i]
            if i == self._active:
                _round_rect(self, x + 0.5, self._inner + 0.5, x + iw - 0.5,
                            h - self._inner - 0.5, max(4, self._radius - 3),
                            fill=T.surface, outline=T.hairline)
                fg = T.ink
            else:
                fg = T.ink if i == self._hover else T.ink_muted
            self.create_text(x + iw / 2, h / 2 + 0.5, text=item, fill=fg, font=self._font)
            x += iw


class Panel(tk.Canvas):
    """圆角卡片。内容放在 `.body`（一个 tk.Frame）里。"""

    def __init__(self, master, pad=18, radius=14, height=None,
                 fill="surface", outline="hairline", **kw):
        self._parent = master
        super().__init__(master, highlightthickness=0, bd=0, bg=self._pb(), **kw)
        self._pad = px(pad)
        self._radius = px(radius)
        self._fill_role = fill
        self._outline_role = outline
        if height:
            self.configure(height=px(height))
        self.body = tk.Frame(self, bg=getattr(T, self._fill_role))
        self._win = self.create_window(self._pad, self._pad, window=self.body, anchor="nw")
        self.bind("<Configure>", lambda _e: self._layout())
        T.subscribe(self._on_theme)

    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _on_theme(self):
        try:
            self.configure(bg=self._pb())
            self.body.configure(bg=getattr(T, self._fill_role))
        except Exception:
            pass
        self._layout()

    def _layout(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.delete("panelbg")
        fill = getattr(T, self._fill_role)
        outline = getattr(T, self._outline_role) if self._outline_role else fill
        _round_rect(self, 0.5, 0.5, w - 1, h - 1, self._radius,
                    fill=fill, outline=outline, tags="panelbg")
        self.tag_lower("panelbg")
        try:
            self.itemconfigure(self._win, width=max(1, w - self._pad * 2),
                               height=max(1, h - self._pad * 2))
        except Exception:
            pass


class StepBadge(tk.Canvas):
    """步骤序号徽标（圆角方块 + 数字）。"""

    def __init__(self, master, num, size=22, radius=7):
        self._parent = master
        super().__init__(master, width=px(size), height=px(size),
                         highlightthickness=0, bd=0, bg=self._pb())
        self._num = num
        self._size = px(size)
        self._radius = px(radius)
        self.bind("<Configure>", lambda _e: self._paint())
        T.subscribe(self._paint)
        self._paint()          # 首帧就要画出来（不能只等主题切换）

    def _pb(self):
        try:
            return self._parent.cget("bg")
        except Exception:
            return T.canvas

    def _paint(self):
        try:
            self.delete("all")
            _round_rect(self, 0, 0, self._size, self._size, self._radius,
                        fill=T.accent_soft, outline=T.accent_soft)
            self.create_text(self._size / 2, self._size / 2 + 0.5, text=str(self._num),
                             fill=T.accent, font=T.font_tiny)
        except Exception:
            pass


class HealthRow(tk.Frame):
    """健康检查的一行：彩点 + 名称 + 结论。"""

    def __init__(self, master, label, value="检查中…", kind="neutral"):
        super().__init__(master, bg=T.surface)
        self._dot = StatusDot(self, size=9, color=T.ink_subtle)
        self._dot.pack(side="left", pady=(px(5), 0))
        themed(tk.Label(self, text=label, font=T.font_small, width=0,
                        anchor="w"), "surface", "ink_muted").pack(side="left", padx=(px(9), 0))
        self._val = themed(tk.Label(self, text=value, font=T.font_small, anchor="e"),
                           "surface", "ink")
        self._val.pack(side="right")
        self.bind("<Destroy>", lambda _e: T.unsubscribe(self._on_theme))
        T.subscribe(self._on_theme)

    def _on_theme(self):
        try:
            self.configure(bg=T.surface)
        except Exception:
            pass

    def set(self, value, kind="neutral"):
        color = {
            "ok": T.success, "success": T.success,
            "warn": T.warn, "error": T.danger, "danger": T.danger,
            "accent": T.accent,
        }.get(kind, T.ink_subtle)
        fg = {
            "ok": T.success, "success": T.success,
            "warn": T.warn, "error": T.danger, "danger": T.danger,
        }.get(kind, T.ink)
        try:
            self._dot.set(color, animate=False)
            self._val.configure(text=value, fg=fg)
        except Exception:
            pass


def toast(root, text, kind="info", ms=3000):
    """右下角轻提示。用于"已保存""已复制"这类不需要打断用户的信息。"""
    try:
        color = {"info": T.accent, "ok": T.success, "warn": T.warn, "error": T.danger
                 }.get(kind, T.accent)
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.98)
        except Exception:
            pass
        pad = px(16)
        f = tkfont.Font(font=T.font_body)
        w = int(f.measure(text) + pad * 2 + px(22))
        h = px(40)
        win.configure(bg=T.hairline)
        cv = tk.Canvas(win, width=w, height=h, highlightthickness=0, bd=0, bg=T.hairline)
        cv.pack()
        _round_rect(cv, 0.5, 0.5, w - 0.5, h - 0.5, px(10), fill=T.surface, outline=T.hairline)
        cv.create_oval(pad - px(4), h / 2 - px(4), pad + px(4), h / 2 + px(4),
                       fill=color, outline=color)
        cv.create_text(pad + px(14), h / 2 + 0.5, text=text, fill=T.ink,
                       font=T.font_body, anchor="w")
        root.update_idletasks()
        x = root.winfo_rootx() + root.winfo_width() - w - 26
        y = root.winfo_rooty() + root.winfo_height() - h - 26
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.after(ms, win.destroy)
        return win
    except Exception:
        return None


# ============================================================
# 配置向导（首次运行）
# ============================================================
class ConfigWizard(tk.Toplevel):
    """配置向导。

    必须做成 Toplevel，不能用第二个 tk.Tk()：
    同进程里再建一个 Tk 会把 tkinter._default_root 抢走，
    向导一关，主窗口的控件就变成 "invalid command name"。
    调用方负责提供 master（首次配置时先建一个隐藏根窗口）。
    """

    def __init__(self, master):
        super().__init__(master)
        T.init_fonts(self)
        T.register_root(self)
        self.title(f"{APP_TITLE} · 配置向导")
        self.configure(bg=T.canvas)
        self.geometry(f"{px(680)}x{px(600)}")
        self.resizable(False, False)
        self.result = False
        install_exception_hook(self)
        self._build()
        self.after(40, lambda: apply_titlebar(self))
        # 模态 + 置顶，避免对话框被其它窗口盖住。
        #
        # ⚠️ 关键：只在 master「可见」时才设 transient。
        # Windows/Tk 下，把 Toplevel transient 到一个不可见的 owner（首次配置
        # 时的 root 是 withdraw() 状态）会导致 Tk 根本不 map 这个窗口 ——
        # 现象是「进程在后台跑着，却永远看不到界面」，wait_window() 无限空等。
        try:
            if master is not None and master.winfo_viewable():
                self.transient(master)
        except Exception:
            pass
        try:
            self.grab_set()
        except Exception:
            pass
        self.lift()
        try:
            self.attributes("-topmost", True)
            self.after(600, lambda: self.attributes("-topmost", False))
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _release(self):
        try:
            self.grab_release()
        except Exception:
            pass

    def _cancel(self):
        self.result = False
        self._release()
        self.destroy()

    def _build(self):
        # ---- 顶部：品牌 + 一句话说明（不用大色块，靠留白与字重分层）----
        head = tk.Frame(self, bg=T.canvas)
        head.pack(fill="x", padx=px(26), pady=(px(22), px(0)))

        badge = tk.Canvas(head, width=px(42), height=px(42), bg=T.canvas,
                          highlightthickness=0, bd=0)
        badge.pack(side="left")
        _round_rect(badge, 0, 0, px(42), px(42), px(12), fill=T.accent, outline=T.accent)
        badge.create_text(px(21), px(21) + 0.5, text="飞", fill=T.accent_ink, font=T.font_h2)

        tbox = tk.Frame(head, bg=T.canvas)
        tbox.pack(side="left", padx=(px(13), px(0)))
        themed(tk.Label(tbox, text="先完成 3 项配置", font=T.font_title, anchor="w"),
               "canvas", "ink").pack(anchor="w")
        themed(tk.Label(tbox, text="填好点一下就能用；以后双击程序即可启动，不用再填",
                        font=T.font_small, anchor="w"),
               "canvas", "ink_muted").pack(anchor="w", pady=(px(3), px(0)))

        # ---- 表单卡片 ----
        card = Panel(self, pad=22, radius=14, fill="surface", outline="hairline")
        card.pack(fill="x", padx=px(26), pady=(px(14), px(0)))
        self._card = card
        body = card.body
        # 内容高度一变（展开高级设置等）就重新贴合窗口大小
        body.bind("<Configure>", lambda _e: self._sync_height())

        # ① 飞书地址
        r1 = tk.Frame(body, bg=T.surface)
        r1.pack(fill="x")
        StepBadge(r1, "1").pack(side="left")
        themed(tk.Label(r1, text="飞书网页版地址", font=T.font_h2, anchor="w"),
               "surface", "ink").pack(side="left", padx=(px(9), px(0)))
        themed(tk.Label(body, text="打开飞书网页版，从浏览器地址栏整段复制",
                        font=T.font_small, anchor="w"),
               "surface", "ink_subtle").pack(anchor="w", pady=(px(4), px(6)))
        self.e_url = REntry(body, mono=True)
        self.e_url.pack(fill="x")
        self.e_url.insert(0, "https://你的租户.feishu.cn/next/messenger/")

        # ② 会话名
        r2 = tk.Frame(body, bg=T.surface)
        r2.pack(fill="x", pady=(px(16), px(0)))
        StepBadge(r2, "2").pack(side="left")
        themed(tk.Label(r2, text="机器人的会话名称", font=T.font_h2, anchor="w"),
               "surface", "ink").pack(side="left", padx=(px(9), px(0)))
        themed(tk.Label(body, text="飞书左侧会话列表里显示的名字（例如：AI教学平台）",
                        font=T.font_small, anchor="w"),
               "surface", "ink_subtle").pack(anchor="w", pady=(px(4), px(6)))
        self.e_chat = REntry(body)
        self.e_chat.pack(fill="x")
        self.e_chat.insert(0, "AI教学平台")

        # ③ API Key
        r3 = tk.Frame(body, bg=T.surface)
        r3.pack(fill="x", pady=(px(16), px(0)))
        StepBadge(r3, "3").pack(side="left")
        themed(tk.Label(r3, text="大模型 API Key", font=T.font_h2, anchor="w"),
               "surface", "ink").pack(side="left", padx=(px(9), px(0)))
        themed(tk.Label(body, text="用于生成答案的密钥。硅基流动、DeepSeek 官方都能直接用，参数会自动适配",
                        font=T.font_small, anchor="w"),
               "surface", "ink_subtle").pack(anchor="w", pady=(px(4), px(6)))
        self.e_key = REntry(body, mono=True, show="•")
        self.e_key.pack(fill="x")

        # ---- 高级设置（默认折叠，展开后窗口自动长高）----
        self.adv_open = False
        self.btn_adv = RButton(body, text="▸  高级设置（接口地址 / 模型名）",
                               variant="ghost", height=30, pad=8,
                               font=T.font_small, command=self._toggle_adv)
        self.btn_adv.pack(anchor="w", pady=(px(12), px(0)))

        self.adv_frame = tk.Frame(body, bg=T.surface)
        themed(tk.Label(self.adv_frame, text="接口地址", font=T.font_small, anchor="w"),
               "surface", "ink_muted").pack(anchor="w", pady=(px(10), px(5)))
        self.e_base = REntry(self.adv_frame, mono=True, height=34)
        self.e_base.pack(fill="x")
        self.e_base.insert(0, "https://api.siliconflow.cn/v1")
        themed(tk.Label(self.adv_frame, text="模型名", font=T.font_small, anchor="w"),
               "surface", "ink_muted").pack(anchor="w", pady=(px(10), px(5)))
        self.e_model = REntry(self.adv_frame, mono=True, height=34)
        self.e_model.pack(fill="x")
        self.e_model.insert(0, "Qwen/Qwen3.5-35B-A3B")

        # 接口类型实时提示：一眼看懂"程序会按哪套参数发请求"
        self.lbl_provider = themed(
            tk.Label(self.adv_frame, text="", font=T.font_small, justify="left",
                     wraplength=px(560), anchor="w"),
            "surface", "ink_subtle")
        self.lbl_provider.pack(anchor="w", pady=(px(9), px(0)))
        for _e in (self.e_base, self.e_model):
            _e.bind("<KeyRelease>", lambda _ev: self._refresh_provider())

        # ---- 底部操作栏 ----
        foot = tk.Frame(self, bg=T.canvas)
        foot.pack(fill="x", padx=px(26), pady=px(16))
        self.lbl_status = themed(tk.Label(foot, text="", font=T.font_small, anchor="w"),
                                 "canvas", "ink_muted")
        self.lbl_status.pack(side="left")
        self.btn_save = RButton(foot, text="保存并开始", variant="primary",
                                height=36, command=self._save)
        self.btn_save.pack(side="right")
        self.btn_test = RButton(foot, text="测试连通性", variant="secondary",
                                height=36, command=self._test)
        self.btn_test.pack(side="right", padx=(px(0), px(8)))

        # 已有配置则回填，避免"打开向导 → 直接保存"把原设置冲掉
        self._prefill()
        self.after(60, self._sync_height)

    def _sync_height(self):
        """让窗口高度贴合内容（展开/收起高级设置时也调用）。

        ⚠️ 不能用 self.geometry("")：那会把宽度也一并重置成「子控件请求宽度」，
        窗口会缩到 400 出头，标题栏与按钮跟着错位。宽度必须钉死为 px(680)。
        """
        if getattr(self, "_syncing", False):
            return
        self._syncing = True
        try:
            self.update_idletasks()
            need = self._card.body.winfo_reqheight() + self._card._pad * 2
            if abs(need - self._card.winfo_height()) > 1:
                self._card.configure(height=need)
            self.update_idletasks()
            h = max(px(540), self.winfo_reqheight())
            self.geometry(f"{px(680)}x{h}")
        except Exception:
            pass
        finally:
            self._syncing = False

    def _prefill(self):
        """用现有 config.yaml 的值回填各输入框。

        没有这一步时，向导显示的是硬编码默认值（飞书地址是占位符），
        用户点一次"保存"就会把真实的 base_url / 密钥 覆盖掉。
        """
        try:
            if not CONFIG_FILE.exists():
                return
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        if not isinstance(cfg, dict):
            return
        fs = cfg.get("feishu") or {}
        llm = cfg.get("llm") or {}

        def fill(entry, value):
            if value:
                entry.delete(0, "end")
                entry.insert(0, str(value))

        fill(self.e_url, fs.get("base_url"))
        fill(self.e_chat, fs.get("target_chat_name"))
        fill(self.e_key, llm.get("api_key"))
        fill(self.e_base, llm.get("base_url"))
        fill(self.e_model, llm.get("model"))
        self._refresh_provider()

    def _refresh_provider(self):
        """按当前填写的接口地址/模型名，实时显示程序会用哪套参数。"""
        if not PROVIDER_LABELS:
            return  # 外部逻辑目录是旧版（无此功能），静默跳过
        try:
            p = detect_provider(self.e_base.get().strip(), self.e_model.get().strip(), "")
            label = PROVIDER_LABELS.get(p, p) or "未知"
            self.lbl_provider.config(text=f"接口类型：自动识别 → {label}")
        except Exception:
            pass

    def _toggle_adv(self):
        if self.adv_open:
            self.adv_frame.pack_forget()
            self.btn_adv.config(text="▸  高级设置（接口地址 / 模型名）")
        else:
            self.adv_frame.pack(fill="x")
            self.btn_adv.config(text="▾  高级设置（接口地址 / 模型名）")
        self.adv_open = not self.adv_open
        self.after(20, self._sync_height)

    def _collect(self):
        return {
            "url": self.e_url.get().strip(),
            "chat": self.e_chat.get().strip(),
            "key": self.e_key.get().strip(),
            "base": self.e_base.get().strip(),
            "model": self.e_model.get().strip(),
        }

    def _validate(self, d):
        if not d["url"] or "feishu.cn" not in d["url"]:
            return "飞书地址看起来不对，应形如 https://xxx.feishu.cn/next/messenger/"
        if "你的租户" in d["url"] or "YOUR_TENANT" in d["url"].upper():
            return ("飞书地址还是占位符，请改成你自己的地址"
                    "（形如 https://xxx.feishu.cn/next/messenger/）")
        if not d["url"].startswith("http"):
            return "飞书地址需要以 http:// 或 https:// 开头"
        if not d["chat"]:
            return "请填写机器人会话名称"
        if not d["key"] or len(d["key"]) < 12:
            return "请填写有效的大模型 API Key"
        if not d["base"].startswith("http"):
            return "接口地址需要以 http 开头"
        return None

    def _test(self):
        d = self._collect()
        err = self._validate(d)
        if err:
            messagebox.showwarning("请检查", err, parent=self)
            return

        self.btn_test.config(state="disabled", text="测试中…")
        self.lbl_status.config(text="正在连接模型接口…", fg=T.ink_muted)

        def work():
            # kind 由 _test_done 映射成当前主题对应的颜色（浅色/深色都好看）
            msg, kind = "连接失败", "error"
            try:
                url = d["base"].rstrip("/") + "/models"
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + d["key"]}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    ok = r.status == 200
                msg = "模型接口连通，密钥有效" if ok else f"接口返回状态 {r.status}"
                kind = "ok" if ok else "warn"
            except Exception as e:
                msg = f"连接失败：{str(e)[:90]}"
                kind = "error"
            try:
                self.after(0, lambda: self._test_done(msg, kind))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, msg, kind="ok"):
        color = {"ok": T.success, "warn": T.warn, "error": T.danger}.get(kind, T.ink_muted)
        icon = {"ok": "✓ ", "warn": "! ", "error": "✕ "}.get(kind, "")
        try:
            self.lbl_status.config(text=icon + msg, fg=color)
            self.btn_test.config(state="normal", text="测试连通性")
        except Exception:
            pass  # 窗口可能已被关闭

    def _save(self):
        try:
            d = self._collect()
            err = self._validate(d)
            if err:
                messagebox.showwarning("请检查", err, parent=self)
                return

            # 基底优先用现有 config.yaml（保住用户其它设置与 prompt），
            # 没有或损坏时才退回模板。
            cfg = None
            try:
                if CONFIG_FILE.exists():
                    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                cfg = None
            if not isinstance(cfg, dict):
                cfg = load_base_config()

            # 防御：模板若被破坏成非 dict，退回内嵌默认
            if not isinstance(cfg, dict):
                cfg = {}

            for sect in ("feishu", "browser", "llm", "polling", "context",
                         "reply", "reply_strategy"):
                if not isinstance(cfg.get(sect), dict):
                    cfg[sect] = {}

            cfg["feishu"]["base_url"] = d["url"]
            cfg["feishu"]["target_chat_name"] = d["chat"]
            cfg["llm"]["api_key"] = d["key"]
            cfg["llm"]["base_url"] = d["base"]
            cfg["llm"]["model"] = d["model"]

            # 接口类型归一到 auto（按 base_url 自动识别）。
            # 只在"域名能被识别"时才动：自建中转网关识别不出域名，
            # 用户手填的 provider 要保留，否则会丢掉该发的那套参数。
            try:
                if provider_from_url(d["base"]):
                    exp = str(cfg["llm"].get("provider") or "").strip().lower()
                    if exp in ("", "auto") or PROVIDER_LABELS.get(exp) or exp in (
                        "silicon", "silicon_flow"
                    ):
                        cfg["llm"]["provider"] = "auto"
            except Exception:
                pass

            # 这三项向导里没有对应表单，**不能无条件覆盖**：
            # 用户手改过（换成 Chrome / 改 user_data_dir / 开无头）会被静默回滚；
            # 而且健康检查里登录态写死了 DATA_DIR/.browser_data，
            # 一旦与 user_data_dir 不同源，界面就会显示错误的登录状态、
            # 「清理残留锁」也会清错目录。用 setdefault：只在没设置过时补默认。
            _br = cfg["browser"]
            _br.setdefault("channel", "msedge")
            _br.setdefault("headless", False)
            _br.setdefault("user_data_dir", "./.browser_data")

            # 原子写：临时文件 → 备份 .bak → 原子替换。
            # 直接 open(..., "w") 若中途断电/被杀，会留下 0 字节或半截 YAML；
            # 下次向导 _prefill 静默失败 → 界面显示占位默认值 → 用户被迫全部重填。
            try:
                text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
                tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                try:
                    with open(tmp, "r+b") as _f:
                        _f.flush()
                        os.fsync(_f.fileno())
                except Exception:
                    pass
                if CONFIG_FILE.exists():
                    try:
                        shutil.copyfile(
                            CONFIG_FILE,
                            CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak"),
                        )
                    except Exception:
                        pass
                os.replace(tmp, CONFIG_FILE)
            except Exception as e:
                messagebox.showerror(
                    "保存失败",
                    f"无法写入配置文件：\n{CONFIG_FILE}\n\n{e}\n\n"
                    f"请确认程序所在目录可写（不要放在 C:\\Program Files 等受保护目录）。",
                    parent=self,
                )
                return

            self.result = True
            # 不再弹"配置完成"对话框：直接关掉向导，由主窗口给一个右下角轻提示
            # （少一次打断，也少一次点击）
            self._release()
            self.destroy()
        except Exception:
            # 任何意外都要让用户看见，绝不静默失败
            tb = traceback.format_exc()
            try:
                with open(ERROR_LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 保存配置失败\n{tb}")
            except Exception:
                pass
            try:
                messagebox.showerror(
                    "保存配置失败",
                    f"保存配置时出现意外错误：\n\n{tb.strip().splitlines()[-1]}\n\n"
                    f"详细信息已写入 launcher_error.log。",
                    parent=self,
                )
            except Exception:
                pass


# ============================================================
# 主控窗口
# ============================================================
class MainWindow(tk.Tk):
    # 状态机：把日志里的技术状态翻译成"人话"
    STATE_TEXT = {
        "idle": ("未运行", "程序处于停止状态。点「启动监控」开始答题。", "neutral"),
        "starting": ("启动中", "正在启动浏览器并加载飞书会话；首次会弹出扫码登录。", "warn"),
        "waiting": ("待命中", "正在轮询消息，暂时没有新题 —— 这是正常状态，程序没有卡住。", "accent"),
        "answering": ("作答中", "已收到新题，正在请求大模型生成答案。", "accent"),
        "sent": ("已发送", "答案已发出，正在等待机器人返回学习反馈。", "success"),
        "course": ("已开课", "检测到「请输入课程名」提示，已自动回复课程名开始下一轮。", "accent"),
        "error": ("需要处理", "运行中出现了错误，请看日志里最后一条红色提示。", "danger"),
        "stopped": ("已停止", "监控已结束。点「启动监控」可重新开始。", "neutral"),
    }

    # 兼容两种日志格式：带日期前缀（step2 现为 "%Y-%m-%d %H:%M:%S"）与纯时间
    LOG_RE = re.compile(
        r"^(?:\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2})\s+"
        r"\[(INFO|WARNING|ERROR|DEBUG)\]\s?(.*)$"
    )
    MAX_LOG_LINES = 4000

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        # 尺寸：按设计稿，但绝不超过屏幕（高 DPI + 小屏时会被截到屏幕外）
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(px(1000), int(sw * 0.9))
        h = min(px(720), int(sh * 0.9))
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.minsize(min(px(880), w), min(px(620), h))
        self.proc = None
        self.log_queue = queue.Queue()
        # —— 状态/指标 ——
        self._state = "idle"
        self._run_start = None
        self._wait_since = None
        self._m_run_answered = 0
        self._m_total_answered = None
        self._m_polls = 0
        self._rec = None            # 正在作答的那条记录
        self._records = 0           # 流水条数
        self._follow = True         # 日志是否跟随最新
        self._active_tab = 0
        install_exception_hook(self)
        T.register_root(self)
        self._build()
        self._load_summary()
        self._set_state("idle")
        T.subscribe(self._on_theme_changed)
        self.after(120, self._drain_log)
        self.after(1000, self._tick_clock)
        self.after(40, lambda: apply_titlebar(self))

    # ---------- UI ----------
    def _build(self):
        T.init_fonts(self)
        self.configure(bg=T.canvas)

        # ---- 头部：品牌 / 副标题 / 状态胶囊 / 主题切换 ----
        head = tk.Frame(self, bg=T.canvas)
        head.pack(fill="x", padx=px(22), pady=(px(18), px(0)))

        mark = tk.Canvas(head, width=px(38), height=px(38), bg=T.canvas,
                         highlightthickness=0, bd=0)
        mark.pack(side="left")
        _round_rect(mark, 0, 0, px(38), px(38), px(11), fill=T.accent, outline=T.accent)
        mark.create_text(px(19), px(19) + 0.5, text="飞", fill=T.accent_ink, font=T.font_body)
        self._mark = mark

        tb = tk.Frame(head, bg=T.canvas)
        tb.pack(side="left", padx=(px(12), px(0)))
        themed(tk.Label(tb, text=APP_TITLE, font=T.font_title, anchor="w"),
               "canvas", "ink").pack(anchor="w")
        self.lbl_sub = themed(tk.Label(tb, text="正在读取配置…", font=T.font_small, anchor="w"),
                              "canvas", "ink_subtle")
        self.lbl_sub.pack(anchor="w", pady=(px(2), px(0)))

        self.pill_state = Pill(head, text="未运行", kind="neutral", dot=True)
        self.pill_state.pack(side="right", pady=(px(8), px(0)))
        self.btn_theme = RButton(head, text="深色", icon="◐", variant="secondary",
                                 height=32, font=T.font_small,
                                 command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=(px(0), px(10)), pady=(px(4), px(0)))

        # ---- 状态卡：一句话说清"现在在干什么" ----
        hero = Panel(self, pad=20, radius=16, height=176)
        hero.pack(fill="x", padx=px(22), pady=(px(16), px(0)))
        self._hero = hero
        hb = hero.body
        hb.bind("<Configure>", lambda _e: self._fit_panel(hero))

        row = tk.Frame(hb, bg=T.surface)
        row.pack(fill="x")
        self.dot = StatusDot(row, size=14, color=T.ink_subtle)
        self.dot.pack(side="left", pady=(px(5), px(0)))
        self.lbl_st_main = themed(tk.Label(row, text="未运行", font=T.font_h2, anchor="w"),
                                  "surface", "ink")
        self.lbl_st_main.pack(side="left", padx=(px(9), px(0)))

        self._metrics = {}

        def metric(key, name, first=False):
            box = tk.Frame(row, bg=T.surface)
            box.pack(side="right", padx=(0 if first else 20, 0))
            val = themed(tk.Label(box, text="—", font=T.font_metric, anchor="e"),
                         "surface", "ink")
            val.pack(anchor="e")
            themed(tk.Label(box, text=name, font=T.font_tiny, anchor="e"),
                   "surface", "ink_subtle").pack(anchor="e")
            self._metrics[key] = val

        metric("total", "累计已答", first=True)
        metric("run", "本轮已答")
        metric("poll", "轮询")
        metric("uptime", "运行时长")

        self.lbl_desc = themed(
            tk.Label(hb, text="", font=T.font_small, anchor="w", justify="left",
                     wraplength=px(840)),
            "surface", "ink_muted")
        self.lbl_desc.pack(fill="x", pady=(px(10), px(0)))

        act = tk.Frame(hb, bg=T.surface)
        act.pack(fill="x", pady=(px(14), px(0)))
        self.btn_start = RButton(act, text="启动监控", icon="▶", variant="primary",
                                 height=38, command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = RButton(act, text="停止", icon="■", variant="secondary",
                                height=38, command=self.stop)
        self.btn_stop.pack(side="left", padx=(px(10), px(0)))
        self.lbl_last = themed(tk.Label(act, text="", font=T.font_small, anchor="e"),
                               "surface", "ink_subtle")
        self.lbl_last.pack(side="right")

        # ---- 页签 ----
        bar = tk.Frame(self, bg=T.canvas)
        bar.pack(fill="x", padx=px(22), pady=(px(18), px(0)))
        self.seg = SegTabs(bar, ["运行日志", "答题流水", "配置与检查"],
                           command=self._switch_tab, height=36)
        self.seg.pack(side="left")

        self.tab_host = tk.Frame(self, bg=T.canvas)
        self.tab_host.pack(fill="both", expand=True, padx=px(22), pady=(px(10), px(0)))

        self._build_tab_log()
        self._build_tab_stream()
        self._build_tab_cfg()
        self._tabs = [self.tab_log, self.tab_stream, self.tab_cfg]
        self._switch_tab(0)

        # ---- 底栏：次要操作 ----
        foot = tk.Frame(self, bg=T.canvas)
        foot.pack(fill="x", padx=px(22), pady=(px(12), px(16)))
        self.btn_data = RButton(foot, text="数据目录", icon="📂", variant="ghost",
                                font=T.font_small, command=self.open_data)
        self.btn_data.pack(side="left")
        self.btn_update = RButton(foot, text="导入更新包", icon="📦", variant="ghost",
                                  font=T.font_small, command=self.import_update)
        self.btn_update.pack(side="left", padx=(px(6), px(0)))
        self.btn_help = RButton(foot, text="使用说明", icon="❓", variant="ghost",
                                font=T.font_small, command=self.show_help)
        self.btn_help.pack(side="left", padx=(px(6), px(0)))
        self.lbl_ver = themed(tk.Label(foot, text="", font=T.font_tiny, anchor="e"),
                              "canvas", "ink_subtle")
        self.lbl_ver.pack(side="right")

    # ---- 三个页签 ----
    def _build_tab_log(self):
        self.tab_log = Panel(self.tab_host, pad=0, radius=14)
        lb = self.tab_log.body

        bar = tk.Frame(lb, bg=T.surface)
        bar.pack(fill="x", padx=px(14), pady=(px(10), px(8)))
        self.btn_follow = RButton(bar, text="● 跟随最新", variant="ghost", height=28,
                                  font=T.font_small, command=self._toggle_follow)
        self.btn_follow.pack(side="left")
        self.btn_copy = RButton(bar, text="复制", variant="ghost", height=28,
                                font=T.font_small, command=self._copy_log)
        self.btn_copy.pack(side="right")
        self.btn_clear = RButton(bar, text="清屏", variant="ghost", height=28,
                                 font=T.font_small, command=self.clear_log)
        self.btn_clear.pack(side="right", padx=(px(0), px(4)))

        wrap = tk.Frame(lb, bg=T.log_bg)
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(
            wrap, wrap="word", font=T.font_mono, bd=0, highlightthickness=0,
            bg=T.log_bg, fg=T.log_ink, insertbackground=T.log_ink,
            padx=px(14), pady=px(8), spacing1=px(1), spacing3=px(2),
        )
        self.sb = _attach_text_scrollbar(wrap, self.txt)
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.configure(state="disabled")
        self._bind_text(self.txt)

    def _build_tab_stream(self):
        self.tab_stream = Panel(self.tab_host, pad=0, radius=14)
        sb_ = self.tab_stream.body

        bar = tk.Frame(sb_, bg=T.surface)
        bar.pack(fill="x", padx=px(14), pady=(px(10), px(8)))
        themed(tk.Label(bar, text="每答一题记一行：题号 · 用时 · 答案 · 结果",
                        font=T.font_small, anchor="w"),
               "surface", "ink_subtle").pack(side="left")
        self.lbl_stream_cnt = themed(tk.Label(bar, text="", font=T.font_small, anchor="e"),
                                     "surface", "ink_subtle")
        self.lbl_stream_cnt.pack(side="right")

        wrap = tk.Frame(sb_, bg=T.log_bg)
        wrap.pack(fill="both", expand=True)
        self.st = tk.Text(
            wrap, wrap="word", font=T.font_mono, bd=0, highlightthickness=0,
            bg=T.log_bg, fg=T.log_ink, insertbackground=T.log_ink,
            padx=px(14), pady=px(8), spacing1=px(2), spacing3=px(6),
        )
        self.st.configure(state="disabled")
        self.st_sb = _attach_text_scrollbar(wrap, self.st)
        self.st.pack(side="left", fill="both", expand=True)
        self._bind_text(self.st)

    def _build_tab_cfg(self):
        # ⚠️ 本页内容是**定高**的（5 行配置 + 分隔线 + 5 行检查 + 按钮 ≈ 490px），
        # 而页签区在默认 1000x720 下只有 ~316px。若像以前那样直接把内容塞进
        # Panel.body，会被 Panel._layout() 按可视高度钳住 → 底部几行被硬裁
        # （实测溢出 172px，「浏览器登录态」只露半截）。
        # 日志页靠内部 Text 自己滚动所以没暴露，配置页没有滚动能力就露馅了。
        # 这里包一层可滚动视口：内容再高也不丢，滚动条按内容按需显隐。
        self.tab_cfg = Panel(self.tab_host, pad=0, radius=14)
        vp = self.tab_cfg.body

        # 滚动条先 pack（与 _attach_text_scrollbar 的顺序一致），确保它先拿到
        # 右侧那条空间，画布再 expand 吃掉剩下的。
        self._cfg_cv = themed(tk.Canvas(vp, highlightthickness=0, bd=0,
                                        yscrollincrement=px(22)), "surface")
        self._cfg_sb = RScrollbar(vp)
        self._cfg_sb.configure(command=self._cfg_cv.yview)
        self._cfg_cv.configure(yscrollcommand=self._cfg_sb.set)
        self._cfg_sb.pack(side="right", fill="y")
        self._cfg_cv.pack(side="left", fill="both", expand=True)
        self._cfg_sb_shown = True

        self._cfg_holder = themed(tk.Frame(self._cfg_cv), "surface")
        self._cfg_win = self._cfg_cv.create_window(0, 0, window=self._cfg_holder,
                                                   anchor="nw")
        self._cfg_holder.bind("<Configure>", lambda _e: self._cfg_sync())
        self._cfg_cv.bind("<Configure>", lambda _e: self._cfg_sync())
        for _w in (self._cfg_cv, self._cfg_holder):
            _w.bind("<MouseWheel>", self._cfg_wheel)

        # 内容本体：留出与原 Panel(pad=22) 一致的 22px 内边距
        cb = themed(tk.Frame(self._cfg_holder), "surface")
        cb.pack(fill="both", expand=True, padx=px(22), pady=px(20))

        themed(tk.Label(cb, text="当前配置", font=T.font_h2, anchor="w"),
               "surface", "ink").pack(anchor="w")
        self.cfg_rows = {}
        for key, name in (("feishu", "飞书地址"), ("chat", "会话名称"),
                          ("model", "模型"), ("base", "接口地址"), ("key", "密钥")):
            r = tk.Frame(cb, bg=T.surface)
            r.pack(fill="x", pady=(px(7), px(0)))
            themed(tk.Label(r, text=name, font=T.font_small, width=9, anchor="w"),
                   "surface", "ink_muted").pack(side="left")
            v = themed(tk.Label(r, text="—", font=T.font_mono_sm, anchor="w"),
                       "surface", "ink")
            v.pack(side="left", fill="x", expand=True)
            self.cfg_rows[key] = v

        tk.Frame(cb, bg=T.hairline, height=px(1)).pack(fill="x", pady=(px(16), px(14)))

        themed(tk.Label(cb, text="运行前检查", font=T.font_h2, anchor="w"),
               "surface", "ink").pack(anchor="w")
        self.health = {}
        for key, name in (("cfg", "配置文件"), ("key", "模型密钥"),
                          ("login", "浏览器登录态"), ("history", "历史上下文"),
                          ("logic", "逻辑版本")):
            row = HealthRow(cb, name)
            row.pack(fill="x", pady=(px(8), px(0)))
            self.health[key] = row

        act = tk.Frame(cb, bg=T.surface)
        act.pack(fill="x", pady=(px(18), px(0)))
        RButton(act, text="修改配置", icon="⚙", height=34,
                command=self.edit_config).pack(side="left")
        RButton(act, text="重新检查", icon="↻", variant="ghost", height=34,
                font=T.font_small, command=self._load_summary).pack(side="left", padx=(px(8), px(0)))

    def _bind_text(self, widget):
        """鼠标滚轮 + 关闭编辑。"""
        widget.bind("<MouseWheel>", lambda e: (widget.yview_scroll(int(-e.delta / 120), "units"),
                                              "break")[1])

    def _cfg_sync(self):
        """同步「配置与检查」滚动视口：撑满宽度 + 按需显隐滚动条。

        只在"该不该显示"发生变化时动 pack，避免 <Configure> 里反复重排。
        """
        try:
            cv = self._cfg_cv
            cv.itemconfigure(self._cfg_win, width=max(1, cv.winfo_width()))
            cv.configure(scrollregion=cv.bbox("all"))
            need = self._cfg_holder.winfo_reqheight()
            show = need > cv.winfo_height() + 1
            if show != self._cfg_sb_shown:
                self._cfg_sb_shown = show
                if show:
                    # before= 保证仍排在画布之前（pack_forget 后再 pack 会跑到最后）
                    self._cfg_sb.pack(side="right", fill="y", before=cv)
                else:
                    self._cfg_sb.pack_forget()
                    cv.yview_moveto(0)
        except Exception:
            pass

    def _cfg_wheel(self, event):
        """鼠标滚轮滚动配置页。"""
        try:
            self._cfg_cv.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass
        return "break"

    def _fit_panel(self, panel):
        """让卡片高度贴合内容（指标/描述换行后也能自动长高）。"""
        try:
            need = panel.body.winfo_reqheight() + panel._pad * 2
            if abs(need - panel.winfo_height()) > 1:
                panel.configure(height=need)
        except Exception:
            pass

    def _switch_tab(self, idx):
        self._active_tab = idx
        for i, t in enumerate(self._tabs):
            try:
                if i == idx:
                    t.pack(fill="both", expand=True)
                else:
                    t.pack_forget()
            except Exception:
                pass
        if idx == 2:
            self._load_summary()

    def _toggle_follow(self):
        self._follow = not self._follow
        self.btn_follow.config(text=("● 跟随最新" if self._follow else "○ 已暂停跟随"))
        if self._follow:
            self.txt.see("end")

    def _copy_log(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.txt.get("1.0", "end").strip())
            toast(self, "日志已复制到剪贴板", "ok")
        except Exception as e:
            toast(self, f"复制失败：{e}", "error")

    def _toggle_theme(self):
        T.toggle()          # 通知所有订阅者：整棵树换色 + 自绘控件重绘

    def _on_theme_changed(self):
        try:
            self.btn_theme.config(text=("深色" if T.mode == "light" else "浅色"))
        except Exception:
            pass
        self._apply_text_theme()
        apply_titlebar(self)

    def _apply_text_theme(self):
        """Text 的 tag 颜色不随 canvas 自动变，这里手工同步。"""
        for w in (getattr(self, "txt", None), getattr(self, "st", None)):
            if w is None:
                continue
            try:
                w.configure(bg=T.log_bg, fg=T.log_ink, insertbackground=T.log_ink)
                w.tag_configure("time", foreground=T.ink_subtle)
                w.tag_configure("meta", foreground=T.ink_subtle)
                w.tag_configure("info", foreground=T.log_ink)
                w.tag_configure("warn", foreground=T.warn)
                w.tag_configure("error", foreground=T.danger)
                w.tag_configure("ok", foreground=T.success)
                w.tag_configure("mark", foreground=T.accent)
                w.tag_configure("idx", foreground=T.accent)
                w.tag_configure("stem", foreground=T.ink_subtle)
                w.tag_configure("dim", foreground=T.ink_subtle)
            except Exception:
                pass

    # ---------- 状态机 ----------
    def _set_state(self, key, detail=None):
        self._state = key
        text, desc, kind = self.STATE_TEXT.get(key, self.STATE_TEXT["idle"])
        if detail:
            desc = detail
        color = {"neutral": T.ink_subtle, "warn": T.warn, "accent": T.accent,
                 "success": T.success, "danger": T.danger}.get(kind, T.ink_subtle)
        running = key not in ("idle", "stopped")
        if key == "waiting" and not self._wait_since:
            self._wait_since = time.time()
        if key != "waiting":
            self._wait_since = None
        try:
            self.dot.set(color, animate=running)
            self.lbl_st_main.configure(text=text)
            self.pill_state.set(text, kind)
            self.lbl_desc.configure(text=desc)
            self.btn_start.config(state="disabled" if running else "normal")
            self.btn_stop.config(state="normal" if running else "disabled")
        except Exception:
            pass
        self._refresh_metrics()

    def _uptime(self):
        if not self._run_start:
            return "—"
        sec = int(time.time() - self._run_start)
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"

    def _refresh_metrics(self):
        vals = {
            "total": "—" if self._m_total_answered is None else str(self._m_total_answered),
            "run": str(self._m_run_answered),
            "poll": str(self._m_polls),
            "uptime": self._uptime(),
        }
        for k, v in vals.items():
            w = self._metrics.get(k)
            if w is not None:
                try:
                    w.configure(text=v)
                except Exception:
                    pass

    def _tick_clock(self):
        try:
            if self._state not in ("idle", "stopped"):
                self._refresh_metrics()
            if self._state == "waiting" and self._wait_since:
                mins = int((time.time() - self._wait_since) // 60)
                self.lbl_last.configure(
                    text=(f"已等待 {mins} 分钟" if mins >= 1 else "刚开始等待"))
        except Exception:
            pass
        self.after(1000, self._tick_clock)

    # ---------- 日志解析：界面的"眼睛" ----------
    def _ingest(self, line):
        """从一行日志里提取状态与指标（不改日志本身）。"""
        m = self.LOG_RE.match(line.strip())
        body = (m.group(3) if m else line).strip()
        level = m.group(2) if m else "INFO"

        try:
            if "自动回复程序 启动" in body:
                self._run_start = time.time()
                self._m_run_answered = 0
                self._m_polls = 0
                self._set_state("starting")
            elif "已记录已答题指纹" in body:
                mm = re.search(r"已记录已答题指纹:\s*(\d+)", body)
                if mm:
                    self._m_total_answered = int(mm.group(1))
            elif "模型:" in body and "接口:" in body:
                mm = re.search(r"模型:\s*(\S+)\s*\|\s*接口:\s*([^（(]+)", body)
                if mm:
                    self._set_subtitle(mm.group(1), mm.group(2).strip())
            elif "浏览器已就绪" in body or "就绪。轮询" in body or "进入会话" in body:
                self._set_state("waiting")
            elif "[待命]" in body:
                mm = re.search(r"已轮询\s*(\d+)\s*轮", body)
                if mm:
                    self._m_polls = int(mm.group(1))
                self._set_state("waiting")
            elif "[开课]" in body:
                self._set_state("course")
            elif "待答题:" in body:
                self._begin_record(body)
                self._set_state("answering")
            elif body.startswith("答案（"):
                if self._rec is not None:
                    self._rec["multi"] = True
            elif body.startswith("答案:"):
                if self._rec is not None:
                    self._rec["ans"] = body.split(":", 1)[1].strip()
            elif body.startswith("|") and self._rec is not None and self._rec.get("multi"):
                self._rec["ans"] += ("\n" if self._rec["ans"] else "") + body.lstrip("| ").rstrip()
            elif "已发送 ✓" in body:
                self._finish_record("ok")
                self._m_run_answered += 1
                self._set_state("sent")
            elif "发送失败" in body:
                self._finish_record("err")
                self._set_state("error", "这条答案没发出去，程序会在下一轮重试。")
            elif "进程已结束" in body:
                self._set_state("stopped")
            elif level == "ERROR":
                self._set_state("error", f"出错：{body[:60]}")
        except Exception:
            pass

    def _set_subtitle(self, model, provider):
        self.lbl_sub.configure(
            text=f"逻辑 v{runtime.logic_version_note()}   ·   {model}   ·   {provider}")

    def _begin_record(self, body):
        mm = re.search(r"\[第\s*(\d+)\s*次\]\s*待答题:\s*([^|]+?)\s*(?:\|\s*(.*))?$", body)
        n, qtag, stem = "", "", ""
        if mm:
            n, qtag, stem = mm.group(1), mm.group(2).strip(), (mm.group(3) or "").strip()
        self._rec = {"n": n, "qtag": qtag or "（无题号）", "stem": stem,
                     "ans": "", "multi": False, "t": time.strftime("%H:%M:%S")}

    def _finish_record(self, status):
        if self._rec is None:
            return
        rec = self._rec
        self._rec = None
        self._records += 1
        try:
            self.lbl_stream_cnt.configure(text=f"共 {self._records} 条")
        except Exception:
            pass
        ans = rec["ans"] or "—"
        one_line = _clip(" ".join(ans.split()), 56)
        self.st.configure(state="normal")
        self.st.insert("end", f"#{rec['n'] or '—'}  ", "idx")
        self.st.insert("end", f"{rec['qtag']}  ", "info")
        self.st.insert("end", f"{rec['t']}   ", "time")
        self.st.insert("end", f"{one_line}   ", "info")
        self.st.insert("end", ("已发送 ✓\n" if status == "ok" else "发送失败 ✕\n"),
                       ("ok" if status == "ok" else "error"))
        if rec["stem"]:
            stem = rec["stem"][:90] + ("…" if len(rec["stem"]) > 90 else "")
            self.st.insert("end", f"     {stem}\n", "stem")
        self.st.see("end")
        self.st.configure(state="disabled")
        try:
            self.lbl_last.configure(
                text=f"最近：{rec['qtag']} · {one_line[:18]}"
                     + (" ✓" if status == "ok" else " ✕"))
        except Exception:
            pass

    # ---------- 日志 ----------
    def _append_text(self, text, tag):
        self.txt.configure(state="normal")
        self.txt.insert("end", text, tag)
        if int(self.txt.index("end-1c").split(".")[0]) > self.MAX_LOG_LINES:
            self.txt.delete("1.0", "200.0")
        if self._follow:
            self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log(self, line, tag=None):
        m = self.LOG_RE.match(line.strip())
        if m and tag is None:
            ts, level, rest = m.group(1), m.group(2), m.group(3)
            t = {"ERROR": "error", "WARNING": "warn"}.get(level, "info")
            if "✓" in rest or "已就绪" in rest:
                t = "ok"
            self._append_text(f"{ts}  ", "time")
            self._append_text(f"{rest}\n", t)
            return
        self._append_text(f"{line}\n", tag or "meta")

    def _drain_log(self):
        try:
            while True:
                raw = self.log_queue.get_nowait()
                line = raw.rstrip("\n")
                self._ingest(line)
                self._log(line)
        except queue.Empty:
            pass
        self.after(120, self._drain_log)

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _load_summary(self):
        """把配置摘要、健康检查、副标题一次刷新。"""
        self._apply_text_theme()
        version = runtime.logic_version_note()
        try:
            self.lbl_ver.configure(text=f"{APP_TITLE} · 逻辑 v{version}")
        except Exception:
            pass

        if not CONFIG_FILE.exists():
            for w in self.cfg_rows.values():
                w.configure(text="尚未配置", fg=T.warn)
            self.health["cfg"].set("未找到 config.yaml", "warn")
            self.health["key"].set("—", "warn")
            self._set_subtitle("未配置", "请先点「修改配置」")
            return

        try:
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self.health["cfg"].set(f"读取失败：{str(e)[:40]}", "error")
            return

        fs = cfg.get("feishu", {}) or {}
        llm = cfg.get("llm", {}) or {}
        key = str(llm.get("api_key", "") or "")
        masked = (key[:8] + "…" + key[-4:]) if len(key) > 14 else "（未填）"
        self._cfg_feishu = fs.get("base_url", "（未填）")
        self._cfg_base = llm.get("base_url", "（未填）")

        self.cfg_rows["feishu"].configure(text=self._cfg_feishu)
        self.cfg_rows["chat"].configure(text=fs.get("target_chat_name", "（未填）"))
        self.cfg_rows["model"].configure(text=llm.get("model", "（未填）"))
        self.cfg_rows["base"].configure(text=self._cfg_base)
        self.cfg_rows["key"].configure(text=masked)

        prov = ""
        try:
            p = detect_provider(llm.get("base_url", ""), llm.get("model", ""),
                                llm.get("provider", ""))
            prov = PROVIDER_LABELS.get(p, "")
        except Exception:
            prov = ""
        self._set_subtitle(llm.get("model", "（未填）"),
                           prov or "未知接口")

        # —— 健康检查 ——
        self.health["cfg"].set("已就绪", "ok")
        if len(key) >= 12:
            self.health["key"].set("已填写", "ok")
        else:
            self.health["key"].set("未填写或过短", "warn")
        try:
            # 与 _profile_dir() 同源：用户改过 browser.user_data_dir 时，
            # 这里检查的才是真正的登录态目录（否则会误报"首次需扫码"）。
            profile = _profile_dir()
            n = sum(1 for _ in profile.iterdir()) if profile.is_dir() else 0
            if n > 3:
                self.health["login"].set("已保存（无需再扫码）", "ok")
            else:
                self.health["login"].set("首次启动需扫码登录", "warn")
        except Exception:
            self.health["login"].set("无法读取", "warn")
        try:
            hist = DATA_DIR / "history.json"
            if hist.exists():
                data = json.loads(hist.read_text(encoding="utf-8"))
                self.health["history"].set(f"{len(data)} 条", "ok")
            else:
                self.health["history"].set("无（也能作答）", "warn")
        except Exception:
            self.health["history"].set("无法读取", "warn")
        self.health["logic"].set(f"v{version}", "accent")

    # ---------- 启停 ----------
    def _python_cmd(self, script, extra=None):
        """构造启动子进程的命令。

        打包后：exe 带 --run-sub <script> 参数，由入口再执行脚本。
        源码运行：直接用 python <script>。

        注意：打包后的 exe 是窗口程序（没有控制台、没有 stdout），
        因此子进程输出统一写入"实时日志文件"，由 _tail_live 读取。
        """
        if IS_FROZEN:
            cmd = [sys.executable, "--run-sub", str(script)]
        else:
            cmd = [sys.executable, "-u", str(script)]
        if extra:
            cmd += list(extra)
        return cmd

    def start(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                return
            self.proc = None          # 上一轮已退出但尚未收尾 → 复位后再启动
        if not CONFIG_FILE.exists():
            if not messagebox.askyesno(
                "尚未配置",
                "还没有配置文件，是否现在填写？",
                parent=self,
            ):
                return
            self.edit_config()
            if not CONFIG_FILE.exists():
                return

        # 清空实时日志（仅本次运行输出）
        try:
            LIVE_LOG.write_text("", encoding="utf-8")
        except Exception:
            pass

        self._log("─" * 60)
        self._log(f"[{time.strftime('%H:%M:%S')}] 启动监控…")

        env = os.environ.copy()
        env["FEISHU_DATA_DIR"] = str(DATA_DIR)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["FEISHU_LIVE_LOG"] = str(LIVE_LOG)

        script = _script_path("step2_auto_reply.py")

        popen_kw = dict(stdin=subprocess.DEVNULL, cwd=str(DATA_DIR), env=env)
        self._live_fh = None
        if IS_FROZEN:
            # 窗口 exe 无 stdout → 重定向到实时日志文件
            try:
                self._live_fh = open(LIVE_LOG, "a", encoding="utf-8", errors="replace")
            except Exception:
                self._live_fh = subprocess.DEVNULL
            popen_kw.update(
                stdout=self._live_fh,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            popen_kw.update(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

        try:
            self.proc = subprocess.Popen(self._python_cmd(script), **popen_kw)
        except Exception as e:
            messagebox.showerror("启动失败", f"无法启动监控进程：\n{e}", parent=self)
            self._log(f"❌ 启动失败：{e}")
            return

        self._set_state("starting")

        self._stopping = False
        self._tail_buf = ""
        if IS_FROZEN:
            try:
                self._live_pos = LIVE_LOG.stat().st_size
            except Exception:
                self._live_pos = 0
            self.after(700, self._tail_live)
        else:
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

        self.after(1000, self._watch)

    def _tail_live(self):
        """读取实时日志文件的新增内容（打包模式）。

        ⚠️ 必须用**二进制**读：st_size 与 seek 都是**字节**偏移，
        而文本模式下 seek() 的实参含义是"字符位置"。
        日志几乎全中文（1 字符 → UTF-8 3 字节），拿字节数去 seek 会直接
        越过真实结尾 → data="" 且 tell() 停在原处 → 此后 st_size 恒等于位置，
        永远读不到任何新内容（打包版日志框一片空白）。
        """
        proc = self.proc
        if proc is None:
            return
        try:
            size = LIVE_LOG.stat().st_size
            # 文件被清空/轮转（size 变小）→ 位置必须重置，否则增量读取永久卡死
            if size < self._live_pos:
                self._live_pos = 0
                self._tail_buf = ""
            if size > self._live_pos:
                with open(LIVE_LOG, "rb") as f:
                    f.seek(self._live_pos)
                    data = f.read()
                    self._live_pos = f.tell()
                buf = self._tail_buf + data.decode("utf-8", "replace")
                *whole, rest = buf.split("\n")
                self._tail_buf = rest          # 半行留到下次，避免把行截断
                for line in whole:
                    self.log_queue.put(line + "\n")
        except FileNotFoundError:
            pass
        except Exception:
            pass

        if proc.poll() is None:
            self.after(500, self._tail_live)
        else:
            # 收尾：读完剩余内容
            try:
                with open(LIVE_LOG, "rb") as f:
                    f.seek(self._live_pos)
                    rest = self._tail_buf + f.read().decode("utf-8", "replace")
                for line in rest.split("\n"):
                    if line:
                        self.log_queue.put(line + "\n")
            except Exception:
                pass
            self._tail_buf = ""
            self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] 进程已结束。\n")

    def _pump(self, proc):
        """把子进程输出搬进队列（源码模式）。"""
        try:
            for line in proc.stdout:
                self.log_queue.put(line)
        except Exception:
            pass
        finally:
            self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] 进程已结束。\n")

    def _close_live_fh(self):
        try:
            fh = getattr(self, "_live_fh", None)
            if fh and fh is not subprocess.DEVNULL:
                fh.close()
        except Exception:
            pass
        self._live_fh = None

    def _watch(self):
        if self.proc is None:
            return
        rc = self.proc.poll()
        if rc is None:
            self.after(1000, self._watch)
        else:
            self._on_proc_exit(rc)

    def _on_proc_exit(self, rc):
        """子进程结束：复位句柄 + 报告退出码。

        原实现从不读 returncode、也从不把 self.proc 复位，于是最常见的
        启动失败（脚本找不到返回 2 / 更新包换坏了 .py / 依赖缺失）用户只看到
        "启动监控 → 1 秒后已停止"，零提示；叠加日志框为空时完全无从下手。
        """
        proc, self.proc = self.proc, None
        self._close_live_fh()
        # 清理可能残留的浏览器锁（程序被强杀时会留下）
        try:
            removed = _clean_profile_locks()
            if removed:
                self._log(f"已清理残留锁文件：{', '.join(removed)}")
        except Exception:
            pass

        if getattr(self, "_stopping", False):
            self._log(f"[{time.strftime('%H:%M:%S')}] 监控已停止。")
            self._set_state("stopped")
            return
        if rc == 0:
            self._log(f"[{time.strftime('%H:%M:%S')}] 程序正常退出（代码 0）。")
            self._set_state("stopped")
            return
        self._log(f"❌ 子进程异常退出（代码 {rc}）。"
                  f" 详情见：{LIVE_LOG}")
        self._set_state("error", f"程序异常退出（代码 {rc}）。")

    def _kill_tree(self, proc, pid):
        """在后台线程里整树结束子进程（不阻塞界面）。"""
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

    def stop(self):
        """停止监控。

        ⚠️ 原实现在 Tk 主线程里同步等 terminate(6s) + kill(6s) + taskkill(20s)
        —— 最长 32 秒界面假死，而禁用按钮的 _set_state("stopped") 在阻塞**之后**
        才执行，用户可连点叠加。现改为：先禁用按钮 → 后台线程整树结束 →
        _watch 检测到进程退出后再统一收尾。
        """
        if self.proc is None:
            return
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self._log(f"[{time.strftime('%H:%M:%S')}] 正在停止…")
        self._set_state("stopped")          # 先禁用按钮，防重复点击
        proc, pid = self.proc, self.proc.pid
        self._close_live_fh()
        threading.Thread(target=self._kill_tree, args=(proc, pid),
                         daemon=True).start()
        # 交给 _watch 收尾；它若已停止轮询则这里兜底再排一次
        self.after(600, self._watch)

    # ---------- 其它按钮 ----------
    def edit_config(self):
        w = ConfigWizard(self)
        self.wait_window(w)
        if w.result:
            self._log("配置已更新。")
            toast(self, "配置已保存", "ok")
        self._load_summary()

    def open_data(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(DATA_DIR))  # noqa: S606 (Windows)
        except Exception:
            try:
                subprocess.Popen(["explorer", str(DATA_DIR)])
            except Exception as e:
                # 兜底分支本身也会失败 → 必须给提示，否则用户"点了没反应"
                toast(self, f"无法打开数据目录：{str(e)[:40]}", "err", ms=4200)

    def import_update(self):
        """导入「逻辑更新包」(.zip)：不换 exe 也能更新逻辑（增量更新）。"""
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showwarning(
                "监控运行中",
                "监控正在运行（进程 %s）。\n\n"
                "被替换的 .py 只在新进程里生效，当前进程仍使用内存中的旧代码。\n"
                "请先点「停止」，再导入更新包。" % self.proc.pid,
                parent=self,
            )
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="选择逻辑更新包（.zip）",
            filetypes=[("逻辑更新包", "*.zip"), ("所有文件", "*.*")],
        )
        if not path:
            return
        ok, msg = runtime.apply_logic_package(path)
        if not ok:
            messagebox.showerror("更新失败", msg, parent=self)
            self._log(f"更新失败：{msg}")
            return
        self._log(f"更新包已导入：{msg}")
        version = runtime.logic_version_note()
        self.title(APP_TITLE)
        self._load_summary()
        toast(self, f"更新完成 · 逻辑 v{version}，重启监控后生效", "ok", ms=4200)
        messagebox.showinfo(
            "更新完成",
            f"{msg}\n\n"
            "请点「停止」（若在运行），再点「启动监控」，新逻辑即生效。\n\n"
            f"逻辑目录：{runtime.logic_dir()}",
            parent=self,
        )

    def show_help(self):
        """在窗口内显示使用说明（单文件分发时没有外部说明文件）。"""
        text = ""
        for p in (APP_DIR / "使用说明.txt", DATA_DIR / "使用说明.txt",
                  _script_path("使用说明.txt")):
            try:
                if p.exists():
                    text = p.read_text(encoding="utf-8", errors="replace")
                    break
            except Exception:
                continue
        if not text:
            text = (f"【{APP_TITLE}】\n\n"
                    "1. 点「修改配置」填写飞书地址、会话名、API Key\n"
                    "2. 点「启动监控」，首次用手机飞书扫码登录\n"
                    "3. 之后保持程序运行即可，浏览器窗口别关\n\n"
                    f"数据与配置保存在：\n{DATA_DIR}\n\n"
                    "（可点「数据目录」按钮直接打开）")

        win = tk.Toplevel(self)
        win.title(f"{APP_TITLE} · 使用说明")
        win.configure(bg=T.canvas)
        win.geometry(f"{px(680)}x{px(600)}")
        # ⚠️ 与配置向导同理：owner 不可见时 transient 会让窗口根本不被 map
        try:
            if self.winfo_viewable():
                win.transient(self)
        except Exception:
            pass
        T.register_root(win)

        head = tk.Frame(win, bg=T.canvas)
        head.pack(fill="x", padx=px(20), pady=(px(16), px(0)))
        themed(tk.Label(head, text="使用说明", font=T.font_title, anchor="w"),
               "canvas", "ink").pack(anchor="w")
        themed(tk.Label(head, text="遇到问题先看这里；更完整的问答在程序目录的「使用说明.txt」",
                        font=T.font_small, anchor="w"),
               "canvas", "ink_subtle").pack(anchor="w", pady=(px(2), px(0)))

        panel = Panel(win, pad=0, radius=14)
        panel.pack(fill="both", expand=True, padx=px(20), pady=(px(14), px(0)))
        txt_wrap = tk.Frame(panel.body, bg=T.log_bg)
        txt_wrap.pack(fill="both", expand=True)
        tx = tk.Text(txt_wrap, wrap="word", font=T.font_body, bd=0,
                     highlightthickness=0, bg=T.log_bg, fg=T.log_ink,
                     padx=px(16), pady=px(12), spacing1=px(2), spacing3=px(2))
        _attach_text_scrollbar(txt_wrap, tx)
        tx.pack(side="left", fill="both", expand=True)
        tx.insert("1.0", text)
        tx.configure(state="disabled")
        tx.bind("<MouseWheel>", lambda e: (
            tx.yview_scroll(int(-e.delta / 120), "units"), "break")[1])

        foot = tk.Frame(win, bg=T.canvas)
        foot.pack(fill="x", padx=px(20), pady=px(14))
        RButton(foot, text="关闭", variant="primary", height=34,
                command=win.destroy).pack(side="right")
        win.after(40, lambda: apply_titlebar(win))

    def on_close(self):
        if self.proc is None or self.proc.poll() is not None:
            self.destroy()
            return
        if not messagebox.askyesno("退出", "监控仍在运行，确定要退出吗？", parent=self):
            return
        self._closing = True
        self.stop()
        # 等收尾完成再 destroy（含 10 秒兜底）—— 否则主线程立刻退出会把
        # 后台的 taskkill 线程一起带走，可能漏杀浏览器、锁住 .browser_data。
        self._wait_close(time.time() + 10)

    def _wait_close(self, deadline):
        if self.proc is None or time.time() > deadline:
            self.destroy()
            return
        self.after(150, lambda: self._wait_close(deadline))


# ============================================================
# 入口
# ============================================================
def run_subprocess_mode():
    """
    打包后子进程模式：exe --run-sub <script.py> [args...]
    由本函数执行目标脚本，使 exe 既能当 GUI 又能当脚本运行器。
    """
    args = sys.argv[2:]
    if not args:
        print("用法: app.exe --run-sub <script.py> [args...]", file=sys.stderr)
        return 2

    # 控制台编码兜底：脚本可能 print 中文 / Emoji / ⚠，而 Windows 控制台
    # 默认 cp936（GBK）—— 这些字符不在码表里会直接 UnicodeEncodeError 崩掉。
    # 打包后用户无法改代码，只能在这里统一换成 UTF-8 + 容错流。
    for _name in ("stdout", "stderr"):
        try:
            _s = getattr(sys, _name, None)
            if _s is not None and (getattr(_s, "encoding", "") or "").lower() \
                    not in ("utf-8", "utf8"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 脚本可能以相对名传入（如 step2_auto_reply.py），
    # 按「外部逻辑目录 → 内置资源目录 → 数据目录」顺序查找。
    raw = args[0]
    raw_p = Path(raw)
    if raw_p.is_absolute():
        candidates = [raw_p]
    else:
        candidates = [Path(d) / raw for d in runtime.logic_paths()] + [raw_p]
    script = next((c for c in candidates if c.exists()), None)
    if script is None:
        print(f"找不到脚本: {raw}（已尝试: {candidates}）", file=sys.stderr)
        return 2

    sys.argv = [str(script)] + args[1:]
    # ⚠️ 顺序很关键：外部逻辑目录必须排在内置副本之前，
    #    否则 import denoise / settings 会拿到 exe 内的旧版本。
    for d in reversed(runtime.logic_paths()):
        if d not in sys.path:
            sys.path.insert(0, d)
    if str(script.parent) not in sys.path:
        sys.path.insert(0, str(script.parent))

    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
    g = {"__name__": "__main__", "__file__": str(script)}
    exec(code, g)
    return 0


def main():
    if "--run-sub" in sys.argv:
        sys.exit(run_subprocess_mode())

    # 进程级异常兜底：打包成窗口程序后 stderr 无处可去，
    # 后台线程的未捕获异常会彻底消失（"静默失败"查不出来的根因）。
    install_global_hooks()

    # 高 DPI：必须在创建任何 Tk 窗口之前声明，
    # 否则 Windows 会把整个界面当位图拉伸，字会发虚、窗口边框也不对。
    enable_dpi_awareness()

    # 单实例保护：两个窗口同抢一个浏览器配置目录必然失败
    other = _acquire_single_instance()
    if other:
        # 用原生消息框：此刻 Tk 还没初始化，messagebox 不一定弹得出来
        _native_msgbox(
            APP_TITLE,
            f"检测到本程序已经在运行（进程 {other}）。\n\n"
            "同时开两个窗口会争抢浏览器配置，导致启动失败。\n\n"
            "如果那个窗口已经找不到了，请按 Ctrl+Shift+Esc 打开任务管理器，\n"
            "结束「飞书自动回复助手.exe」进程后再重新双击本程序。",
            0x30,
        )
        return

    try:
        # 清理上次被强杀留下的浏览器锁文件（否则首次启动必失败）
        try:
            _clean_profile_locks()
        except Exception:
            pass

        if not CONFIG_FILE.exists():
            # 首次配置：先建一个隐藏根窗口承载向导（不能用第二个 Tk 当向导）
            root = tk.Tk()
            root.withdraw()
            wiz = ConfigWizard(root)
            # 显式把向导映射出来：root 是隐藏的，向导必须自己显示，
            # 否则 wait_window() 会一直空等（见 ConfigWizard 里的说明）。
            try:
                wiz.deiconify()
                wiz.update()        # 强制处理一次事件，确保窗口真正出现
                wiz.lift()
                wiz.grab_set()      # 窗口已可见后再抓取，模态才生效
            except Exception:
                pass
            wiz.wait_window()
            ok = wiz.result
            try:
                root.destroy()
            except Exception:
                pass
            if not ok:
                return

        win = MainWindow()
        win.protocol("WM_DELETE_WINDOW", win.on_close)
        win.mainloop()
    except Exception as e:
        # 启动阶段出错：windowed 模式下原本会静默退出，用户看不到任何提示。
        tb = traceback.format_exc()
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n"
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动失败\n{tb}")
        except Exception:
            pass
        _native_msgbox(
            f"{APP_TITLE} · 启动失败",
            f"{type(e).__name__}: {e}\n\n"
            f"详细信息已写入程序目录下的 launcher_error.log。",
            0x10,
        )
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
