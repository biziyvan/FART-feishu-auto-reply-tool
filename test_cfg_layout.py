# -*- coding: utf-8 -*-
"""配置页布局 / 换肤自检（需要带 tkinter 的解释器，会短暂弹出窗口）

覆盖 v1.1.1 修掉的两个问题：

  1. **内容被硬裁**（用户报的"排版错误"）
     「配置与检查」页内容 ≈ 490px，而页签区在 1000x720 下只有 ~316px。
     以前直接把内容塞进 Panel.body，会被 Panel._layout() 按可视高度钳死
     → 底部几行被裁掉（修复前实测：需要 488px / 可视只有 272px，溢出 172px，
       「浏览器登录态」只露半截）。
     现在包了一层可滚动视口，内容必须**完整可达**。

  2. **换肤撞车**
     浅色下 surface 与 accent_ink 同为 #FFFFFF，而整树换肤是**按颜色值**
     建映射的 → 谁后写谁生效。accent_ink 后写，导致所有 `bg=surface` 的
     容器在深色下被染成 accent_ink 的 #0B0C10（比卡片 #131519 更黑）。
     新增的滚动画布整块就是这个色 → 一眼可见的脏色块。

运行：
  .buildenv313\\Scripts\\python.exe test_cfg_layout.py

⚠️ 两个环境注意点（都跟被测代码无关）：
  1. 只有 .buildenv313 这个环境带 tkinter（项目 .venv / 受管 3.13.x 都没装）。
  2. 在 **WorkBuddy/CodeBuddy 的终端里**跑时，CLI 会往 PYTHONPATH 里注入
     `...\\cli\\vendor\\shim\\sitecustomize.py`；该 shim 会让"建过 Tk 窗口的进程"
     结束后无法退出（实测 os._exit / TerminateProcess 都拦不住，25s+ 挂住）。
     普通终端没这个问题；在 Agent 终端里请这样跑：
        PYTHONPATH= .buildenv313/Scripts/python.exe test_cfg_layout.py
"""
import os
import sys

sys.argv = ["launcher.py"]

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}\n      期望 {want!r}\n      实际 {got!r}")


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


try:
    import tkinter  # noqa: F401
except Exception as e:
    print(f"[跳过] 当前解释器没有 tkinter：{e}")
    print("       请用 .buildenv313\\Scripts\\python.exe 运行")
    sys.exit(0)

import launcher as L

print("=" * 60)
print("  配置页布局 / 换肤自检")
print("=" * 60)

win = L.MainWindow()


def pump(n=8):
    for _ in range(n):
        win.update_idletasks()
        win.update()


