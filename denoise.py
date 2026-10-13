"""
消息去噪预处理模块
==================
作用：
  1. 清洗飞书消息文本（去掉卡片标记、UI 噪音、多余空白）
  2. 过滤无用消息（学习提醒、系统提示等）
  3. 压缩上下文（只保留对答题有用的信息），节省 token

对外接口：
  - clean_text(text)        -> 清洗单条消息的文本
  - classify(text)          -> 判断消息类型
  - is_noise(text)          -> 是否为可忽略的噪音
  - denoise(messages)       -> 过滤噪音消息
  - build_context(messages) -> 构造精简的上下文（省 token）
"""

import re

# ---------- 消息类型 ----------
TYPE_QUESTION = "question"      # 正式题目（AI 自适应问答 / 当前题目）
TYPE_FEEDBACK = "feedback"      # 学习反馈（答对/答错+解析）
TYPE_REMINDER = "reminder"      # 学习提醒（催答题）
TYPE_COURSE = "course"          # 课程名提示（"请输入课程名称，如「智能合约」"）
TYPE_SWITCH = "switch"          # 课程切换
TYPE_MINE = "mine"              # 我发的
TYPE_OTHER = "other"            # 其他


# 需要在清洗时去掉的噪音模式
_NOISE_PATTERNS = [
    r"开启读屏标签.*",              # 无障碍提示
    r"读屏标签已关闭.*",
    r"开启通知功能.*",
    r"下载飞书客户端.*",
    r"今天也辛苦啦.*",
    r"Shift \+ Enter 换行.*",
    r"发送给.*",
    r"搜索\s*\(?Ctrl\+K\)?",
]


def clean_text(text: str) -> str:
    """清洗单条消息文本：去 UI 噪音、规整空白。"""
    if not text:
        return ""
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # 去掉纯符号行
        if set(line) <= set("​ \t·—-"):
            continue
        skip = False
        for pat in _NOISE_PATTERNS:
            if re.search(pat, line):
                skip = True
                break
        if skip:
            continue
        lines.append(line)
    # 合并，去掉零宽空格
    out = "\n".join(lines).replace("\u200b", "").strip()
    return out


# ---------- 「请输入课程名」提示 ----------
# 机器人有时（如每日提醒、课程结束）只发一句"开始学习/输入课程名称"的话，
# 里面**没有题目**，只要求你回复课程名。程序识别到就自动回复课程名开课。
# 注意：必须与「学习反馈」尾部那句"⚠️ 需切换课程，直接发新的课程名（如「密码学基础」）"
# 区分开 —— 后者是**附带在反馈卡里**的，不能回复课程名。
_COURSE_HINT_RE = re.compile(
    r"(直接回复课程名|回复课程名|请输入课程名称|输入课程名称|回复课程名称|"
    r"请输入你要?学习的课程|回复你想学习的课程|你想学习的课程|"
    r"如要开始学习|开始学习[，,]?\s*请?输入|开始练习)"
)
# 含以下字样的一律**不**当作课程名提示（题目/反馈卡里也可能出现"课程名"字样）
_COURSE_EXCLUDE_RE = re.compile(
    r"(学习反馈|掌握度|第\s*\d+\s*题|答对了|答错了|当前题目|选项作答)"
)


def is_course_prompt(text: str) -> bool:
    """是否是"请你输入课程名"的提示（无题目）。

    例如：
      「如要开始学习，请输入课程名称，如「智能合约」」
      「学习提醒 … 直接回复课程名（如「智能合约」）开始练习。」
    """
    t = clean_text(text) if text else ""
    if not t:
        return False
    if _COURSE_EXCLUDE_RE.search(t):
        return False
    return bool(_COURSE_HINT_RE.search(t))


def classify(text: str) -> str:
    """判断消息类型。"""
    if not text:
        return TYPE_OTHER
    if "AI 自适应问答" in text or "AI自适应问答" in text:
        return TYPE_QUESTION
    if "学习反馈" in text:
        return TYPE_FEEDBACK
    if "学习提醒" in text or "没有答题啦" in text:
        return TYPE_REMINDER
    # 第③种题目推送形态：形如「请直接回复选项作答 / 当前题目：…」，常不带题号
    if "当前题目" in text or "请直接回复选项作答" in text:
        return TYPE_QUESTION
    if is_course_prompt(text):
        return TYPE_COURSE
    if "课程切换" in text or "开始学习：" in text:
        return TYPE_SWITCH
    return TYPE_OTHER


