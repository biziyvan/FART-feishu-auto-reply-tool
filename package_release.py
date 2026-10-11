#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一打包脚本 —— 分发包文件名自动带版本号。

用法：
    python package_release.py                      # 按当前 VERSION 打包
    python package_release.py --bump patch         # 版本号 +1 (1.0.0 -> 1.0.1) 再打包
    python package_release.py --bump minor         # 1.0.1 -> 1.1.0
    python package_release.py --bump major         # 1.1.0 -> 2.0.0
    python package_release.py --set 1.2.3          # 直接指定版本号再打包
    python package_release.py --source-only        # 只打源码包
    python package_release.py --no-validate        # 跳过解压验收

产物（版本号只加在文件名上，压缩包内目录名保持不变）：
    飞书自动回复助手-绿色版-v<版本>.zip           <- 给最终用户
    github_pkg/feishu-auto-reply-v<版本>.zip      <- 源码包
"""
import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"

DIST_DIR = ROOT / "dist" / "飞书自动回复助手"          # PyInstaller 产物
SRC_DIR = ROOT / "github_pkg" / "feishu-auto-reply"    # 源码包目录

GREEN_BASE = "飞书自动回复助手-绿色版"   # zip 文件名前缀（带版本号）
GREEN_ROOT = "飞书自动回复助手"          # 压缩包内的目录名（不带版本号，保持稳定）
SOURCE_BASE = "feishu-auto-reply"

# 打包源码包时要排除的目录
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


# ----------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------
def _clean_old(prefix: str, keep: Path) -> None:
    """删掉同前缀的旧 zip（含无版本号的），避免目录里堆一堆。"""
    for p in ROOT.glob(f"{prefix}*.zip"):
        if p.resolve() == keep.resolve():
            continue
        try:
            p.unlink()
            print(f"  - 清理旧包 {p.name}")
        except Exception as e:
            print(f"  ! 删不掉 {p.name}: {e}")


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


def _zip_dir(src: Path, out: Path, root_name: str) -> tuple:
    """把 src 目录打成 out，压缩包内前缀为 root_name/。返回 (文件数, 字节)。"""
    files = [p for p in sorted(src.rglob("*")) if p.is_file()]
    files = [p for p in files
             if not any(part in SKIP_PARTS for part in p.relative_to(src).parts)]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zc:
        for p in files:
            zc.write(p, (Path(root_name) / p.relative_to(src)).as_posix())
    return len(files), out.stat().st_size


def _front_files(src: Path, out: Path) -> list:
    return sorted(n for n in zipfile.ZipFile(out).namelist() if n.count("/") == 1)


def build_green(ver: str, validate: bool = True) -> Path:
    if not DIST_DIR.is_dir():
        raise SystemExit(f"[ERR] 找不到打包产物目录：{DIST_DIR}\n"
                         f"      请先运行 PyInstaller 生成 dist/飞书自动回复助手/")
    out = ROOT / f"{GREEN_BASE}-v{ver}.zip"
    print(f"[绿色版] {DIST_DIR.name}  ->  {out.name}")
    n, size = _zip_dir(DIST_DIR, out, GREEN_ROOT)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    print(f"  {n} 个文件，{size/1024/1024:.1f} MB，md5 {md5}")

    if validate:
        z = zipfile.ZipFile(out)
        tops = sorted(set(x.split("/")[0] for x in z.namelist()))
        bad = [x for x in z.namelist()
               if x.endswith("/config.yaml") or ".browser_data" in x
               or x.endswith("answered.json") or x.endswith("reply_stats.json")]
        print(f"  验收: 顶层={tops}  用户数据误入={'无' if not bad else bad}")
        if tops != [GREEN_ROOT]:
            print(f"  ! 顶层目录名异常，应为 [{GREEN_ROOT}]")
        if bad:
            raise SystemExit("[ERR] 压缩包内混入了用户数据，已终止。")
        exe_name = f"{GREEN_ROOT}/{GREEN_ROOT}.exe"
        if exe_name not in z.namelist():
            print(f"  ! 未在包内找到 {exe_name}")

    _clean_old(GREEN_BASE, out)
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
    _clean_old(str(Path("github_pkg") / SOURCE_BASE), out)
    return out


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="带版本号的分发包打包脚本")
    ap.add_argument("--bump", choices=["major", "minor", "patch"],
                    help="先递增版本号再打包")
    ap.add_argument("--set", dest="set_ver", metavar="X.Y.Z",
                    help="直接指定版本号再打包")
    ap.add_argument("--source-only", action="store_true", help="只打源码包")
    ap.add_argument("--green-only", action="store_true", help="只打绿色版包")
    ap.add_argument("--no-validate", action="store_true", help="跳过解压验收")
    args = ap.parse_args()

    ver = read_version()
    if args.set_ver:
        ver = args.set_ver.strip()
    if args.bump:
        ver = bump(ver, args.bump)
    if args.set_ver or args.bump:
        write_version(ver)

    print(f"===== 打包版本 v{ver} =====\n")
    if not args.no_validate:
        if not check_embedded_prompt():
            print("\n[中止] 内嵌示例与 config.example.yaml 的 prompt 不一致，请先同步。")
            return 2
        print()
    made = []
    if not args.source_only:
        made.append(build_green(ver, validate=not args.no_validate))
        print()
    if not args.green_only:
        made.append(build_source(ver, validate=not args.no_validate))

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
