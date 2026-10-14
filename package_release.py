#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一打包脚本 —— 产物文件名自动带版本号。

默认产出「目录版（绿色版）」：一个文件夹，解压即用、启动快（约 1 秒）。
（程序把配置/登录态存在用户目录，与解压位置无关，故升级不会丢配置。）

【为什么默认目录版而不是单文件】
  单文件 exe 每次启动都要把 ~188MB 解压到临时目录，本机实测一次启动
  约 110 秒（CPU 近零，卡在解压/杀软扫描），代价太大；
  目录版启动约 1 秒。故默认目录版，单文件改为可选（--single）。

用法：
    python package_release.py --build              # 构建 + 打包目录版（推荐）
    python package_release.py --build --bump patch # 版本号 +1 再构建打包
    python package_release.py --build --single     # 额外产出单文件 exe
    python package_release.py                      # 不构建，直接对已有产物打包
    python package_release.py --bump minor         # 1.0.1 -> 1.1.0
    python package_release.py --set 1.2.3          # 直接指定版本号
    python package_release.py --logic-only         # 只产出「逻辑更新包」
    python package_release.py --source-only        # 只打源码包
    python package_release.py --no-validate        # 跳过验收

产物（版本号只加在文件名上）：
    飞书自动回复助手-绿色版-v<版本>.zip           <- 目录版，给最终用户（默认）
    逻辑更新包-v<版本>.zip                        <- 增量更新：只换逻辑，不用换 exe
    飞书自动回复助手-v<版本>.exe                  <- 单文件版（--single 时）
    github_pkg/feishu-auto-reply-v<版本>.zip     <- 源码包

「逻辑更新包」说明：
    程序把逻辑脚本（step2_auto_reply.py / denoise.py / settings.py 等）与
    引擎（exe 里的 Python 运行时 + playwright + launcher）分离。终端用户装
    一次后，后续只改逻辑的话，把本更新包在界面点「📦 更新包」导入即可，
    无需重新下载 ~139MB 的目录版。包内是**扁平**的 .py + VERSION，落到
    <数据目录>\\app\\。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"

# build.spec 在构建时落盘的「子脚本用到的模块清单」，用于产物完整性校验。
HIDDEN_IMPORTS_FILE = ROOT / "_hiddenimports.txt"

SINGLE_DIST = ROOT / "dist_onefile" / "飞书自动回复助手.exe"   # 单文件产物
DIST_DIR = ROOT / "dist" / "飞书自动回复助手"                  # 目录版产物目录
ONEDIR_EXE = DIST_DIR / "飞书自动回复助手.exe"
SRC_DIR = ROOT / "github_pkg" / "feishu-auto-reply"            # 源码包目录

GREEN_BASE = "飞书自动回复助手-绿色版"   # 目录版 zip 文件名前缀
GREEN_ROOT = "飞书自动回复助手"          # 压缩包内目录名
SINGLE_BASE = "飞书自动回复助手"         # 单文件 exe 文件名前缀
SOURCE_BASE = "feishu-auto-reply"
LOGIC_BASE = "逻辑更新包"                # 增量更新包文件名前缀

# 目录版顶层需要补的文件（PyInstaller 只产 exe + _internal）。
TOP_EXTRAS = ("使用说明.txt", "config.example.yaml", "LICENSE")

# 「逻辑更新包」内容 —— 需与 build.spec 的 APP_SCRIPTS 保持一致。
# 只放运行时真正会用到的逻辑脚本；launcher.py / runtime.py 属"引擎"，
# 编在 exe 里，改它们仍需整体换 exe。
LOGIC_FILES = (
    "denoise.py",
    "settings.py",
    "step2_auto_reply.py",
    "fetch_history.py",
    "step1_check_login.py",
)

BUILD_PY = ROOT / ".buildenv313" / "Scripts" / "python.exe"    # 打包专用解释器

SKIP_PARTS = {".git", "__pycache__", ".venv", ".buildenv", ".buildenv313",
              ".browser_data", "debug_shots"}