def is_noise(text: str) -> bool:
    """
    是否为可忽略的噪音消息。
    注意：学习提醒虽是噪音，但其中可能隐含"当前课程"信息，
    所以这里不直接丢弃，交给 build_context 按需处理。
    """
    if not text:
        return True
    # 纯 UI 噪音
    if len(text) < 2:
        return True
    t = clean_text(text)
    if not t:
        return True
    # 催答题提醒视为噪音（对判断"该回什么"无帮助，只会误导）
    if classify(t) == TYPE_REMINDER:
        return True
    return False


def denoise(messages, drop_reminders=True, dedup=True):
    """
    过滤噪音消息。
    messages: [{"id","is_from_other","text"}]
    drop_reminders: 是否丢弃"学习提醒"与"课程名提示"（默认丢弃：这两类对
                    判断"该答什么"没有帮助，程序会另行处理课程名提示）
    dedup: 是否对"连续重复"的相同内容去重（机器人会重复推送同一题，必须去重）
    """
    out = []
    seen_recent = []  # 最近出现过的文本（用于去重）
    for m in messages:
        t = clean_text(m.get("text", ""))
        if not t:
            continue
        if drop_reminders and classify(t) in (TYPE_REMINDER, TYPE_COURSE):
            continue

        if dedup:
            # 规范化后比较（去空白差异）
            key = re.sub(r"\s+", "", t)
            if key in seen_recent:
                continue
            seen_recent.append(key)

        out.append({**m, "text": t, "type": classify(t)})
    return out


def dedup_keep_last(messages):
    """
    全局去重：相同内容只保留【最后一次】出现的位置。
    用于压缩历史上机器人重复推送的同一道题。
    返回时间正序的列表。
    """
    last_pos = {}
    normalized = []
    for i, m in enumerate(messages):
        key = re.sub(r"\s+", "", clean_text(m.get("text", "")))
        normalized.append(key)
        if key:
            last_pos[key] = i
    out = []
    for i, m in enumerate(messages):
        if normalized[i] and last_pos.get(normalized[i]) != i:
            continue  # 不是最后一次出现，丢弃
        out.append(m)
    return out


def build_context(messages, max_chars=3000, keep_questions=5, current=None):
    """
    构造精简上下文，控制 token 开支。

    策略：
      1. 按真实时间序（雪花 ID）排序
      2. 去重（机器人重复推送的同一题只保留最后一次）
      3. 从后往前取消息
      4. 「学习反馈」只保留判定+关键解析（截断）
      5. 总字符数超限则从最早的消息开始丢
      6. 明确标出"当前要回答的这道题"（current），避免模型答错题

    messages: 已 denoise 过的消息列表
    current: dict，形如 {"id","text","qnum","body"}，指向当前待答题消息；
             若给出，会在上下文中显式标注 [★当前待答题目★]
    返回：拼好的上下文字符串
    """
    if not messages:
        return ""

    # 统一按真实时间排序
    messages = sort_by_time(messages)

    # 去重：相同内容只保留最后一次出现
    messages = dedup_keep_last(messages)
    if not messages:
        return ""

    cur_id = (current or {}).get("id")
    picked = []
    for m in reversed(messages):
        t = m["text"]
        mtype = m.get("type") or classify(t)

        if mtype == TYPE_FEEDBACK:
            # 反馈 = 判定 + 解析（可截断） + 可能夹带的新题（**必须完整保留选项**）
            head = t.split("掌握度")[0]
            head = re.sub(r"\s+", " ", head).strip()
            if len(head) > 160:
                head = head[:160] + "…"
            _q, _qb = extract_pending_question(t)
            if _qb:
                tag = f"第 {_q} 题" if _q is not None else "新题"
                line = f"{head}\n【{tag}】{_qb}"
            else:
                line = head
            picked.append(("机器人", line, m["id"]))
        else:
            picked.append(("机器人" if m["is_from_other"] else "我", t, m["id"]))
        # 只保留最近 keep_questions 题范围内的消息，控制长度
        if len(picked) >= keep_questions * 6:
            break

    # 反转回正序
    picked.reverse()

    # 控制总长度：从前面丢
    def total_len(items):
        return sum(len(w) + len(c) + 8 for w, c, _ in items)

    while picked and total_len(picked) > max_chars:
        picked.pop(0)

    # 保证至少留下当前待答题
    if not picked and messages:
        last = messages[-1]
        picked = [("机器人" if last["is_from_other"] else "我", last["text"], last["id"])]

    lines = ["以下是我和教学机器人「AI教学平台」的最近对话：", ""]
    for who, content, mid in picked:
        if cur_id and mid == cur_id:
            lines.append(f"[{who}] {content}")
            lines.append("")
            lines.append("↑↑↑ 上面这一条是机器人【最新】发来的消息，")
            _q = (current or {}).get("qnum")
            _b = (current or {}).get("body")
            _tag = f"第 {_q} 题" if _q is not None else "最新这道题"
            if _b:
                lines.append(f"我需要回答的就是其中这道题：{_tag} —— {_b}")
            else:
                lines.append("我需要回答的就是其中最新提出的那道题。")
            lines.append("注意：只需要回答这一道题，不要回答更早的题目。")
        else:
            lines.append(f"[{who}] {content}")
    lines.append("")
    lines.append("请针对上面【最新】的这道题，给出我下一条应该发送给机器人的回复内容。")
    return "\n".join(lines)


