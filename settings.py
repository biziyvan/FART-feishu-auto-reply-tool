"""
配置加载模块
============
统一入口，供所有脚本使用。

优先级：环境变量 > config.yaml > config.example.yaml

首次使用：
  1. 复制 config.example.yaml 为 config.yaml
  2. 填入你的飞书地址、会话名、API Key
  （或改用环境变量，避免密钥落盘 —— 见 README）

支持的环境变量：
  FEISHU_DATA_DIR      数据目录（配置/登录态/日志存放位置）
  FEISHU_BASE_URL      飞书网页版地址
  FEISHU_TARGET_CHAT   目标会话名
  LLM_API_KEY          大模型 API Key
  LLM_BASE_URL         大模型接口地址
  LLM_MODEL            模型名

本模块同时提供「大模型接口自适应」的纯函数：
  不同厂商关闭"思考模式"的参数名不一样（硅基流动用 enable_thinking，
  DeepSeek 官方用 thinking:{type:disabled}），且 DeepSeek 默认**开启**思考、
  开启后 temperature 失效。detect_provider() / resolve_thinking() /
  apply_thinking() 负责按 base_url 自动判断该用哪套参数，
  因此同一份程序既能跑硅基流动，也能直接跑 DeepSeek 官方接口。
"""

import os
from urllib.parse import urlparse

import yaml

from runtime import DATA_DIR, config_path

BASE_DIR = DATA_DIR


# ---------------- 大模型接口自适应 ----------------

PROVIDER_LABELS = {
    "deepseek": "DeepSeek 官方",
    "siliconflow": "硅基流动",
    "openai": "OpenAI",
    "generic": "OpenAI 兼容接口",
}

# 主机名关键字 → 接口类型（按顺序匹配，越具体越靠前）
_HOST_RULES = (
    ("deepseek", ("api.deepseek.com", "deepseek.com")),
    ("siliconflow", ("siliconflow.cn", "siliconflow.com")),
    ("openai", ("api.openai.com", "openai.azure.com")),
)

# 各接口里"思考模式开关"用的参数名（None = 该接口没有这个参数，不发送）
_THINKING_KEY = {
    "deepseek": "thinking",
    "siliconflow": "enable_thinking",
}

_THINK_ON = ("enabled", "enable", "on", "true", "yes", "1")
_THINK_OFF = ("disabled", "disable", "off", "false", "no", "none", "0")


def url_host(url):
    """取出 URL 的主机名（小写）；解析不了返回空串。"""
    try:
        host = urlparse(str(url or "")).hostname
    except Exception:
        host = None
    return (host or "").lower()


def provider_from_url(base_url):
    """仅按接口地址识别提供方；识别不出返回 ""。"""
    host = url_host(base_url)
    if not host:
        return ""
    for provider, keys in _HOST_RULES:
        for k in keys:
            if host == k or host.endswith("." + k):
                return provider
    return ""


def provider_from_model(model):
    """按模型名兜底识别（用于自建网关/中转服务）；识别不出返回 ""。"""
    m = str(model or "").strip().lower()
    if not m:
        return ""
    if m.startswith("deepseek"):
        return "deepseek"
    return ""


def detect_provider(base_url, model="", explicit=""):
    """自适应判断接口类型，返回 deepseek / siliconflow / openai / generic。

    优先级：
      1. 接口地址（base_url）—— 最可靠，用户改配置也骗不过它
      2. 显式配置的 provider（仅当地址识别不出时才用，兼容自建网关）
      3. 模型名特征
      4. generic（通用的 OpenAI 兼容接口，只发最基础的参数）

    注意：地址与显式配置**矛盾**时以地址为准，并把矛盾暴露给
    provider_conflict()，由主程序记一条告警日志
    （踩过的坑：他机配置写着 provider: siliconflow，base_url 却是
      api.deepseek.com，于是发出去的 enable_thinking 被 DeepSeek 忽略、
      思考模式一直开着）。
    """
    by_url = provider_from_url(base_url)
    if by_url:
        return by_url
    exp = str(explicit or "").strip().lower()
    if exp and exp != "auto":
        if exp in PROVIDER_LABELS:
            return exp
        if exp in ("silicon", "silicon_flow"):
            return "siliconflow"
        return "generic"
    return provider_from_model(model) or "generic"


def provider_conflict(base_url, explicit):
    """显式 provider 与接口地址识别结果矛盾时返回 (显式值, 实际值)，否则 None。"""
    by_url = provider_from_url(base_url)
    exp = str(explicit or "").strip().lower()
    if not by_url or not exp or exp == "auto":
        return None
    if exp in ("silicon", "silicon_flow"):
        exp = "siliconflow"
    if exp not in PROVIDER_LABELS:
        return None
    return (exp, by_url) if exp != by_url else None


def resolve_thinking(provider, llm_cfg):
    """决定是否开启思考模式，返回 True / False / None。

    None = 不干预，交由接口默认（仅用于硅基流动等默认即关闭的平台，
    保持与本程序旧版本完全一致的行为）。

    配置优先级：llm.thinking（"enabled"/"disabled"）> llm.enable_thinking（布尔）。
    两者都没写时：
      - DeepSeek 官方：默认**关闭**。官方默认开启思考，而本程序的场景
        （只回一个字母 / 一段短代码）开思考纯属浪费——又慢又贵，还会
        让 temperature 失效。
      - 其它平台：不干预（保持旧行为）。
    """
    raw = (llm_cfg or {}).get("thinking", None)
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in _THINK_ON:
            return True
        if v in _THINK_OFF:
            return False
    if isinstance((llm_cfg or {}).get("enable_thinking"), bool):
        return llm_cfg["enable_thinking"]
    if provider == "deepseek":
        return False
    return None


def apply_thinking(payload, provider, enabled):
    """把"思考开关"按该接口的参数名写进 payload（原地修改并返回）。

    - deepseek   → {"thinking": {"type": "disabled"}}
    - siliconflow→ {"enable_thinking": false}
    - 其它/None  → 不动
    另外：DeepSeek 思考模式下 temperature / top_p 官方明确"不生效"
    （不会报错），此时直接不发送，避免误导。
    """
    if enabled is None:
        return payload
    key = _THINKING_KEY.get(provider)
    if key == "thinking":
        payload["thinking"] = {"type": "enabled" if enabled else "disabled"}
        if enabled:
            payload.pop("temperature", None)
            payload.pop("top_p", None)
    elif key == "enable_thinking":
        payload["enable_thinking"] = bool(enabled)
    return payload


def load_config():
    path = config_path()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    fs = cfg.setdefault("feishu", {})
    llm = cfg.setdefault("llm", {})

    if os.environ.get("FEISHU_BASE_URL"):
        fs["base_url"] = os.environ["FEISHU_BASE_URL"]
    if os.environ.get("FEISHU_TARGET_CHAT"):
        fs["target_chat_name"] = os.environ["FEISHU_TARGET_CHAT"]
    if os.environ.get("LLM_API_KEY"):
        llm["api_key"] = os.environ["LLM_API_KEY"]
    if os.environ.get("LLM_BASE_URL"):
        llm["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        llm["model"] = os.environ["LLM_MODEL"]

    return cfg