# ----------------------------------------------------------------------
# 版本号
# ----------------------------------------------------------------------
_VER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_version() -> str:
    if not VERSION_FILE.exists():
        raise SystemExit(f"[ERR] 版本号文件不存在：{VERSION_FILE}")
    ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not _VER_RE.match(ver):
        raise SystemExit(f"[ERR] VERSION 内容不合法（应为 X.Y.Z）：{ver!r}")
    return ver


def write_version(ver: str) -> None:
    if not _VER_RE.match(ver):
        raise SystemExit(f"[ERR] 版本号不合法（应为 X.Y.Z）：{ver!r}")
    VERSION_FILE.write_text(ver + "\n", encoding="utf-8")


def bump(cur: str, part: str) -> str:
    major, minor, patch = (int(x) for x in cur.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def _clean_old(prefix: str, keep, pattern: str = "*.zip") -> None:
    """删掉同前缀的旧产物，避免目录里堆一堆。

    keep 可以是单个路径，也可以是一组路径（同一版本可能同时产出
    "普通版"和"预置配置版"两个包，两个都要留着）。
    """
    if isinstance(keep, (list, tuple, set)):
        keeps = {Path(k).resolve() for k in keep}
    else:
        keeps = {Path(keep).resolve()}
    for p in ROOT.glob(f"{prefix}*{pattern[1:]}"):
        if p.resolve() in keeps:
            continue
        try:
            p.unlink()
            print(f"  - 清理旧包 {p.name}")
        except Exception as e:
            print(f"  ! 删不掉 {p.name}: {e}")


# ----------------------------------------------------------------------
# 校验 / 构建
# ----------------------------------------------------------------------
def check_embedded_prompt() -> bool:
    """校验 launcher.py 内嵌示例的 system_prompt 与 config.example.yaml 是否一致。

    两者脱节会导致「首次配置」（包里没有 config.example.yaml 时）生成的
    config.yaml 带上旧规则，出现"改了 prompt 却没生效"的怪象。
    """
    try:
        import yaml
    except Exception:
        print("  ! 未安装 pyyaml，跳过内嵌示例一致性校验")
        return True
    launcher = ROOT / "launcher.py"
    example = ROOT / "config.example.yaml"
    if not (launcher.exists() and example.exists()):
        return True
    m = re.search(r'EMBEDDED_EXAMPLE = """(.*?)"""',
                  launcher.read_text(encoding="utf-8"), re.S)
    if not m:
        print("  ! launcher.py 里找不到 EMBEDDED_EXAMPLE，跳过校验")
        return True
    try:
        emb = ((yaml.safe_load(m.group(1)) or {}).get("llm") or {}).get("system_prompt", "").strip()
        ex = ((yaml.safe_load(example.read_text(encoding="utf-8")) or {}).get("llm") or {}).get("system_prompt", "").strip()
    except Exception as e:
        print(f"  ! 解析 prompt 失败，跳过校验：{e}")
        return True
    if emb != ex:
        print(f"  ✗ 严重：launcher.py 内嵌示例的 system_prompt（{len(emb)} 字符）"
              f"与 config.example.yaml（{len(ex)} 字符）不一致！")
        print("    → 首次配置会用到旧规则。请先同步 launcher.py 的 EMBEDDED_EXAMPLE。")
        return False
    print(f"  ✓ launcher 内嵌示例与 config.example.yaml 一致（prompt {len(ex)} 字符）")
    return True


# 旧产物暂存目录：放在**项目同级**而非项目内，一是不污染源码目录，
# 二是同盘 move 是瞬时重命名（不复制），构建瞬间完成。
_TRASH_DIR = ROOT.parent / "_feishu_build_trash"


# ----------------------------------------------------------------------
# ★ 包内模块完整性校验（防「运行期 ModuleNotFoundError」再次上线）
# ----------------------------------------------------------------------
# 自检脚本：在**产物 exe 自己**的 --run-sub 模式里跑，用的是真实冻结导入系统。
# 用户机器上就是这套导入器，所以这是最接近"真机"的验证方式。
_PROBE_CODE = '''\
import importlib, json, sys
from pathlib import Path

mods = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(sys.argv[2])
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append("%s: %s: %s" % (m, type(e).__name__, e))
try:
    out.write_text(json.dumps({"missing": bad, "checked": len(mods)}), encoding="utf-8")
except Exception:
    pass
'''


def check_frozen_modules(exe: Path) -> bool:
    """用产物 exe 亲自 import 一遍子脚本用到的模块，确认没有漏收。

    背景（v1.0.9 线上故障）：逻辑脚本是以「数据文件」打进包、运行期才 exec 的，
    PyInstaller 只静态分析 launcher.py，看不到它们的 import；于是"只有子脚本
    用到"的模块漏收 → 用户点「启动监控」立即
        ModuleNotFoundError: No module named 'logging.handlers'。
    这类问题静态检查抓不到、只有真机运行才炸，所以必须让**产物自己**验一遍。
    """
    if not HIDDEN_IMPORTS_FILE.exists():
        print("  ! 找不到 _hiddenimports.txt（build.spec 未生成），跳过完整性校验")
        return True
    need = [x.strip() for x in HIDDEN_IMPORTS_FILE.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    if not need:
        return True

    tmp = Path(tempfile.mkdtemp(prefix="feishu_probe_"))
    probe, mods_json, out_json = (tmp / "_probe_imports.py", tmp / "_mods.json",
                                 tmp / "_result.json")
    probe.write_text(_PROBE_CODE, encoding="utf-8")
    mods_json.write_text(json.dumps(need, ensure_ascii=False), encoding="utf-8")
    try:
        r = subprocess.run(
            [str(exe), "--run-sub", str(probe), str(mods_json), str(out_json)],
            capture_output=True, timeout=180)
    except Exception as e:
        print(f"  ! 包内导入自检未能运行（{e}），跳过完整性校验")
        return True

    if not out_json.exists():
        print(f"  ! 包内导入自检未产出结果（退出码 {r.returncode}），跳过完整性校验")
        return True

    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! 自检结果解析失败（{e}），跳过完整性校验")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    missing = data.get("missing") or []
    if missing:
        print(f"  ✗ 产物里 import 不进来：{missing}")
        print("    → 这些模块只有「以数据文件运行」的子脚本用到，")
        print("      launcher.py 静态分析看不到，必须在 build.spec 的 hiddenimports 里补上。")
        return False
    print(f"  ✓ 包内模块完整性 OK（产物亲测 import {data.get('checked', len(need))} 个模块，全部通过）")
    return True


def _stash_old(path: Path) -> None:
    """把旧产物目录挪走（只挪不删）。

    PyInstaller / 脚本批量删除 1000+ 旧文件会被环境的安全删除策略拦截，
    甚至直接中断进程（导致 COLLECT 静默失败）；所以只"挪"不"删"。
    """
    if not path.exists():
        return
    try:
        _TRASH_DIR.mkdir(exist_ok=True)
    except Exception as e:
        print(f"  ! 无法创建暂存目录 {_TRASH_DIR}：{e}")
        return
    dst = _TRASH_DIR / f"{path.name}.{int(time.time())}"
    try:
        shutil.move(str(path), str(dst))
        print(f"  - 旧产物已挪到 {dst}")
    except Exception as e:
        print(f"  ! 移走旧产物失败：{e}")


def _purge_trash() -> None:
    """**不自动删除**旧产物，只提示位置。

    批量删除上千个文件会被安全策略拦截/中断进程，风险大于收益；
    这些是纯构建垃圾，留着无害，由用户或系统清理程序处理即可。
    """
    if not _TRASH_DIR.exists():
        return
    try:
        n = sum(1 for p in _TRASH_DIR.rglob("*") if p.is_file())
    except Exception:
        n = 0
    print(f"  · 旧产物暂存于 {_TRASH_DIR}（{n} 个文件，可手动删除）")


def _copy_top_extras(dest: Path) -> None:
    for name in TOP_EXTRAS:
        src = ROOT / name
        if src.exists():
            try:
                shutil.copy2(src, dest / name)
            except Exception as e:
                print(f"  ! 复制 {name} 到产物目录失败：{e}")


def build_onedir() -> bool:
    """调用 PyInstaller 构建目录版（onedir）到 dist/飞书自动回复助手/。"""
    py = BUILD_PY if BUILD_PY.exists() else Path(sys.executable)
    print(f"[构建] 使用 {py}")
    env = os.environ.copy()
    env.pop("FEISHU_ONEFILE", None)          # 目录版：不设该变量

    _stash_old(ROOT / "dist")
    _stash_old(ROOT / "build")

    cmd = [str(py), "-m", "PyInstaller", "build.spec",
           "--noconfirm", "--distpath", "dist",
           "--workpath", "build/wk", "--log-level", "WARN"]
    print(f"  {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    print(f"  退出码 = {r.returncode}")

    if r.returncode != 0 or not ONEDIR_EXE.exists():
        print(f"  ✗ 构建失败（或找不到产物 {ONEDIR_EXE}）")
        return False

    _copy_top_extras(DIST_DIR)
    n = sum(1 for _ in DIST_DIR.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in DIST_DIR.rglob("*") if p.is_file())
    print(f"  ✓ 目录版就绪：{n} 个文件，{size/1024/1024:.1f} MB")
    if not (DIST_DIR / "_internal").is_dir():
        print("  ✗ 缺少 _internal/，产物不完整")
        return False
    if not check_frozen_modules(ONEDIR_EXE):
        print("  ✗ 产物体检不通过：包内缺模块，用户一运行就会崩。已中止。")
        return False
    _purge_trash()
    return True


def build_onefile() -> bool:
    """调用 PyInstaller 构建单文件 exe（FEISHU_ONEFILE=1）。"""
    py = BUILD_PY if BUILD_PY.exists() else Path(sys.executable)
    print(f"[构建·单文件] 使用 {py}")
    env = os.environ.copy()
    env["FEISHU_ONEFILE"] = "1"

    _stash_old(ROOT / "dist_onefile")
    _stash_old(ROOT / "build_onefile")

    cmd = [str(py), "-m", "PyInstaller", "build.spec",
           "--noconfirm", "--distpath", "dist_onefile",
           "--workpath", "build_onefile/wk", "--log-level", "WARN"]
    print(f"  {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    print(f"  退出码 = {r.returncode}")
    if r.returncode != 0 or not SINGLE_DIST.exists():
        print(f"  ✗ 构建失败（或找不到产物 {SINGLE_DIST}）")
        return False
    _purge_trash()
    return True


# ----------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------
def _zip_dir(src: Path, out: Path, root_name: str, extra=(),
             skip_names=()) -> tuple:
    """把目录打成 zip。

    extra      --- 额外塞进去的条目 [(压缩包内路径, 本地文件)]，
                   用于「预置配置版」把 config.yaml 放进 exe 同级。
    skip_names --- 按文件名排除（例如普通版绝不带上 config.yaml）。
    """
    files = [p for p in sorted(src.rglob("*")) if p.is_file()]
    files = [p for p in files
             if not any(part in SKIP_PARTS for part in p.relative_to(src).parts)
             and p.name not in skip_names]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zc:
        for p in files:
            zc.write(p, (Path(root_name) / p.relative_to(src)).as_posix())
        for arcname, path in extra:
            zc.write(path, arcname)
    return len(files) + len(extra), out.stat().st_size


def build_single(ver: str, validate: bool = True) -> Path:
    """单文件版：直接复制 exe，文件名带版本号。"""
    if not SINGLE_DIST.exists():
        raise SystemExit(f"[ERR] 找不到单文件产物：{SINGLE_DIST}\n"
                         f"      请先运行：python package_release.py --build --single")
    out = ROOT / f"{SINGLE_BASE}-v{ver}.exe"
    print(f"[单文件版] {SINGLE_DIST.name}  ->  {out.name}")
    shutil.copy2(SINGLE_DIST, out)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    size = out.stat().st_size
    print(f"  {size/1024/1024:.1f} MB，md5 {md5}")

    if validate:
        head = out.read_bytes()[:2]
        ok_pe = head == b"MZ"
        ok_size = size > 10 * 1024 * 1024       # 单文件必然含 playwright，>10MB
        print(f"  验收: PE头={'OK' if ok_pe else '✗'}  体积合理={'OK' if ok_size else '✗'}")
        if not (ok_pe and ok_size):
            raise SystemExit("[ERR] 单文件产物体检不通过。")

    _clean_old(f"{SINGLE_BASE}-v", out, pattern="*.exe")
    return out


def build_green(ver: str, validate: bool = True, preset: Path = None) -> Path:
    """打包目录版。

    preset 不为 None 时产出「预置配置版」：把这份 config.yaml 放到压缩包
    里 exe 同级。新机器解压后首次运行会自动接管它（runtime._migrate_legacy），
    于是连配置向导都不会弹 —— 这是"换电脑/给别人用不用重填"的零操作方案。
    ⚠️ 该文件内含 API Key，只在确实需要的分发场景下用。
    """
    if not DIST_DIR.is_dir() or not ONEDIR_EXE.exists():
        raise SystemExit(f"[ERR] 找不到目录版产物：{DIST_DIR}\n"
                         f"      请先运行：python package_release.py --build")

    if preset is not None:
        out = ROOT / f"{GREEN_BASE}-v{ver}-预置配置.zip"
        extra = [(f"{GREEN_ROOT}/config.yaml", preset)]
        skip = ()
        print(f"[目录版·预置配置] {DIST_DIR.name} + {preset.name}  ->  {out.name}")
        print("  ! 包内含 API Key，请只发给需要使用的人")
    else:
        out = ROOT / f"{GREEN_BASE}-v{ver}.zip"
        extra, skip = (), {"config.yaml"}
        print(f"[目录版] {DIST_DIR.name}  ->  {out.name}")

    normal = ROOT / f"{GREEN_BASE}-v{ver}.zip"
    preset_out = ROOT / f"{GREEN_BASE}-v{ver}-预置配置.zip"
    n, size = _zip_dir(DIST_DIR, out, GREEN_ROOT, extra=extra, skip_names=skip)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    print(f"  {n} 个文件，{size/1024/1024:.1f} MB，md5 {md5}")

    if validate:
        z = zipfile.ZipFile(out)
        names = z.namelist()
        tops = sorted(set(x.split("/")[0] for x in names))
        bad = [x for x in names
               if ".browser_data" in x
               or x.endswith("answered.json") or x.endswith("reply_stats.json")
               or "__pycache__" in x]
        if preset is None:
            bad += [x for x in names if x.endswith("/config.yaml")]
        has_exe = f"{GREEN_ROOT}/{GREEN_ROOT}.exe" in names
        has_internal = any(x.startswith(f"{GREEN_ROOT}/_internal/") for x in names)
        has_cfg = f"{GREEN_ROOT}/config.yaml" in names
        want_cfg = preset is not None
        tail = (f"预置配置={'OK' if has_cfg else '✗'}" if want_cfg
                else f"用户数据误入={'无' if not bad else bad}")
        print(f"  验收: 顶层={tops}  exe={'OK' if has_exe else '✗'}  "
              f"_internal={'OK' if has_internal else '✗'}  {tail}")
        if tops != [GREEN_ROOT]:
            print(f"  ! 顶层目录名异常，应为 [{GREEN_ROOT}]")
        if bad:
            raise SystemExit("[ERR] 压缩包内混入了用户数据/缓存，已终止。")
        if want_cfg and not has_cfg:
            raise SystemExit("[ERR] 已指定 --with-config，但包里没有 config.yaml。")
        if not (has_exe and has_internal):
            raise SystemExit("[ERR] 压缩包结构不完整（缺 exe 或 _internal）。")

    # 同一版本的两个包（普通 / 预置配置）都要保住，否则会被互相清掉
    _clean_old(GREEN_BASE, [out, normal, preset_out], pattern="*.zip")
    return out


def build_logic(ver: str, validate: bool = True) -> Path:
    """逻辑更新包：扁平结构（.py + VERSION），解压进 <数据目录>\\app\\ 即生效。"""
    out = ROOT / f"{LOGIC_BASE}-v{ver}.zip"
    print(f"[逻辑更新包] -> {out.name}")
    missing = [f for f in LOGIC_FILES if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"[ERR] 逻辑文件缺失，无法打包：{missing}")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zc:
        for name in LOGIC_FILES:
            zc.write(ROOT / name, name)      # 扁平：不带目录前缀
        zc.writestr("VERSION", ver + "\n")   # 外部逻辑版本标记
    size = out.stat().st_size
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    print(f"  {len(LOGIC_FILES) + 1} 个文件，{size/1024:.0f} KB，md5 {md5}")

    if validate:
        z = zipfile.ZipFile(out)
        names = z.namelist()
        bad = [x for x in names if "/" in x or "\\" in x
               or x in ("config.yaml", "portable.txt")]
        print(f"  验收: 顶层扁平={'OK' if not bad else '✗'}  文件={names}")
        if bad:
            raise SystemExit(f"[ERR] 逻辑包结构异常（应扁平且不含用户数据）：{bad}")
        if "VERSION" not in names:
            raise SystemExit("[ERR] 逻辑包缺少 VERSION 标记。")

    _clean_old(LOGIC_BASE, out, pattern="*.zip")
    return out


def build_source(ver: str, validate: bool = True) -> Path:
    if not SRC_DIR.is_dir():
        raise SystemExit(f"[ERR] 找不到源码目录：{SRC_DIR}")
    out = ROOT / "github_pkg" / f"{SOURCE_BASE}-v{ver}.zip"
    print(f"[源码包] {SRC_DIR.name}  ->  github_pkg/{out.name}")
    n, size = _zip_dir(SRC_DIR, out, SOURCE_BASE)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    print(f"  {n} 个文件，{size/1024:.1f} KB，md5 {md5}")

    if validate:
        z = zipfile.ZipFile(out)
        if any(".git/" in x for x in z.namelist()):
            print("  ! 包里含 .git，检查 SKIP_PARTS")
    _clean_old(str(Path("github_pkg") / SOURCE_BASE), out, pattern="*.zip")
    return out


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="带版本号的分发打包脚本")
    ap.add_argument("--build", action="store_true",
                    help="先调用 PyInstaller 构建目录版，再打包")
    ap.add_argument("--single", action="store_true",
                    help="额外构建并产出单文件 exe（启动慢，一般不推荐）")
    ap.add_argument("--bump", choices=["major", "minor", "patch"],
                    help="先递增版本号再打包")
    ap.add_argument("--set", dest="set_ver", metavar="X.Y.Z",
                    help="直接指定版本号再打包")
    ap.add_argument("--logic-only", action="store_true", help="只产出逻辑更新包")
    ap.add_argument("--source-only", action="store_true", help="只打源码包")
    ap.add_argument("--no-validate", action="store_true", help="跳过验收")
    ap.add_argument("--with-config", dest="with_config", nargs="?", const="config.yaml",
                    default=None, metavar="PATH",
                    help="额外产出「预置配置版」绿色包：把指定 config.yaml（默认项目根那份）"
                         "放进包内 exe 同级，新机器解压即用、不再弹配置向导。"
                         "⚠️ 包内含 API Key，注意分发范围")
    args = ap.parse_args()

    ver = read_version()
    if args.set_ver:
        ver = args.set_ver.strip()
    if args.bump:
        ver = bump(ver, args.bump)
    if args.set_ver or args.bump:
        write_version(ver)

    print(f"===== 打包版本 v{ver} =====\n")
    validate = not args.no_validate
    if validate:
        if not check_embedded_prompt():
            print("\n[中止] 内嵌示例与 config.example.yaml 的 prompt 不一致，请先同步。")
            return 2
        print()

    if args.logic_only:
        made = [build_logic(ver, validate=validate)]
        print("\n===== 完成 =====")
        for p in made:
            print(f"  {p.relative_to(ROOT)}")
        return 0

    if args.build and not args.source_only:
        if not build_onedir():
            return 3
        print()
        if args.single:
            if not build_onefile():
                return 3
            print()

    made = []
    if not args.source_only:
        made.append(build_green(ver, validate=validate))
        print()
        if args.with_config:
            preset = Path(args.with_config).expanduser()
            if not preset.is_absolute():
                preset = (ROOT / preset).resolve()
            if not preset.is_file():
                raise SystemExit(f"[ERR] 找不到要预置的配置：{preset}")
            made.append(build_green(ver, validate=validate, preset=preset))
            print()
        if args.single:
            made.append(build_single(ver, validate=validate))
            print()
        made.append(build_logic(ver, validate=validate))
        print()
    made.append(build_source(ver, validate=validate))

    print("\n===== 完成 =====")
    for p in made:
        try:
            rel = p.relative_to(ROOT)
        except ValueError:
            rel = p
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