def extract_current_course(messages):
    """从消息中提取当前正在学习的课程名（用于提醒类消息的兜底）。"""
    for m in reversed(messages):
        t = m.get("text", "")
        mt = re.search(r"正在学习[:：]\s*(.+?)(?:\n|$)", t)
        if mt:
            return mt.group(1).strip()
    return None


# ---------- 雪花 ID 时间序 ----------
def snowflake_ts(mid):
    """
    飞书消息 id 是雪花 ID（Snowflake），其高位就是时间戳。
    提取时间戳分量，用于还原真实先后顺序。
    纯数字 id 才有意义；否则返回 0。
    """
    s = str(mid or "")
    if not s.isdigit():
        return 0
    return int(s) >> 22


def sort_by_time(messages):
    """按雪花 ID 的时间分量升序排列（即真实时间顺序）。"""
    return sorted(messages, key=lambda m: (snowflake_ts(m.get("id")), int(m["id"]) if str(m.get("id", "")).isdigit() else 0))


# ---------- 提取"当前待答的题" ----------
# 反馈卡片尾部夹带的下一题形如：
#   掌握度: 15% / 第 3 题 / 请用自己的话说说「以太坊开发环境」…  ⚠️ 需切换课程…
_QNUM_RE = re.compile(r"第\s*(\d+)\s*题")
# 以"掌握度: N%"为锚，其后第一个"第 N 题"才是真正要答的新题
# （解析文字里可能也提到题号，所以必须从锚点之后再找）
_TRAILING_Q_RE = re.compile(r"掌握度[:：]\s*\d+\s*%\s*(.*)$", re.S)
_ASK_RE = re.compile(r"第\s*(\d+)\s*题")
# 题目尾部开始的提示行：这些之后的内容一律不要
_TAIL_STOP_RE = re.compile(r"💡\s*直接回复|⚠️\s*需切换课程|🔍")
# 第③种推送形态「请直接回复选项作答 / 当前题目：…」的锚点与尾部截断
_CQ_ANCHOR_RE = re.compile(r"当前题目\s*[：:]?\s*")
_CQ_HEAD_JUNK_RE = re.compile(r"^\s*请直接回复选项作答\s*")
_CQ_STOP_RE = re.compile(
    r"\n\s*(?:回复\s*A\s*/\s*B|不明白题目意思|想结束练习|回复「|回复\"|💡|⚠️)"
)
# 卡片里题号形如「第 5 题（掌握度 0%）」，题号后面可能带括号说明
_QNUM_LEAD_RE = re.compile(r"^\s*第\s*(\d+)\s*题\s*(?:[（(][^）)]*[）)]\s*)?[：:]?\s*")
# 选项行（A. / B、 / C) / D． 等形式）
_OPTION_LINE_RE = re.compile(r"^\s*[A-D][\.、．)）]\s*")
# 题干里需要去掉的前后缀碎片
_BODY_NOISE = [
    r"^[（(]\s*掌握度\s*\d+\s*%?\s*[）)]\s*",
    r"^[（(]\s*掌握度[^）)]*[）)]\s*",
    r"💡\s*直接回复[^\n]*",
    r"⚠️\s*需切换课程[^\n]*",
    r"🔍[^\n]*",
]


