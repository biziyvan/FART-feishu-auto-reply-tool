# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置
====================
支持两种产物形态，由环境变量 FEISHU_ONEFILE 控制：

  （未设置）        →  目录版：dist/飞书自动回复助手/（exe + _internal/）★默认分发形态
  FEISHU_ONEFILE=1  →  单文件：dist/飞书自动回复助手.exe（一个 exe）

★为什么默认目录版：单文件每次启动要解压 ~188MB 到临时目录，实测一次
  约 110 秒（CPU 近零，卡在解压/杀软扫描）；目录版启动约 1 秒。
  故默认目录版，单文件仅作可选（package_release.py --single）。

关键点：
  1. playwright 包内自带 node 驱动（driver/ 目录，约 105MB），必须整包收集，
     否则运行时报 "Driver not found"。
  2. launcher.py 通过 --run-sub 复用同一个 exe 来跑子脚本，
     因此 step2_auto_reply.py / denoise.py 等必须以 .py 数据文件形式
     打进包内（而非编译进 exe），供运行时读取执行。
  3. tkinter 需要显式收集 tcl/tk 运行库。
  4. 数据文件（config.yaml / .browser_data / 日志）一律走 runtime.DATA_DIR，
     单文件模式下不会写进临时解压目录（否则一关就丢）。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# ------------------------------------------------------------
# 项目根目录（本 spec 所在目录）
# ------------------------------------------------------------
ROOT = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821 (SPEC 由 PyInstaller 注入)

ONEFILE = os.environ.get("FEISHU_ONEFILE", "").strip() in ("1", "true", "yes")

datas = []
binaries = []
hiddenimports = []

# ---- 1. playwright：驱动 + 子模块 ----
pw_datas, pw_bins, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_bins
hiddenimports += pw_hidden

# ---- 2. 其余第三方库 ----
for pkg in ("yaml", "requests", "urllib3", "certifi", "charset_normalizer", "idna"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# ---- 3. tkinter ----
hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.filedialog",
                  "tkinter.messagebox", "tkinter.scrolledtext"]

# ---- 4. 本项目脚本：作为数据文件打进包，供 --run-sub 读取执行 ----
# 只放**运行期真正会用到**的逻辑脚本（与 package_release.py 的 LOGIC_FILES 对应）。
# test_* / diag_* 等开发与诊断脚本**不进发行包** —— 它们只存在于源码仓库，
# 用 .venv 解释器直接运行即可（见 README「开发与打包 → 测试 / 诊断工具」）。
# 历史遗留：test_send / test_background / test_occlusion / diag_dom / diag_page
# 曾被误打进 _internal/，属面向最终用户的载荷冗余，v1.2.1 起移除。
APP_SCRIPTS = [
    "runtime.py",
    "settings.py",
    "denoise.py",
    "step2_auto_reply.py",
    "fetch_history.py",
    "step1_check_login.py",
]
for name in APP_SCRIPTS:
    p = os.path.join(ROOT, name)
    if os.path.exists(p):
        datas.append((p, "."))
    else:
        print(f"[build.spec] !!! 警告：脚本缺失 {p}，将不会打进包 !!!")

# ---- 4b. ★ 自动收集「子脚本」的 import 作为 hiddenimports ----
# ★★ 这是本项目最容易翻车的地方，务必保留 ★★
# APP_SCRIPTS 是**数据文件**，运行期才由 launcher.py 用 exec 执行；
# PyInstaller 的静态分析只看得到 launcher.py，**看不到它们的 import**。
# 于是"只有子脚本用到"的模块不会被收进 PYZ → 运行期 ModuleNotFoundError。
# 真实故障（v1.0.9 绿色版）：点「启动监控」立刻崩：
#   step2_auto_reply.py line 37: from logging.handlers import RotatingFileHandler
#   ModuleNotFoundError: No module named 'logging.handlers'
#   （launcher.py 从不 import logging，logging 只因 urllib3 传递依赖才进包，
#     而 logging.handlers 是独立子模块，没人 import 就没有。）
# 所以这里用 ast 扫一遍 APP_SCRIPTS，把它们用到的模块全部显式声明。
import ast  # noqa: E402
import importlib.util  # noqa: E402

