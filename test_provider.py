# -*- coding: utf-8 -*-
"""大模型接口自适应回归测试（不需要网络/浏览器）

背景（2026-10-13 他机日志）：
  他机 config.yaml 写着 `provider: siliconflow`，但 `base_url` 是
  `https://api.deepseek.com`、模型是 `deepseek-flash` —— 于是程序发的
  `enable_thinking: false` 是**硅基流动**的参数名，DeepSeek 官方根本不认
  （未知字段被静默忽略），思考模式一直开着：又慢又贵，且 temperature 失效。

本模块验证「按接口地址自动切换参数」这套逻辑：
  · settings.detect_provider   —— 接口类型识别（地址 > 显式配置 > 模型名）
  · settings.provider_conflict —— 配置矛盾检测（用于启动告警）
  · settings.resolve_thinking  —— 是否开着思考模式（DeepSeek 默认关）
  · settings.apply_thinking    —— 参数名落地（enable_thinking / thinking）
  · step2.build_payload        —— 真实请求体组装

运行：
  .venv\\Scripts\\python.exe test_provider.py
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.argv = ["test_provider"]

sys.path.insert(0, ".")

import step2_auto_reply as S  # noqa: E402
from settings import (  # noqa: E402
    apply_thinking,
    detect_provider,
    provider_conflict,
    provider_from_model,
    provider_from_url,
    resolve_thinking,
    url_host,
)

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


def payload_with(llm_cfg, ctx="CTX"):
    """用给定 llm 配置组装真实请求体（不改动模块全局）。"""
    old = S.LLM
    S.LLM = dict(llm_cfg)
    try:
        p = detect_provider(
            llm_cfg.get("base_url", ""), llm_cfg.get("model", ""), llm_cfg.get("provider", "")
        )
        th = resolve_thinking(p, llm_cfg)
        return p, S.build_payload(ctx, p, th)
    finally:
        S.LLM = old


def main():
    print("\n[1] url_host / provider_from_url —— 按地址识别")
    check("api.deepseek.com", provider_from_url("https://api.deepseek.com"), "deepseek")
    check("带 /v1 路径", provider_from_url("https://api.deepseek.com/v1"), "deepseek")
    check("带尾斜杠", provider_from_url("https://api.siliconflow.cn/v1/"), "siliconflow")
    check("硅基流动 .com", provider_from_url("https://api.siliconflow.com/v1"), "siliconflow")
    check("openai 官方", provider_from_url("https://api.openai.com/v1"), "openai")
    check("自建网关识别不出", provider_from_url("https://llm.mycorp.com/v1"), "")
    check("空地址", provider_from_url(""), "")
    check("垃圾字符串不抛异常", provider_from_url("not a url"), "")
    check("非字符串不抛异常", provider_from_url(None), "")
    check("host 解析", url_host("https://API.DeepSeek.com/v1"), "api.deepseek.com")
    check("大小写不敏感", provider_from_url("https://API.DEEPSEEK.COM/v1"), "deepseek")

    print("\n[2] provider_from_model —— 模型名兜底")
    check("deepseek-flash", provider_from_model("deepseek-flash"), "deepseek")
    check("deepseek-v4-pro", provider_from_model("deepseek-v4-pro"), "deepseek")
    check("硅基流动的 DeepSeek 也认", provider_from_model("deepseek-ai/DeepSeek-V3"), "deepseek")
    check("Qwen 不猜", provider_from_model("Qwen/Qwen3.5-35B-A3B"), "")
    check("空模型名", provider_from_model(""), "")

    print("\n[3] detect_provider —— 优先级：地址 > 显式配置 > 模型名")
    check(
        "地址可识别时压过显式配置（他机错配场景）",
        detect_provider("https://api.deepseek.com", "deepseek-flash", "siliconflow"),
        "deepseek",
    )
    check(
        "地址识别不出时才用显式配置（自建网关）",
        detect_provider("https://llm.mycorp.com/v1", "gpt-4o", "siliconflow"),
        "siliconflow",
    )
    check(
        "没有显式配置 → 看模型名",
        detect_provider("https://llm.mycorp.com/v1", "deepseek-flash", ""),
        "deepseek",
    )
    check(
        "全都认不出 → generic",
        detect_provider("https://llm.mycorp.com/v1", "gpt-4o", ""),
        "generic",
    )
    check(
        "provider=auto 视同没填",
        detect_provider("https://llm.mycorp.com/v1", "deepseek-flash", "auto"),
        "deepseek",
    )
    check(
        "provider 大小写/空格容错",
        detect_provider("https://llm.mycorp.com/v1", "x", "  SiliconFlow "),
        "siliconflow",
    )
    check(
        "本机现状：硅基流动 + Qwen",
        detect_provider("https://api.siliconflow.cn/v1", "Qwen/Qwen3.5-35B-A3B", "siliconflow"),
        "siliconflow",
    )

    print("\n[4] provider_conflict —— 配置矛盾检测（启动告警用）")
    check(
        "地址 deepseek + 写 siliconflow → 报矛盾",
        provider_conflict("https://api.deepseek.com", "siliconflow"),
        ("siliconflow", "deepseek"),
    )
    check(
        "地址 siliconflow + 写 deepseek → 报矛盾",
        provider_conflict("https://api.siliconflow.cn/v1", "deepseek"),
        ("deepseek", "siliconflow"),
    )
    check("一致 → 不报", provider_conflict("https://api.deepseek.com", "deepseek"), None)
    check("auto → 不报", provider_conflict("https://api.deepseek.com", "auto"), None)
    check("没填 → 不报", provider_conflict("https://api.deepseek.com", ""), None)
    check(
        "自建网关 + 显式配置 → 不报（这是合法用法）",
        provider_conflict("https://llm.mycorp.com/v1", "siliconflow"),
        None,
    )

    print("\n[5] resolve_thinking —— 要不要开思考模式")
    check("DeepSeek 且没配 → 默认关（官方默认开，对答题是浪费）",
          resolve_thinking("deepseek", {}), False)
    check("硅基流动 且没配 → 不干预（保持旧行为）",
          resolve_thinking("siliconflow", {}), None)
    check("旧写法 enable_thinking=false 生效",
          resolve_thinking("siliconflow", {"enable_thinking": False}), False)
    check("旧写法 enable_thinking=true 生效",
          resolve_thinking("siliconflow", {"enable_thinking": True}), True)
    check("新写法 thinking=disabled 生效",
          resolve_thinking("siliconflow", {"thinking": "disabled"}), False)
    check("新写法 thinking=enabled 生效",
          resolve_thinking("deepseek", {"thinking": "enabled"}), True)
    check("thinking 优先于 enable_thinking",
          resolve_thinking("deepseek", {"thinking": "disabled", "enable_thinking": True}), False)
    check("thinking 值非法 → 回退到 enable_thinking",
          resolve_thinking("deepseek", {"thinking": "??", "enable_thinking": False}), False)
    check("generic 平台没配 → 不干预",
          resolve_thinking("generic", {}), None)

    print("\n[6] apply_thinking —— 参数名落地")
    p = {"model": "m", "temperature": 0.3}
    check("DeepSeek 关思考", apply_thinking(dict(p), "deepseek", False),
          {"model": "m", "temperature": 0.3, "thinking": {"type": "disabled"}})
    check("DeepSeek 开思考 → 剔除 temperature（官方：思考模式下无效）",
          apply_thinking(dict(p), "deepseek", True),
          {"model": "m", "thinking": {"type": "enabled"}})
    check("DeepSeek 开思考 → 一并剔除 top_p",
          apply_thinking({"temperature": 0.3, "top_p": 0.9}, "deepseek", True),
          {"thinking": {"type": "enabled"}})
    check("硅基流动关思考", apply_thinking({}, "siliconflow", False),
          {"enable_thinking": False})
    check("硅基流动开思考", apply_thinking({}, "siliconflow", True),
          {"enable_thinking": True})
    check("None = 不干预（硅基流动保持旧行为）",
          apply_thinking({"model": "m"}, "siliconflow", None), {"model": "m"})
    check("OpenAI 官方：不发任何思考参数",
          apply_thinking({"model": "m"}, "openai", False), {"model": "m"})
    check("自建网关（generic）：不发任何思考参数",
          apply_thinking({"model": "m"}, "generic", True), {"model": "m"})
    check("原地修改并返回同一个对象",
          apply_thinking(p, "siliconflow", False) is p, True)

    print("\n[7] build_payload —— 真实请求体（端到端）")
    # 他机现状：硅基流动的参数名 + DeepSeek 的地址（就是那个 bug）
    prov, pay = payload_with({
        "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "provider": "siliconflow", "enable_thinking": False,
        "system_prompt": "SYS", "max_tokens": 1000, "temperature": 0.3,
    })
    check("他机场景 → 判定为 deepseek", prov, "deepseek")
    check("他机场景 → 发的是 DeepSeek 的参数（不再发 enable_thinking）",
          ("enable_thinking" in pay, pay.get("thinking")), (False, {"type": "disabled"}))
    check("他机场景 → 关思考时仍保留 temperature",
          pay.get("temperature"), 0.3)

    # 本机现状：硅基流动 + Qwen（必须完全不受影响）
    prov, pay = payload_with({
        "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3.5-35B-A3B",
        "provider": "siliconflow", "enable_thinking": False,
        "system_prompt": "SYS", "max_tokens": 1000, "temperature": 0.3,
    })
    check("本机场景 → 判定为 siliconflow", prov, "siliconflow")
    check("本机场景 → 仍是 enable_thinking=false（与旧版逐字节一致）",
          (pay.get("enable_thinking"), "thinking" in pay), (False, False))
    check("本机场景 → temperature 保留", pay.get("temperature"), 0.3)

    # 老配置（没有 enable_thinking 键）→ 硅基流动不应凭空多出参数
    _, pay = payload_with({
        "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3.5-35B-A3B",
        "system_prompt": "SYS",
    })
    check("老配置不凭空加参数", ("enable_thinking" in pay, "thinking" in pay), (False, False))

    # 换模型即切 DeepSeek：只改 base_url + model，其它不动
    prov, pay = payload_with({
        "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
        "system_prompt": "SYS",
    })
    check("只改地址+模型就自动切 DeepSeek", (prov, pay.get("thinking")),
          ("deepseek", {"type": "disabled"}))

    # 用户显式要开思考
    prov, pay = payload_with({
        "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "system_prompt": "SYS", "thinking": "enabled", "temperature": 0.3,
    })
    check("显式开思考 → thinking enabled 且不带 temperature",
          (pay.get("thinking"), "temperature" in pay), ({"type": "enabled"}, False))

    print("\n[8] 请求体基本结构未被破坏")
    _, pay = payload_with({
        "base_url": "https://api.siliconflow.cn/v1", "model": "M",
        "system_prompt": "SYS\n", "max_tokens": 500, "temperature": 0.2,
    })
    check("model", pay.get("model"), "M")
    check("messages 为 system+user 两条",
          [(m["role"], m["content"]) for m in pay["messages"]],
          [("system", "SYS"), ("user", "CTX")])
    check("max_tokens", pay.get("max_tokens"), 500)
    check("temperature", pay.get("temperature"), 0.2)

    print("\n[9] 与主程序状态一致性")
    check("step2 模块级 PROVIDER 是已知类型", S.PROVIDER in
          ("deepseek", "siliconflow", "openai", "generic"), True)
    check("ENABLE: provider_desc() 可用", isinstance(S.provider_desc(), str), True)

    print(f"\n{'=' * 62}")
    print(f"全部完成：通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