def _strip_body(body):
    """清理题干里的 UI 残留。**保留换行结构**（选项必须分行才看得懂）。"""
    if not body:
        return body
    body = body.strip()
    for pat in _BODY_NOISE:
        body = re.sub(pat, "", body)
    # 只压行内多余空白，不碰换行
    body = re.sub(r"[ \t]+", " ", body)
    # 选项前缀去重：卡片里是 "A. A. start.sh"，规范成 "A. start.sh"
    body = re.sub(r"(?m)^([A-D])[\.、．)）]\s*\1[\.、．)）]\s*", r"\1. ", body)
    # 折叠多余空行（保留单换行）
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip() or None


def extract_pending_question(text):
    """
    从单条机器人消息里抽出"真正要回答的那道题"。

    机器人有三种消息形态：
      A. 「AI 自适应问答」卡片：整条就是一道题（带「第 N 题」）
      B. 「学习反馈」卡片：先给上一题判定，尾部夹带"第 N 题 + 题干"
      C. 「请直接回复选项作答 / 当前题目：…」：整条就是一道题，**常不带题号**

    返回 (题号 or None, 题干 str or None)
    """
    if not text:
        return None, None

    if "AI 自适应问答" in text or "AI自适应问答" in text:
        m = _ASK_RE.search(text)
        qnum = int(m.group(1)) if m else None
        if m:
            body = text[m.end():]
        else:
            idx = text.find("题")
            body = text[idx + 1:] if idx >= 0 else text
        # 砍掉尾部的操作提示
        body = _TAIL_STOP_RE.split(body)[0]
        return qnum, _strip_body(body)

    if "学习反馈" in text:
        # 以"掌握度: N%"为锚点，其后第一个"第 N 题"才是新题；
        # 一直取到尾部提示为止 —— **这样题干与 A/B/C/D 选项都会保留**。
        m = _TRAILING_Q_RE.search(text)
        if m:
            rest = m.group(1)
            qm = _QNUM_RE.search(rest)
            if qm:
                body = rest[qm.end():]
                body = _TAIL_STOP_RE.split(body)[0]
                return int(qm.group(1)), _strip_body(body)

    # C. 第③种推送形态：以「当前题目：」为锚（可能带「第 N 题」，也常不带）
    if "当前题目" in text or "请直接回复选项作答" in text:
        m = _CQ_ANCHOR_RE.search(text)
        if m:
            rest = text[m.end():]
        else:
            # 没有「当前题目」锚点，就把开头的"请直接回复选项作答"去掉
            rest = _CQ_HEAD_JUNK_RE.sub("", text)
        qm = _QNUM_LEAD_RE.match(rest)
        if qm:
            qnum = int(qm.group(1))
            body = rest[qm.end():]
        else:
            qnum = None
            body = rest
        body = _CQ_STOP_RE.split(body)[0]
        body = _TAIL_STOP_RE.split(body)[0]
        return qnum, _strip_body(body)

    return None, None


def question_fingerprint(text):
    """
    题目指纹：把题干规范化为可比较的 key。

    关键点：机器人的同一道题会出现两种形态——
      A. 「AI 自适应问答」卡片：题干 + 完整选项
      B. 「学习反馈」尾部夹带：题干（可能截断）
    两者文字不完全相同，因此指纹取【题号 + 题干前 N 字的核心片段】，
    并额外提供 core 便于做包含关系匹配。
    返回 (qnum, core) 元组；无题则返回 None。
    """
    qnum, body = extract_pending_question(text)
    if not body:
        return None
    # 只取题干：剔除 A./B./C./D. 选项行，避免选项文字干扰指纹
    stem_lines = [l for l in body.split("\n") if not _OPTION_LINE_RE.match(l)]
    stem = " ".join(stem_lines).strip() or body
    core = re.sub(r"[\s\W]+", "", stem)
    core = core[:40]
    if not core:
        return None
    return (qnum, core)


