"""多模型 A/B 自检：同一批选择题，对比「只回字母(格式)」+「答案正确」+ 耗时 + 成本。

只调大模型，不碰浏览器。复用 test_choice.py 的 8 道题与卡片格式。

运行：.venv\\Scripts\\python.exe test_models_ab.py
"""

import io
import re
import sys
import time

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import step2_auto_reply as S  # noqa: E402
from test_choice import CASES, build_cards  # noqa: E402

# 官方定价页 2026-10：[输入, 输出] 元 / 百万 token
PRICES = {
    "Qwen/Qwen3.5-35B-A3B": (0.40, 3.20),
    "zai-org/GLM-5.3-Flash": (0.80, 2.80),
    "stepfun-ai/Step-3.5-Flash": (0.70, 2.10),
    "inclusionAI/Ling-mini-2.0": (0.50, 2.00),
    "XingChenAGI/Xing4.0-29B": (0.0, 0.0),
}

MODELS = list(PRICES.keys())


def call(model, context_str, think_off):
    """按 step2_auto_reply.ask_llm 的载荷结构发一次请求，额外返回 usage。"""
    key = S.LLM["api_key"].strip()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": S.LLM["system_prompt"].strip()},
            {"role": "user", "content": context_str},
        ],
        "max_tokens": S.LLM.get("max_tokens", 500),
        "temperature": S.LLM.get("temperature", 0.3),
    }
    if think_off:
        payload["enable_thinking"] = False
    url = S.LLM["base_url"].rstrip("/") + "/chat/completions"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=S.LLM.get("timeout", 45),
    )
    r.raise_for_status()
    j = r.json()
    return j["choices"][0]["message"]["content"].strip(), j.get("usage", {})


def run_model(model):
    """跑完 8 题，返回统计；自动探测是否支持 enable_thinking。"""
    think_off = True
    probe_note = ""
    ok_fmt = ok_ans = 0
    tin = tout = 0
    t_all = 0.0
    fails = []
    for i, (stem, opts, key) in enumerate(CASES, 1):
        ctx = build_cards(stem, opts)
        t = time.time()
        try:
            ans, usage = call(model, ctx, think_off)
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", 0)
            if think_off and 400 <= code < 500 and code != 429:
                # 该模型不认 enable_thinking → 去掉重试一次
                think_off = False
                probe_note = "（不支持 enable_thinking，已自动去掉）"
                ans, usage = call(model, ctx, think_off)
            else:
                raise
        dt = time.time() - t
        t_all += dt
        ans = (ans or "").strip()
        stripped = re.sub(r"[^A-Za-z]", "", ans).upper()
        fmt_ok = stripped == key or (len(ans) == 1 and ans.upper() in "ABCD")
        ans_ok = stripped == key
        ok_fmt += fmt_ok
        ok_ans += ans_ok
        tin += usage.get("prompt_tokens", 0)
        tout += usage.get("completion_tokens", 0)
        if not ans_ok:
            fails.append(f"      第{i}题 期望{key} → {ans[:60]!r}")
    pin, pout = PRICES[model]
    cost = tin / 1e6 * pin + tout / 1e6 * pout
    return {
        "model": model,
        "ok_fmt": ok_fmt,
        "ok_ans": ok_ans,
        "avg_s": t_all / len(CASES),
        "tin": tin,
        "tout": tout,
        "cost": cost,
        "note": probe_note,
        "fails": fails,
    }


def main():
    cfg_model = S.LLM["model"]
    print("=" * 78)
    print(f"多模型 A/B 自检   配置当前模型: {cfg_model}   共 {len(CASES)} 题")
    print("=" * 78)
    rows = []
    for m in MODELS:
        tag = "  ← 当前" if m == cfg_model else ""
        print(f"\n▶ 测试 {m}{tag}")
        try:
            r = run_model(m)
        except Exception as e:
            print(f"    ✗ 调用失败: {str(e)[:160]}")
            rows.append({"model": m, "err": str(e)[:60]})
            continue
        rows.append(r)
        print(f"    只回字母 {r['ok_fmt']}/8   答案正确 {r['ok_ans']}/8   "
              f"均耗时 {r['avg_s']:.1f}s   token {r['tin']}+{r['tout']}   "
              f"成本 ¥{r['cost']:.4f} {r['note']}")
        for f in r["fails"]:
            print(f)

    print("\n" + "=" * 78)
    print(f"{'模型':<30}{'格式':>6}{'正确':>6}{'均耗时':>8}{'8题成本':>11}")
    print("-" * 78)
    for r in rows:
        if "err" in r:
            print(f"{r['model']:<30}{'调用失败: ' + r['err']}")
            continue
        print(f"{r['model']:<30}{r['ok_fmt']:>4}/8{r['ok_ans']:>5}/8"
              f"{r['avg_s']:>7.1f}s{'¥' + format(r['cost'], '.4f'):>11}")
    print("=" * 78)


if __name__ == "__main__":
    main()