try:
    pump()
    win._switch_tab(2)
    pump()

    print("\n[1] 内容必须完整可达（不再被 Panel 硬裁）")
    cv, holder = win._cfg_cv, win._cfg_holder
    need = holder.winfo_reqheight()
    region = str(cv.cget("scrollregion")).split()
    reached = int(float(region[3])) if len(region) >= 4 else 0
    print(f"      窗口 {win.winfo_width()}x{win.winfo_height()}"
          f"  视口 {cv.winfo_height()}  内容 {need}")
    ok("滚动区高度覆盖全部内容", reached >= need, f"scrollregion={region}")
    ok("内容高于视口时滚动条可见",
       bool(win._cfg_sb_shown) == (need > cv.winfo_height()),
       f"showed={win._cfg_sb_shown}")

    # 页面最后一行的实际位置必须在滚动区内
    last = win.health["logic"]
    ok("最后一行（逻辑版本）在滚动区内",
       last.winfo_y() + last.winfo_height() <= reached,
       f"y={last.winfo_y()} h={last.winfo_height()} 区高={reached}")

    print("\n[2] 窗口拉高后滚动条自动隐藏")
    win.geometry("%dx%d" % (win.winfo_width(), need + 420))
    pump()
    ok("足够高时隐藏", win._cfg_sb_shown is False,
       f"视口={cv.winfo_height()} 内容={need}")
    win.geometry("1000x720")
    pump()
    ok("缩回后重新出现", win._cfg_sb_shown is True,
       f"视口={cv.winfo_height()}")

    print("\n[3] 深色模式配色（防换肤按色值撞车）")
    L.T.set_mode("dark")
    pump(12)
    ok("滚动画布 = 卡片底色", cv.cget("bg").lower() == L.T.surface.lower(),
       f"{cv.cget('bg')} 应为 {L.T.surface}")
    ok("内容容器 = 卡片底色",
       holder.winfo_children()[0].cget("bg").lower() == L.T.surface.lower(),
       holder.winfo_children()[0].cget("bg"))
    ok("面板体 = 卡片底色",
       win.tab_cfg.body.cget("bg").lower() == L.T.surface.lower(),
       win.tab_cfg.body.cget("bg"))

    print("\n[4] 切回浅色")
    L.T.set_mode("light")
    pump(12)
    ok("画布恢复卡片色", cv.cget("bg").lower() == L.T.surface.lower(),
       cv.cget("bg"))
    ok("内容容器恢复卡片色",
       holder.winfo_children()[0].cget("bg").lower() == L.T.surface.lower(),
       holder.winfo_children()[0].cget("bg"))

    print("\n[5] 三个页签都能正常切换（结构未被改坏）")
    for i, name in enumerate(("运行日志", "答题流水", "配置与检查")):
        win._switch_tab(i)
        pump()
        t = win._tabs[i]
        ok(f"页签 {i} {name} 有尺寸", t.winfo_width() > 100 and t.winfo_height() > 50,
           f"{t.winfo_width()}x{t.winfo_height()}")

    print("\n[6] 数据目录行 + 配置搬运按钮（换电脑免重填的入口）")
    win._switch_tab(2)
    pump()
    import runtime as R
    txt = str(win.lbl_data_dir.cget("text"))
    ok("数据目录行显示的是真实路径", str(R.DATA_DIR) in txt, txt)
    ok("并标明了来源（对不上号时好自证）",
       R.data_dir_source() in txt, txt)
    ry = win.lbl_data_dir.winfo_rooty() - holder.winfo_rooty()
    ok("数据目录行在滚动区内（不会被裁掉）",
       ry + win.lbl_data_dir.winfo_height() <= reached,
       f"y={ry} h={win.lbl_data_dir.winfo_height()} 区高={reached}")
    ok("「更改数据目录」按钮已创建",
       win.btn_chdir.winfo_reqwidth() > 40 and win.btn_chdir.winfo_reqheight() > 10,
       f"{win.btn_chdir.winfo_reqwidth()}x{win.btn_chdir.winfo_reqheight()}")
    ok("导出 / 导入配置方法可用",
       callable(getattr(win, "export_config", None))
       and callable(getattr(win, "import_config", None))
       and callable(getattr(win, "change_data_dir", None)))
    # ★ 这条是关键：按钮曾经被放在页面最底部 → 折叠线以下 → 用户看不到。
    act = win.btn_chdir.master
    act_bottom = act.winfo_rooty() - holder.winfo_rooty() + act.winfo_height()
    ok("主要按钮在首屏可见（不用滚动就能点到）",
       act_bottom <= cv.winfo_height() + 1,
       f"按钮底 {act_bottom} > 视口 {cv.winfo_height()}")

    print("\n[7] 本次运行正确率指标（v1.4.0）")
    win._switch_tab(0)
    pump()
    ok("指标区注册了 rate", "rate" in win._metrics, f"keys={list(win._metrics)}")

    win._m_judged, win._m_correct = 0, 0
    win._refresh_metrics()
    pump()
    ok("还没收到判定时显示 —（不拿 0% 冒充全错）",
       win._metrics["rate"].cget("text") == "—",
       win._metrics["rate"].cget("text"))

    # 走一遍真实的日志链路：step2 打的那行 → _ingest → 指标
    win._ingest("2026-10-14 21:50:00 [INFO] [统计] 本次运行: 已答 12 题 | "
                "已判定 9 题（对 7 / 错 2）| 正确率 77%")
    win._refresh_metrics()
    pump()
    check("日志解析出 对/已判定/已答",
          (win._m_correct, win._m_judged, win._m_run_answered), (7, 9, 12))
    check("正确率显示 77%", win._metrics["rate"].cget("text"), "77%")

    # 重新启动必须清零，否则新一次运行会顶着上一次的百分比
    win._ingest("2026-10-14 21:51:00 [INFO] 自动回复程序 启动")
    win._refresh_metrics()
    pump()
    check("重新启动后归零",
          (win._m_correct, win._m_judged, win._metrics["rate"].cget("text")),
          (0, 0, "—"))

    # 加到第 5 个指标后，横排要放得开、且不压到左侧状态标题
    rects = sorted((w.master.winfo_rootx(),
                    w.master.winfo_rootx() + w.master.winfo_width())
                   for w in win._metrics.values())
    dup = [(a, b) for a, b in zip(rects, rects[1:]) if b[0] < a[1]]
    ok("5 个指标横向不重叠", not dup, f"重叠 {dup}")
    title_right = win.lbl_st_main.winfo_rootx() + win.lbl_st_main.winfo_width()
    ok("指标区没压到左侧状态标题", title_right <= rects[0][0],
       f"标题右 {title_right} vs 最左指标 {rects[0][0]}")
except Exception as e:
    import traceback
    traceback.print_exc()
    ok("自检可执行（控件结构未被改坏）", False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 60)
print(f"  通过 {PASS} / 失败 {FAIL}")
print("=" * 60)

# 建过 Tk 窗口后解释器收尾会卡住 → 强制退出（只影响测试脚本自身）
sys.stdout.flush()
os._exit(1 if FAIL else 0)
