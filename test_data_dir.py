# -*- coding: utf-8 -*-
"""
数据目录解析 —— 自检脚本
=========================
验证 runtime.py 的"数据目录"机制。这是"换电脑 / 重新解压要不要重填配置"
的唯一决定点，出错代价很大（用户以为配置丢了，实际是读了另一个目录）。

覆盖：
  1. 打包模式下按优先级选目录：环境变量 > portable.txt > 指针 > 用户目录
  2. **换解压位置不改变数据目录**（最核心的一条，就是用户报的那个问题）
  3. 指针文件非法 / 目标不存在 / 环境变量给了坏路径 —— 都不能让程序起不来
  4. set_data_dir()：迁移数据、写指针、拒绝同路径与相对路径、不覆盖同名
  5. reset_data_dir()：只删指针不搬数据

运行：python test_data_dir.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import runtime  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"    [{extra}]" if extra else ""))


TMP = Path(tempfile.mkdtemp(prefix="feishu_dd_"))
LAD = TMP / "LocalAppData"                       # 冒充 %LOCALAPPDATA%
EXEDIR = TMP / "飞书自动回复助手-绿色版" / "飞书自动回复助手"
EXEDIR.mkdir(parents=True, exist_ok=True)
EXE = EXEDIR / "飞书自动回复助手.exe"
EXE.write_bytes(b"MZ")
DEFDIR = LAD / "飞书自动回复助手"                 # 默认数据目录

_orig_exe_dir = runtime._exe_dir
_orig_frozen = getattr(sys, "frozen", None)
_had_frozen = hasattr(sys, "frozen")
_orig_frozen_flag = runtime.IS_FROZEN
_orig_data_dir = runtime.DATA_DIR
_env_lad = os.environ.get("LOCALAPPDATA")
_env_dd = os.environ.get("FEISHU_DATA_DIR")


def setup():
    """每个场景开始前把环境重置成"干净的新机器"。"""
    shutil.rmtree(LAD, ignore_errors=True)
    LAD.mkdir(parents=True, exist_ok=True)
    if (EXEDIR / "portable.txt").exists():
        (EXEDIR / "portable.txt").unlink()
    os.environ["LOCALAPPDATA"] = str(LAD)
    os.environ.pop("FEISHU_DATA_DIR", None)
    sys.frozen = True                      # 伪装打包模式
    runtime.IS_FROZEN = True
    runtime._exe_dir = lambda: EXEDIR


try:
    # ===============================================================
    print("\n[1] 优先级：默认用户目录")
    setup()
    got = runtime._resolve_data_dir()
    check("无 portable / 无指针 → 用户目录", got == DEFDIR, str(got))
    check("目录会被创建", DEFDIR.is_dir())

    # ===============================================================
    print("\n[2] 优先级：portable.txt（便携模式）")
    setup()
    (EXEDIR / "portable.txt").write_text("", encoding="utf-8")
    got = runtime._resolve_data_dir()
    check("portable.txt → 数据随 exe 文件夹", got == EXEDIR, str(got))

    # ===============================================================
    print("\n[3] 优先级：指针文件（界面「更改数据目录」写的就是它）")
    setup()
    DEFDIR.mkdir(parents=True, exist_ok=True)
    custom = TMP / "自定义数据位置"
    custom.mkdir(exist_ok=True)
    (DEFDIR / "location.txt").write_text(str(custom), encoding="utf-8")
    got = runtime._resolve_data_dir()
    check("指针 → 使用自定义位置", got == custom, str(got))
    check("指针文件在默认目录里（自己不能跟着数据走）",
          runtime.location_file() == DEFDIR / "location.txt")

    setup()
    (EXEDIR / "portable.txt").write_text("", encoding="utf-8")
    DEFDIR.mkdir(parents=True, exist_ok=True)
    (DEFDIR / "location.txt").write_text(str(custom), encoding="utf-8")
    check("portable.txt 优先于指针（同目录显式意图更强）",
          runtime._resolve_data_dir() == EXEDIR)

    # ===============================================================
    print("\n[4] 非法 / 失效指针一律安全退回默认（绝不能让程序起不来）")
    setup()
    DEFDIR.mkdir(parents=True, exist_ok=True)
    (DEFDIR / "location.txt").write_text("这不是绝对路径", encoding="utf-8")
    check("相对路径 → 退回默认", runtime._resolve_data_dir() == DEFDIR)
    (DEFDIR / "location.txt").write_text("   \n", encoding="utf-8")
    check("空白内容 → 退回默认", runtime._resolve_data_dir() == DEFDIR)
    (DEFDIR / "location.txt").write_text(str(TMP / "新位置"), encoding="utf-8")
    got = runtime._resolve_data_dir()
    check("目标不存在 → 自动创建并使用", got == TMP / "新位置" and got.is_dir(), str(got))

    # ===============================================================
    print("\n[5] 环境变量 FEISHU_DATA_DIR")
    setup()
    os.environ["FEISHU_DATA_DIR"] = str(TMP / "envdir")
    check("环境变量最优先", runtime._resolve_data_dir() == TMP / "envdir")
    # 父路径是个文件 → mkdir 必然失败。以前会直接抛异常把程序带崩。
    os.environ["FEISHU_DATA_DIR"] = str(EXE / "不可能")
    check("坏路径不崩、按后续规则兜底", runtime._resolve_data_dir() == DEFDIR)

    # ===============================================================
    print("\n[6] ★ 换解压位置不改变数据目录（用户报的那个问题）")
    setup()
    first = runtime._resolve_data_dir()
    runtime._exe_dir = lambda: TMP / "另一个解压目录-新版本"
    second = runtime._resolve_data_dir()
    check("解压到别的文件夹 → 数据目录不变", first == second, f"{first} vs {second}")
    check("数据目录不在解压目录内", EXEDIR not in second.parents and second != EXEDIR)
    runtime._exe_dir = lambda: EXEDIR

    # 旧版本把 config.yaml 写在 exe 旁边 → 首次运行自动迁移（一次性）
    setup()
    (EXEDIR / "config.yaml").write_text("feishu: {base_url: x}\n", encoding="utf-8")
    got = runtime._resolve_data_dir()
    check("旧版就地升级：exe 旁 config.yaml 自动搬进数据目录",
          got == DEFDIR and (DEFDIR / "config.yaml").exists())
    check("迁移不会搬走浏览器登录态（几百 MB，重扫一次更划算）",
          not (DEFDIR / ".browser_data").exists())
    # 已经迁移过之后，再解压新包不该被旧文件覆盖
    before = (DEFDIR / "config.yaml").read_text(encoding="utf-8")
    (EXEDIR / "config.yaml").write_text("feishu: {base_url: 旧包里的}\n", encoding="utf-8")
    runtime._resolve_data_dir()
    check("数据目录已有配置 → 不被包里的旧配置覆盖",
          (DEFDIR / "config.yaml").read_text(encoding="utf-8") == before)

    # ===============================================================
    print("\n[7] set_data_dir()：迁移 + 写指针")
    setup()
    old = TMP / "旧数据目录"
    old.mkdir(parents=True, exist_ok=True)
    (old / "config.yaml").write_text("feishu: {base_url: 本地}\n", encoding="utf-8")
    (old / "answered.json").write_text("[]", encoding="utf-8")
    (old / "live.log").write_text("x\n", encoding="utf-8")
    sub = old / ".browser_data"
    sub.mkdir()
    (sub / "Cookies").write_bytes(b"cookie")
    runtime.DATA_DIR = old

    new = TMP / "固定位置-我选的"
    okc, msg = runtime.set_data_dir(new)
    check("返回成功", okc, msg)
    check("小文件已迁移", (new / "config.yaml").exists() and (new / "answered.json").exists())
    check("浏览器登录态一并迁移（同盘是瞬时重命名）", (new / ".browser_data" / "Cookies").exists())
    check("原目录已空", not any(old.iterdir()))
    loc = DEFDIR / "location.txt"
    check("指针已写入默认目录", loc.is_file() and loc.read_text(encoding="utf-8").strip() == str(new))
    check("再次解析 → 指向新位置", runtime._resolve_data_dir() == new)

    okc, msg = runtime.set_data_dir(new)
    check("同路径被拒绝", okc is False, msg)
    okc, msg = runtime.set_data_dir("相对路径不行")
    check("相对路径被拒绝", okc is False, msg)

    # 目标里已有同名文件 → 保留目标那份，不覆盖
    setup()
    src2 = TMP / "src2"
    src2.mkdir(parents=True, exist_ok=True)
    (src2 / "config.yaml").write_text("新的\n", encoding="utf-8")
    dst2 = TMP / "dst2"
    dst2.mkdir(parents=True, exist_ok=True)
    (dst2 / "config.yaml").write_text("旧的\n", encoding="utf-8")
    runtime.DATA_DIR = src2
    runtime.set_data_dir(dst2)
    check("目标已有同名项 → 保留目标那份", (dst2 / "config.yaml").read_text(encoding="utf-8") == "旧的\n")

    # ===============================================================
    print("\n[8] reset_data_dir()：只删指针，不动数据")
    setup()
    kept = TMP / "保留的数据"
    kept.mkdir(parents=True, exist_ok=True)
    (kept / "config.yaml").write_text("x\n", encoding="utf-8")
    DEFDIR.mkdir(parents=True, exist_ok=True)
    (DEFDIR / "location.txt").write_text(str(kept), encoding="utf-8")
    okc, msg = runtime.reset_data_dir()
    check("返回成功", okc, msg)
    check("指针已删除", not (DEFDIR / "location.txt").exists())
    check("数据一个没动", (kept / "config.yaml").exists())
    check("解析回到默认目录", runtime._resolve_data_dir() == DEFDIR)

    # ===============================================================
    print("\n[9] data_dir_source()：界面上给用户看的来源说明")
    setup()
    check("默认 → 用户目录（默认）", runtime.data_dir_source() == "用户目录（默认）",
          runtime.data_dir_source())
    (EXEDIR / "portable.txt").write_text("", encoding="utf-8")
    check("便携 → 便携模式", "便携" in runtime.data_dir_source())
    (EXEDIR / "portable.txt").unlink()
    DEFDIR.mkdir(parents=True, exist_ok=True)
    (DEFDIR / "location.txt").write_text(str(TMP), encoding="utf-8")
    check("指针 → 自定义位置", runtime.data_dir_source() == "自定义位置")
    os.environ["FEISHU_DATA_DIR"] = str(TMP / "e3")
    check("环境变量最优先", "FEISHU_DATA_DIR" in runtime.data_dir_source())

finally:
    # —— 还原（这个脚本会在别的自检里也可能被 import，别留副作用）——
    runtime._exe_dir = _orig_exe_dir
    runtime.IS_FROZEN = _orig_frozen_flag
    runtime.DATA_DIR = _orig_data_dir
    if _had_frozen:
        sys.frozen = _orig_frozen
    elif hasattr(sys, "frozen"):
        del sys.frozen
    if _env_lad is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _env_lad
    if _env_dd is None:
        os.environ.pop("FEISHU_DATA_DIR", None)
    else:
        os.environ["FEISHU_DATA_DIR"] = _env_dd
    shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 56)
print(f"  通过 {PASS} / 失败 {FAIL}")
print("=" * 56)
sys.exit(1 if FAIL else 0)
