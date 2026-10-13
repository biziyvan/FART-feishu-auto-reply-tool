"""
运行环境适配模块
================
统一处理"源码运行"与"打包成 exe 运行"两种模式下的路径差异。

核心问题：
  PyInstaller 单文件（onefile）打包后，`__file__` / `sys._MEIPASS` 指向
  临时解压目录，程序一退出就被清空 —— 如果把它当数据目录，配置、
  登录态、日志、已答记录全都会丢失。

解决方案：
  - 代码资源（.py、config.example.yaml）→ 可能在 _MEIPASS 里（只读）
  - 数据文件（config.yaml、.browser_data、日志、history.json 等）
    → 必须放在**持久且与 exe 位置无关**的目录

数据目录优先级（frozen 模式）：
  1. 环境变量 FEISHU_DATA_DIR（用户显式指定，便于多实例/自定义位置）
  2. exe 同级存在 portable.txt → 便携模式，数据随 exe 走（绿色版）
  3. 用户数据目录 %LOCALAPPDATA%\\飞书自动回复助手（默认，推荐）
     └ 这样 exe 放桌面、放 U 盘、被覆盖升级，配置与登录态都不丢

  ★ 兼容旧版本：老版本把数据写在 exe 同级目录，首次运行时自动迁移。

源码模式：数据仍在项目根目录，行为不变。

  ★ 增量更新：逻辑脚本优先从 <DATA_DIR>\\app\\ 读取（见下文「外部逻辑目录」），
    这样修逻辑只需替换几个 .py，不必重新分发整个 exe。

对外提供：
  APP_DIR    代码/资源所在目录（源码运行=项目根目录；frozen=_MEIPASS）
  DATA_DIR   数据读写目录
  IS_FROZEN  是否运行在 PyInstaller 打包环境中
"""

import json
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "飞书自动回复助手"

# 旧版本（绿色版 onedir）把数据写在 exe 同级目录；首次运行时迁移过来。
# ⚠️ 只搬"小文件"（配置 + 已答记录），**绝不搬 .browser_data**：
#    浏览器登录态动辄几百 MB，静默复制会拖慢启动、磁盘翻倍，
#    而重新登录只需扫一次二维码，代价小得多。
_LEGACY_MIGRATE = (
    "config.yaml",
    "history.json",
    "history_raw.json",
    "answered.json",
    "reply_stats.json",
)


def _resolve_app_dir():
    """代码 / 只读资源所在目录。"""
    if getattr(sys, "frozen", False):
        # PyInstaller：资源被解压到 _MEIPASS
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _exe_dir():
    """打包后 exe 所在目录（数据目录的候选之一，也是旧版数据位置）。"""
    try:
        return Path(sys.executable).resolve().parent
    except Exception:
        return Path.cwd()