_LOCAL_MODS = {os.path.splitext(n)[0] for n in APP_SCRIPTS}


def _scan_imports(path):
    """返回某个 .py 顶层/from 导入的模块名（不含 from ... import 的成员名）。"""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
    except Exception as e:
        print(f"[build.spec] ! 解析 {path} 失败，跳过扫描：{e}")
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module)
    return mods


def _resolvable(mod):
    """确认模块可被 PyInstaller 找到（避免塞进无效 hiddenimport 触发告警）。"""
    if mod in _LOCAL_MODS or mod.startswith("__"):
        return False
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


_auto = set()
for _name in APP_SCRIPTS:
    _p = os.path.join(ROOT, _name)
    if not os.path.exists(_p):
        continue
    for _m in _scan_imports(_p):
        _top = _m.split(".")[0]
        # 同时收「点号全名」和「顶层包名」：前者精确定位子模块（logging.handlers），
        # 后者保证包本体一定在（logging）。
        for _cand in (_m, _top):
            if _resolvable(_cand):
                _auto.add(_cand)
hiddenimports += sorted(_auto)
print(f"[build.spec] 子脚本自动收集 hiddenimports（{len(_auto)} 项）：{sorted(_auto)}")

# 把这批模块名落盘，供 package_release.py 在构建后核对「产物里真的有」。
# 单一事实来源：校验方直接读这份清单，不会再算一遍导致口径不一致。
try:
    with open(os.path.join(ROOT, "_hiddenimports.txt"), "w", encoding="utf-8") as _f:
        _f.write("\n".join(sorted(_auto)) + ("\n" if _auto else ""))
except Exception as _e:
    print(f"[build.spec] ! 写入 _hiddenimports.txt 失败：{_e}")

# ---- 5. 配置模板 ----
# 注意：这个文件一旦漏打包，首次配置向导会因找不到模板而失败。
# launcher.py 内已内置 EMBEDDED_EXAMPLE 作为兜底，但这里仍然
# 显式检查，缺失时大声报警，避免"静默漏掉"。
example = os.path.join(ROOT, "config.example.yaml")
if os.path.exists(example):
    datas.append((example, "."))
    print(f"[build.spec] 已包含配置模板: {example}")
else:
    print(f"[build.spec] !!! 警告：找不到配置模板 {example}，"
          f"将依赖 launcher.py 的内嵌兜底配置 !!!")

# ---- 6. 使用说明 ----
for extra in ("使用说明.txt", "LICENSE"):
    p = os.path.join(ROOT, extra)
    if os.path.exists(p):
        datas.append((p, "."))

# ---- 7. 版本号 ----
# runtime.py 用它显示"内置逻辑版本"，也是增量更新包版本比对的基准。
ver_file = os.path.join(ROOT, "VERSION")
if os.path.exists(ver_file):
    datas.append((ver_file, "."))
    print(f"[build.spec] 已包含版本号: {open(ver_file, encoding='utf-8').read().strip()}")
else:
    print("[build.spec] !!! 警告：找不到 VERSION，界面将显示「未知」!!!")


block_cipher = None

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL",
        "pytest", "setuptools", "pip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

_ICON = os.path.join(ROOT, "app.ico")
_ICON = _ICON if os.path.exists(_ICON) else None

if ONEFILE:
    # 单文件：把 binaries / datas 全部塞进 EXE，不生成 _internal 目录
    print("[build.spec] 模式 = 单文件 (onefile)")
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="飞书自动回复助手",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,          # GUI 程序：不弹黑窗
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
    )
else:
    print("[build.spec] 模式 = 目录版 (onedir)")
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="飞书自动回复助手",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,          # GUI 程序：不弹黑窗
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="飞书自动回复助手",
    )
