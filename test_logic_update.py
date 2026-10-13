# -*- coding: utf-8 -*-
"""
逻辑增量更新 —— 自检脚本
=========================
验证 runtime.py 的"外部逻辑目录"机制：

  1. 未启用外部逻辑时，行为与改造前完全一致（零回归）
  2. 外部逻辑目录存在时，脚本查找 / import 顺序都优先用外部版本
  3. 逻辑更新包能安全落地（白名单、扁平化、拒绝路径穿越与用户数据）
  4. 旧清单文件会被自动清理（避免旧文件永久遮蔽内置副本）

运行：python test_logic_update.py
"""
import shutil
import sys
import tempfile
import zipfile
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
        print(f"  ✗ {name}  {extra}")


def make_zip(path, entries):
    """entries: {归档内路径: 内容}"""
    with zipfile.ZipFile(path, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return path


def main():
    real_frozen = runtime.IS_FROZEN
    real_logic = runtime.LOGIC_DIR

    # ---------------------------------------------------------------
    print("\n[1] 未启用外部逻辑 → 与改造前一致")
    runtime.IS_FROZEN = False
    p = runtime.script_path("step2_auto_reply.py")
    check("script_path 指向内置/项目目录", p == runtime.APP_DIR / "step2_auto_reply.py", str(p))
    check("logic_paths 不含外部目录",
          all("app" not in x.replace("\\", "/").split("/")[-1:] for x in runtime.logic_paths()),
          str(runtime.logic_paths()))
    check("logic_version 为 None（未启用）", runtime.logic_version() is None)
    check("logic_version_note 标注「内置」", "内置" in runtime.logic_version_note(),
          runtime.logic_version_note())

    # ---------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        logic = td / "app"
        runtime.IS_FROZEN = True
        runtime.LOGIC_DIR = logic

        print("\n[2] 外部逻辑目录不存在 → 仍用内置（不能因为开启开关就找不到脚本）")
        check("_logic_active 为假（目录不存在）", runtime._logic_active() is False)
        check("script_path 仍在内置目录",
              runtime.script_path("step2_auto_reply.py") == runtime.APP_DIR / "step2_auto_reply.py")

        print("\n[3] 建立外部逻辑目录 → 优先外部")
        logic.mkdir(parents=True)
        (logic / "step2_auto_reply.py").write_text("# external v2\n", encoding="utf-8")
        check("_logic_active 为真", runtime._logic_active() is True)
        check("命中外部副本",
              runtime.script_path("step2_auto_reply.py") == logic / "step2_auto_reply.py")
        check("外部缺该文件时回退内置",
              runtime.script_path("denoise.py") == runtime.APP_DIR / "denoise.py")
        lp = runtime.logic_paths()
        check("import 顺序：外部目录排第一", lp[0] == str(logic), str(lp))

        print("\n[4] 逻辑更新包：正常落地")
        zp = make_zip(td / "u1.zip", {
            "denoise.py": "# new denoise\n",
            "step2_auto_reply.py": "# new step2\n",
            "VERSION": "9.9.9\n",
        })
        ok, msg = runtime.apply_logic_package(zp)
        check("返回成功", ok, msg)
        check("denoise.py 已落地", (logic / "denoise.py").exists())
        check("VERSION 生效 → logic_version=9.9.9", runtime.logic_version() == "9.9.9",
              str(runtime.logic_version()))
        check("logic_version_note 标注「外部更新」", "外部更新" in runtime.logic_version_note(),
              runtime.logic_version_note())
        check("清单已写出", (logic / runtime._LOGIC_MANIFEST).exists())

        print("\n[5] 安全防护：拒绝非法条目")
        logic2 = td / "app2"
        runtime.LOGIC_DIR = logic2
        zp2 = make_zip(td / "u2.zip", {
            "../../evil.py": "bad",                 # 路径穿越
            "sub/dir/nested.py": "# nested ok\n",   # 嵌套 → 平铺
            "config.yaml": "hijack",                # 用户数据，必须拒绝
            "portable.txt": "x",                    # 便携开关，必须拒绝
            "readme.md": "# not allowed\n",         # 非白名单后缀
            "ok.py": "# ok\n",
        })
        ok2, msg2 = runtime.apply_logic_package(zp2)
        check("整体成功（有合法项）", ok2, msg2)
        check("ok.py 落地", (logic2 / "ok.py").exists())
        check("嵌套文件被平铺到根", (logic2 / "nested.py").exists())
        check("路径穿越被拒绝", not (td / "evil.py").exists())
        check("config.yaml 未被写入逻辑目录", not (logic2 / "config.yaml").exists())
        check("portable.txt 未被写入", not (logic2 / "portable.txt").exists())
        check("非白名单后缀被拒绝", not (logic2 / "readme.md").exists())

        print("\n[6] 清单清理：被移除的旧文件不再残留")
        runtime.LOGIC_DIR = logic
        # 先装 A+B，再装只含 B 的新包
        runtime.apply_logic_package(make_zip(td / "u3a.zip", {
            "aaa.py": "a\n", "bbb.py": "b\n", "VERSION": "9.9.9\n"}))
        check("aaa.py 已存在", (logic / "aaa.py").exists())
        runtime.apply_logic_package(make_zip(td / "u3b.zip", {
            "bbb.py": "b2\n", "VERSION": "9.9.10\n"}))
        check("新包未含的 aaa.py 被清理", not (logic / "aaa.py").exists())
        check("仍存在的 bbb.py 保留", (logic / "bbb.py").exists())
        check("版本推进到 9.9.10", runtime.logic_version() == "9.9.10",
              str(runtime.logic_version()))

        print("\n[7] 异常输入")
        ok3, msg3 = runtime.apply_logic_package(td / "not_exist.zip")
        check("不存在的包 → 失败", ok3 is False)
        badzip = td / "bad.zip"
        badzip.write_bytes(b"this is not a zip")
        ok4, msg4 = runtime.apply_logic_package(badzip)
        check("非法 zip → 失败且不抛异常", ok4 is False)
        emptyzip = make_zip(td / "empty.zip", {"readme.md": "x"})
        ok5, msg5 = runtime.apply_logic_package(emptyzip)
        check("无可落地文件 → 失败", ok5 is False)

        print("\n[9] 版本守护：更旧的外部逻辑必须被忽略")
        runtime.IS_FROZEN = True
        logic3 = td / "app3"
        runtime.LOGIC_DIR = logic3
        logic3.mkdir(parents=True)
        (logic3 / "denoise.py").write_text("# stale\n", encoding="utf-8")
        (logic3 / "VERSION").write_text("0.0.1\n", encoding="utf-8")
        emb = runtime.app_version()
        check(f"内置版本可读（{emb}）", runtime._ver_tuple(emb) is not None, emb)
        check("更旧 → _logic_active 为假", runtime._logic_active() is False)
        check("更旧 → logic_version 为 None", runtime.logic_version() is None)
        check("更旧 → script_path 回内置",
              runtime.script_path("denoise.py") == runtime.APP_DIR / "denoise.py")
        check("note 明确提示「已忽略」", "已忽略" in runtime.logic_version_note(),
              runtime.logic_version_note())
        (logic3 / "VERSION").write_text("99.0.0\n", encoding="utf-8")
        check("版本变新 → 恢复生效", runtime._logic_active() is True)
        old_pkg = make_zip(td / "old.zip", {"denoise.py": "x\n", "VERSION": "0.0.1\n"})
        ok6, msg6 = runtime.apply_logic_package(old_pkg)
        check("拒绝更旧的更新包", ok6 is False and "更旧" in msg6, msg6)
        same_pkg = make_zip(td / "same.zip", {"denoise.py": "y\n", "VERSION": emb + "\n"})
        ok7, msg7 = runtime.apply_logic_package(same_pkg)
        check("同版本更新包可导入（幂等）", ok7 is True, msg7)

        print("\n[8] 源码模式不受影响（回归）")
        runtime.IS_FROZEN = False
        check("_logic_active 为假", runtime._logic_active() is False)
        check("script_path 回内置", runtime.script_path("denoise.py") == runtime.APP_DIR / "denoise.py")

    # 还原
    runtime.IS_FROZEN = real_frozen
    runtime.LOGIC_DIR = real_logic

    print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