def _user_data_dir():
    """用户级数据目录：%LOCALAPPDATA%\\飞书自动回复助手。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    # 非 Windows 兜底
    return Path.home() / f".{APP_NAME}"


def _migrate_legacy(old_dir, new_dir):
    """把旧版散落在 exe 同级目录的数据搬到新目录（仅在目标缺失时拷贝）。

    仅在旧目录确实存在 config.yaml 时才动手 —— 那是"上一版留下的安装"的
    明确信号，避免误搬无关文件。
    """
    try:
        old_dir, new_dir = Path(old_dir), Path(new_dir)
        if old_dir.resolve() == new_dir.resolve():
            return
        if not (old_dir / "config.yaml").exists():
            return            # 旧目录没有配置 → 不是旧安装，不迁移
        if (new_dir / "config.yaml").exists():
            return            # 新目录已有配置 → 已迁移或在用，不打扰
        for name in _LEGACY_MIGRATE:
            src, dst = old_dir / name, new_dir / name
            if not src.exists() or dst.exists():
                continue
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            except Exception:
                pass
    except Exception:
        pass


def _resolve_data_dir():
    """
    可写数据目录。

    优先级：
      1. 环境变量 FEISHU_DATA_DIR（用户显式指定）
      2. frozen + portable.txt → exe 同级（便携/绿色模式）
      3. frozen → %LOCALAPPDATA%\\飞书自动回复助手（默认）
      4. 源码 → 项目根目录
    """
    env = os.environ.get("FEISHU_DATA_DIR")
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    if getattr(sys, "frozen", False):
        exe_dir = _exe_dir()
        # 便携模式：exe 同级放一个 portable.txt 即可让数据随 exe 走
        if (exe_dir / "portable.txt").exists():
            return exe_dir

        target = _user_data_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            # 极端情况（无权限）→ 退回 exe 同级，保证程序还能跑
            return exe_dir
        _migrate_legacy(exe_dir, target)
        return target
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
DATA_DIR = _resolve_data_dir()
IS_FROZEN = bool(getattr(sys, "frozen", False))


# ============================================================
# 外部逻辑目录（增量更新）
# ------------------------------------------------------------
# 本程序的"逻辑脚本"（step2_auto_reply.py / denoise.py / settings.py 等）
# 并不编进 exe，而是随包以 .py 形式存放。运行时按下面的顺序查找同名文件：
#
#   ① <数据目录>\app\        ← 外部逻辑（增量更新时只替换这里的几个 .py）
#   ② <exe 解压资源目录>      ← 随 exe 一起分发的内置副本
#   ③ <数据目录>
#
# ① 不存在时，行为与改造前**完全一致**（用内置副本），所以这是纯增量、
# 零回归的改动。仅打包模式启用；源码模式直接改源码即可，不引入间接层。
#
# 收益：修逻辑只需分发几百 KB 的 .py，不必重新分发 ~51MB 的 exe；
#       配置/登录态/历史记录本就在数据目录，天然不受影响。
#   注意：launcher.py / runtime.py 属于"引擎"，编在 exe 里，改它们仍需换 exe。
#
# ⚠️ 版本守护：外部逻辑若比 exe 内置版本**更旧**则自动忽略。否则用户装过的
#    旧更新包会永久遮蔽之后新 exe 里的新逻辑（"换了新 exe 却没生效"）。
# ============================================================
LOGIC_DIR = DATA_DIR / "app"

# 更新包里允许落地的文件（其余一律忽略，防止误放或夹带无关文件）
_LOGIC_ALLOW_EXACT = {"VERSION"}
_LOGIC_ALLOW_SUFFIX = (".py",)
# 明确拒绝：这些属于"用户数据"，绝不能由更新包覆盖
_LOGIC_DENY = {"config.yaml", "config.yml", "portable.txt"}

_LOGIC_MANIFEST = "_logic_manifest.json"


def logic_dir():
    """外部逻辑目录（可能不存在）。"""
    return LOGIC_DIR


def _read_version(base):
    try:
        f = Path(base) / "VERSION"
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace").strip() or None
    except Exception:
        pass
    return None


def _ver_tuple(s):
    """把 '1.0.4' 解析成 (1, 0, 4)；无法解析时返回 None。"""
    try:
        parts = str(s).strip().split(".")
        if len(parts) >= 2 and all(p.isdigit() for p in parts):
            return tuple(int(p) for p in parts)
    except Exception:
        pass
    return None


def app_version():
    """exe 内置的逻辑版本（打包时随 VERSION 打进资源目录）。"""
    return _read_version(APP_DIR) or "未知"


def _external_ignored():
    """外部逻辑目录存在、但版本比内置更旧，被主动忽略。"""
    if not (IS_FROZEN and LOGIC_DIR.is_dir()):
        return False
    ext, emb = _ver_tuple(_read_version(LOGIC_DIR)), _ver_tuple(app_version())
    return bool(ext and emb and ext < emb)


def _logic_active():
    """外部逻辑是否生效。

    条件：打包模式 + 目录存在 + **版本不旧于内置**。
    版本无法比较时按"生效"处理（用户自建/自改的场景不拦）。
    """
    if not (IS_FROZEN and LOGIC_DIR.is_dir()):
        return False
    return not _external_ignored()


def logic_version():
    """当前生效的外部逻辑版本；未启用或被忽略时返回 None。"""
    return _read_version(LOGIC_DIR) if _logic_active() else None


def logic_version_note():
    """给界面显示用的版本描述，如 '1.0.4（内置）' / '1.0.5（外部更新）'。"""
    if _external_ignored():
        return f"{app_version()}（内置，已忽略更旧的外部 {_read_version(LOGIC_DIR)}）"
    ext = logic_version()
    return f"{ext}（外部更新）" if ext else f"{app_version()}（内置）"


def logic_paths():
    """import / 脚本查找顺序（外部逻辑优先于内置副本）。"""
    paths = []
    if _logic_active():
        paths.append(str(LOGIC_DIR))
    paths.append(str(APP_DIR))
    if str(DATA_DIR) != str(APP_DIR):
        paths.append(str(DATA_DIR))
    return paths


def script_path(name):
    """按优先级定位脚本或资源：外部逻辑目录 → 内置资源 → 数据目录。"""
    for base in logic_paths():
        p = Path(base) / name
        if p.exists():
            return p
    return APP_DIR / name


def _allowed_entry(name):
    """判断更新包内的文件名是否允许落地。"""
    if name in _LOGIC_DENY:
        return False
    if name in _LOGIC_ALLOW_EXACT:
        return True
    return name.lower().endswith(_LOGIC_ALLOW_SUFFIX)


def apply_logic_package(zip_path):
    """把「逻辑更新包」(.zip) 应用到外部逻辑目录。

    规则：
      · 只接受 .py 与 VERSION（白名单外一律忽略，config.yaml 等用户数据明确拒绝）
      · 拒绝含路径穿越（..）或绝对路径的条目；所有文件平铺到逻辑目录
      · 依据上一次的清单，自动清理被本次更新包"删除"的旧文件，避免残留
        （旧文件会永久遮蔽 exe 内置的同名副本，必须清掉）

    返回 (是否成功, 说明文字)。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return False, f"找不到更新包：{zip_path}"

    # 版本守卫：比当前程序更旧的更新包没有意义（即便导入也会被忽略），
    # 直接拒绝并说明原因，避免"导入了却没变化"的困惑。
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            if "VERSION" in zf.namelist():
                pkg_ver = zf.read("VERSION").decode("utf-8", "replace").strip()
                pv, av = _ver_tuple(pkg_ver), _ver_tuple(app_version())
                if pv and av and pv < av:
                    return False, (f"该更新包是 v{pkg_ver}，比当前程序 v{app_version()} 更旧，"
                                   f"无需更新。")
    except Exception:
        pass  # 读不出来就交给下面的解压流程去报错

    try:
        LOGIC_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"无法创建逻辑目录：{e}"

    # 上一次由更新包写入的文件（用于清理）
    old_written = set()
    manifest_file = LOGIC_DIR / _LOGIC_MANIFEST
    try:
        if manifest_file.exists():
            old_written = set(json.loads(manifest_file.read_text(encoding="utf-8")).get("files", []))
    except Exception:
        old_written = set()

    written, skipped = [], []
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                raw = info.filename.replace("\\", "/")
                parts = [q for q in raw.split("/") if q not in ("", ".", "..")]
                if not parts:
                    skipped.append(raw)
                    continue
                name = parts[-1]                      # 平铺：只取文件名
                if not _allowed_entry(name):
                    skipped.append(raw)
                    continue
                try:
                    dst = LOGIC_DIR / name
                    with zf.open(info) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    written.append(name)
                except Exception as e:
                    skipped.append(f"{name}({e})")
    except Exception as e:
        return False, f"更新包无法解析（应为 .zip）：{e}"

    if not written:
        return False, "更新包里没有可用的逻辑文件（应包含 .py 文件）。"

    # 清理本次未包含、但上次写过的旧文件（避免旧逻辑永久遮蔽内置副本）
    removed = []
    for name in sorted(old_written - set(written)):
        if name in _LOGIC_DENY:
            continue
        f = LOGIC_DIR / name
        try:
            if f.exists():
                f.unlink()
                removed.append(name)
        except Exception:
            pass

    # 写清单
    try:
        manifest_file.write_text(
            json.dumps({"files": sorted(set(written))}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    ver = logic_version()
    msg = f"已应用 {len(written)} 个逻辑文件"
    if ver:
        msg += f"，逻辑版本 → {ver}"
    if removed:
        msg += f"，清理旧文件 {len(removed)} 个"
    if skipped:
        msg += f"（忽略 {len(skipped)} 项）"
    return True, msg


def resource_path(*parts):
    """获取只读资源路径（优先看数据目录，其次看打包资源目录）。"""
    for base in (DATA_DIR, APP_DIR):
        p = base.joinpath(*parts)
        if p.exists():
            return p
    return DATA_DIR.joinpath(*parts)


def data_path(*parts):
    """获取数据文件路径（始终在可写的数据目录下）。"""
    return DATA_DIR.joinpath(*parts)


def config_path():
    """
    返回应使用的配置文件路径。
    优先 config.yaml，不存在则回退 config.example.yaml。
    """
    user_cfg = data_path("config.yaml")
    if user_cfg.exists():
        return user_cfg
    example = resource_path("config.example.yaml")
    if example.exists():
        return example
    return user_cfg