def fp_to_str(fp):
    """指纹 -> 可 JSON 序列化的字符串（'qnum|core'）。"""
    if not fp:
        return None
    qnum, core = fp
    return f"{qnum if qnum is not None else ''}|{core}"


def fp_from_str(s):
    """字符串 -> 指纹元组。"""
    if not s or "|" not in s:
        return None
    q, core = s.split("|", 1)
    qnum = int(q) if q.isdigit() else None
    return (qnum, core)


def _fp_match(fp, answered_fps):
    """
    判断指纹是否已答过。
    除完全相等外，还做"一方完整包含另一方"的模糊匹配，用于覆盖
    「同一题在不同推送形态下文字略有差异」的情况（反馈卡常截断题干或
    附带「🪜 考点提示：F」等尾注；第③种形态「当前题目：…」常不带题号）。
    仅当两边都带题号且不同时才判定为"不同题"；任一方无题号时靠题干整体
    包含来判定（并要求重叠≥12 字，避免极短题干误判）。
    ...

    ⚠️ 曾经这里还有一条 `core[:16] == oc[:16]` 的"前 16 字符相同即同题"规则，
    后果严重，已删除：本项目题目大量以「在Solidity智能合约中若要…」这类
    固定句式开头，只要**题号相同**，两道彻底不同的题就会被误判为同题 →
    find_pending_question 在 latest_only 模式下直接返回"无新题" →
    程序持续待命、**永不作答**（实测：第 10 题因与旧第 10 题共享前 16 字，
    被静默跳过 7 分钟）。
    短前缀比较**不能**用于判同，必须要求"整体包含"。

    answered_fps: 指纹字符串集合（见 fp_to_str）
    """
    if not fp:
        return False
    if fp_to_str(fp) in answered_fps:
        return True
    qnum, core = fp
    if not core:
        return False
    for s in answered_fps:
        old = fp_from_str(s)
        if not old:
            continue
        oq, oc = old
        if not oc:
            continue
        # 两边都有题号且不同 → 一定不是同一题（避免"同题号不同题"误判）
        if oq is not None and qnum is not None and oq != qnum:
            continue
        # 一方完整包含另一方（含前缀/后缀截断）→ 视为同一题。
        # 第③种推送形态（「当前题目：…」）常常**不带题号**（qnum=None），
        # 此时只能靠题干整体包含来判定；为避免极短题干误判，要求重叠长度≥12。
        if oq is None or qnum is None:
            if min(len(core), len(oc)) < 12:
                continue
        if core in oc or oc in core:
            return True
    return False


def find_pending_question(messages, answered_fps=None, latest_only=True, on_skip=None):
    """
    从消息列表里找出"当前应该回答的那道题"。

    策略：
      1. 按真实时间排序
      2. 从后往前找最新一条带题目的机器人消息（提问卡片 or 反馈夹带）
      3. 若该题已被答过（指纹匹配），继续往前找
      4. latest_only=True：只在"最新那道题未被答过"时返回，否则返回 None。
         即【只答最新一题，不补答历史旧题】——避免程序启动时把一堆旧题
         全部刷一遍。
      5. latest_only=False：往回找第一道未答的题（用于手动补答）

    on_skip: 可选回调 on_skip(qnum, fp)。当某道题因"指纹已答过"被跳过时调用，
             用于让上层把"跳过了哪道题"记进日志 ——
             否则日志里只有"待命"，无法区分"机器人没推新题"与
             "推了新题但被判成旧题"（后者曾导致某题被静默跳过 7 分钟）。

    返回 (消息 dict, 题号, 题干) 或 (None, None, None)
    """
    answered_fps = answered_fps or set()
    for m in reversed(sort_by_time(messages)):
        if not m.get("is_from_other"):
            continue
        t = clean_text(m.get("text", ""))
        if not t:
            continue
        if classify(t) == TYPE_REMINDER:
            continue
        fp = question_fingerprint(t)
        if not fp:
            continue
        qnum, body = extract_pending_question(t)
        if _fp_match(fp, answered_fps):
            if on_skip:
                try:
                    on_skip(qnum, fp)
                except Exception:
                    pass
            if latest_only:
                # 最新这一题已答过 → 认为当前没有待答题，不再往前补
                return None, None, None
            continue
        return m, qnum, body
    return None, None, None
