"""代码题 A/B：同一批代码题，对比各模型输出的「围栏/注释/精简度/耗时/成本」。

自包含（不 import 其它测试脚本，避免多层包装 sys.stdout）。
运行：.venv\\Scripts\\python.exe test_code_ab.py
"""

import re
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import step2_auto_reply as S  # noqa: E402

PRICES = {
    "Qwen/Qwen3.5-35B-A3B": (0.40, 3.20),
    "zai-org/GLM-5.3-Flash": (0.80, 2.80),
    "stepfun-ai/Step-3.5-Flash": (0.70, 2.10),
    "inclusionAI/Ling-mini-2.0": (0.50, 2.00),
    "XingChenAGI/Xing4.0-29B": (0.0, 0.0),
}
MODELS = list(PRICES)

CASES = [
    "编写一段与「智能合约基本概念」相关的核心代码，实现其基本逻辑，并说明关键行的作用。",
    "用 Solidity 编写一个函数，实现向指定地址转账并在成功后记录事件，说明关键行的作用。",
    "编写一段 Solidity 代码，实现一个简单的计数器：可以自增、可以查询当前值，说明关键行作用。",
    "编写一段代码，读取一个整数列表并返回其中的偶数，说明关键行作用。",
]


def build(question, course="智能合约技术"):
    return (
        f"【历史对话】\n[机器人] AI 自适应问答\n📚 正在学习：{course}\n"
        f"第 1 题（掌握度 0%）\n{question}\n💡 直接回复你的答案\n\n"
        f"★当前待答题目★（机器人最新一条消息，需要回复它）：\n{question}\n"
    )


def inspect(ans):
    issues = []
    if "```" in ans:
        issues.append("含 ``` 围栏")
    body = re.sub(r"```[a-zA-Z]*", "", ans)
    if re.search(r"(^|\n)\s*(//|#\s|/\*|\*)", body):
        issues.append("疑似有注释行")
    if re.search(r"\b(try:|except |catch \(|throws )", body):
        issues.append("有异常处理")
    if re.search(r"\b(if\s+.*==\s*None|if\s+not\s+isinstance|len\(.*\)\s*==\s*0)", body):
        issues.append("有参数校验")
    if re.search(r"\bdef main|if __name__|public static void main", body):
        issues.append("有 main 示例")
    return issues


def call(model, ctx, think_off):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": S.LLM["system_prompt"].strip()},
            {"role": "user", "content": ctx},
        ],
        "max_tokens": S.LLM.get("max_tokens", 500),
        "temperature": S.LLM.get("temperature", 0.3),
    }
    if think_off:
        payload["enable_thinking"] = False
    url = S.LLM["base_url"].rstrip("/") + "/chat/completions"
    r = requests.post(url, headers={"Authorization": f"Bearer {S.LLM['api_key'].strip()}",
                                    "Content-Type": "application/json"},
                      json=payload, timeout=S.LLM.get("timeout", 45))
    r.raise_for_status()
    j = r.json()
    return j["choices"][0]["message"]["content"].strip(), j.get("usage", {})


def safe_call(model, ctx):
    try:
        return call(model, ctx, True)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", 0)
        if 400 <= code < 500 and code != 429:
            return call(model, ctx, False)
        raise


def run_model(model):
    n = len(CASES)
    fence = comment = 0
    lines = chars = tin = tout = 0
    t_all = 0.0
    first = None
    for q in CASES:
        t = time.time()
        ans, usage = safe_call(model, build(q))
        t_all += time.time() - t
        ans = ans or ""
        iss = inspect(ans)
        if any("围栏" in x for x in iss):
            fence += 1
        if any("注释" in x for x in iss):
            comment += 1
        lines += len([x for x in ans.splitlines() if x.strip()])
        chars += len(ans)
        tin += usage.get("prompt_tokens", 0)
        tout += usage.get("completion_tokens", 0)
        if first is None:
            first = (ans, iss)
    pin, pout = PRICES[model]
    return dict(model=model, n=n, fence=fence, comment=comment, avg_lines=lines / n,
                avg_chars=chars / n, avg_s=t_all / n,
                cost=tin / 1e6 * pin + tout / 1e6 * pout, first=first)


def main():
    print("=" * 80)
    print(f"代码题 A/B 自检（{len(CASES)} 道，配置当前模型: {S.LLM['model']}）")
    print("=" * 80)
    rows = []
    for m in MODELS:
        tag = "  ← 当前" if m == S.LLM["model"] else ""
        print(f"\n▶ 测试 {m}{tag}")
        try:
            r = run_model(m)
        except Exception as e:
            print(f"    ✗ 调用失败: {str(e)[:160]}")
            rows.append({"model": m, "err": str(e)[:60]})
            continue
        rows.append(r)
        print(f"    含围栏 {r['fence']}/{r['n']}   含注释 {r['comment']}/{r['n']}   "
              f"均非空行 {r['avg_lines']:.1f}   均字符 {r['avg_chars']:.0f}   "
              f"均耗时 {r['avg_s']:.1f}s   成本 ¥{r['cost']:.4f}")

    print("\n" + "=" * 80)
    print(f"{'模型':<30}{'围栏':>6}{'注释':>6}{'均行':>7}{'均字符':>8}{'均耗时':>8}{'成本':>10}")
    print("-" * 80)
    for r in rows:
        if "err" in r:
            print(f"{r['model']:<30} 调用失败: {r['err']}")
            continue
        print(f"{r['model']:<30}{r['fence']:>4}/{r['n']}{r['comment']:>4}/{r['n']}"
              f"{r['avg_lines']:>7.1f}{r['avg_chars']:>8.0f}{r['avg_s']:>7.1f}s"
              f"{'¥' + format(r['cost'], '.4f'):>10}")
    print("=" * 80)

    for want in ("Qwen/Qwen3.5-35B-A3B", "XingChenAGI/Xing4.0-29B", "zai-org/GLM-5.3-Flash"):
        r = next((x for x in rows if x.get("model") == want and "first" in x), None)
        if r:
            print(f"\n########## {want} — 第 1 题实际输出 ##########")
            print(r["first"][0])
            print(f"---- 检查: {'、'.join(r['first'][1]) if r['first'][1] else '✓ 干净'}")


if __name__ == "__main__":
    main()
