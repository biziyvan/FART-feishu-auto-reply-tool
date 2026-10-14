# 飞书 AI 教学平台 · 自动回复

自动监控「AI教学平台」机器人的提问 → 调用大模型生成答案 → 自动发送回复。

**技术路线**：Playwright + Edge 浏览器自动化（有头模式，复用登录态）

---

## 一、环境（已配置完成）

- Python 虚拟环境：`.venv`（已装 playwright / pyyaml / requests）
- 浏览器：**Edge**（`channel: msedge`，无需额外下载内核）
- 登录态：保存在 `./.browser_data`，**登录一次长期复用**

如需重装：
```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install playwright pyyaml requests -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 二、使用步骤

### 第 1 步：抓取历史对话（偶尔跑一次）

```bash
.venv\Scripts\python.exe fetch_history.py
```

滚动抓取完整对话历史，**去噪去重**后保存为 `history.json`，供主程序作上下文。
- 建议：换课程、或想更新上下文时跑一次
- 平时**不需要**反复跑

### 第 2 步：启动自动回复

```bash
.venv\Scripts\python.exe step2_auto_reply.py
```

常用参数：
```bash
# 一直运行（默认）
.venv\Scripts\python.exe step2_auto_reply.py

# 最多回复 10 次后停止
.venv\Scripts\python.exe step2_auto_reply.py --max-replies 10

# 干跑：只生成答案不发送（测试用）
.venv\Scripts\python.exe step2_auto_reply.py --dry-run --max-replies 5
```

**Ctrl+C 停止**。

### 可选：环境验证 / 能力自检

```bash
.venv\Scripts\python.exe step1_check_login.py    # 登录+定位会话+读消息
.venv\Scripts\python.exe test_background.py      # 验证后台/遮挡下能否正常工作
.venv\Scripts\python.exe test_occlusion.py       # 验证页面失联后能否自动恢复
```

---

## 三、配置说明（config.yaml）

| 配置项 | 说明 |
|---|---|
| `feishu.base_url` | 飞书网页版地址 |
| `feishu.target_chat_name` | 目标会话名，默认「AI教学平台」 |
| `browser.channel` | `msedge`（Edge）/ `chrome`（Chrome） |
| `browser.headless` | **必须 false**，飞书对无头浏览器风控严格 |
| `polling.interval` | 轮询间隔秒数，默认 5 |
| `polling.jitter` | 随机抖动，降低风控概率 |
| `context.context_size` | 上下文消息条数上限，默认 30 |
| `context.max_chars` | 上下文最大字符数，默认 3000（**控制 token 开支**） |
| `reply_strategy.latest_only` | **只答最新一题**（默认 true），不补答历史旧题 |
| `llm.model` | 模型名，默认 `Qwen/Qwen3.5-35B-A3B` |
| `llm.enable_thinking` | 是否开启思考模式，默认 **false**（快 50 倍） |
| `llm.system_prompt` | 系统提示词（**准确率关键**） |
| `reply.send_delay_min/max` | 发送前随机延迟（秒），模拟真人 |

---

## 四、文件说明

```
飞书自动回复/
├── config.yaml            ← 配置
├── denoise.py             ← 去噪/选题预处理模块（核心）
├── fetch_history.py       ← 抓取历史（偶尔手动跑）
├── step1_check_login.py   ← 环境验证
├── step2_auto_reply.py    ← 自动回复主程序
├── test_background.py     ← 后台/遮挡环境能力自检
├── test_occlusion.py      ← 失联自动恢复能力自检
├── history.json           ← 去噪后的历史上下文
├── history_raw.json       ← 原始抓取数据
├── answered.json          ← 已答题指纹 + 消息ID（防重复）
├── reply_stats.json       ← 每次问答记录
├── auto_reply.log         ← 运行日志
├── debug_shots/           ← 诊断截图
└── .browser_data/         ← 浏览器登录态（勿删）
```

---

## 五、核心机制

### 1. 不反复滚动
飞书消息列表是**虚拟滚动**，且机器人会**重复推送同一题**。因此：
- **`fetch_history.py`**：偶尔手动跑，滚动抓全量历史（边滚边累积，用消息 id 去重）
- **`step2_auto_reply.py`**：日常运行**只读当前可见的最新消息，不滚动**
- 结果：启动快（~8秒），不会卡死

### 2. 只答最新一题（`reply_strategy.latest_only`）
程序**只在机器人推了新题、而且是最新那道题**时才回复。
- 答完后进入**待命状态**（日志显示 `[待命] 暂无新题，持续监控中…`），静默等新题
- 程序启动时若历史里有大量积压旧题，**一律跳过**，不会刷屏
- 若想按时间倒序把旧题全部补答，把 `latest_only` 改成 `false`

### 3. 正确识别"该答哪道题"（避免答非所问）
机器人有两种消息形态，程序能正确区分：
- **`AI 自适应问答` 卡片** = 正式题目
- **`学习反馈` 卡片** = 上一题判定，**尾部夹带下一题**

程序的做法：
1. 用**雪花 ID 高位时间戳**还原消息真实先后顺序（不依赖 DOM 顺序）
2. 用 `extract_pending_question()` 从两种卡片里抽出**当前要答的那道题**
3. 用**题目指纹**（题号 + 题干核心）判重，避免同一题重复作答
4. 在上下文里显式标注「★当前待答题目★」，防止模型串题

### 4. 去噪预处理（denoise.py）
处理三类噪音，大幅省 token：
- **UI 噪音**：无障碍提示、客户端下载提示等
- **催答提醒**：`学习提醒 / 你已经XX小时没有答题`（含误导性的课程名示例）
- **重复消息**：机器人同一题重复推送 N 次（实测重复率高达 50%）

效果：**125 条 → 约 20 条，压缩率 ~85%**。

### 5. 抗浏览器遮挡（重要）
浏览器窗口被其他软件盖住、最小化、或失去焦点时，Chromium 会对页面做**降级**：
定时器被节流、渲染被冻结、DOM 停止更新 → 读不到新消息、发送失败。

程序已做四层加固：
1. **抗节流启动参数**：关闭后台定时器节流、渲染降级、窗口遮挡检测
2. **伪装页面可见性**：让页面始终认为自己在前台（`visibilityState=visible`）
3. **不依赖键盘焦点发送**：用 CDP `Input.insertText` + `Input.dispatchKeyEvent`
   直接走浏览器原生输入层，后台也能发出
4. **失联自动恢复**：检测到页面失联时自动重载/新建页面并重进会话

> 实测：浏览器被遮挡时，`setInterval(100ms)` 3 秒内仍触发 **30/30 次**（默认会降到 0~1 次）。

### 6. 严格只发一次（防重复发送）
飞书输入框是 **Lark 富文本编辑器**，发送后输入框可能残留节点。
如果靠"输入框是否为空"判断发送是否成功，就会误判 → **重复发送**。

程序的做法：
- 成功判据是 **消息条数是否 +1**，不看输入框内容
- 三条发送路径（CDP 原生输入 / 点击发送按钮 / 键盘模拟）**每条执行前都先复查**
  是否已经发出，一旦确认已发出立即停止，绝不再走后续路径

### 7. 模型
`Qwen/Qwen3.5-35B-A3B`（硅基流动），**关闭思考模式**后响应约 1 秒。

---

## 六、如何判断程序是否正常

程序正常运行时，日志会周期性打印：
```
[待命] 暂无新题，持续监控中… (已轮询 12 轮 / DOM 9 条 / 池 110 条 / 最新: ...)
```

**看到这条说明程序一切正常**，只是已经答完最新题、在等机器人推新题（这是
`latest_only: true` 的预期行为，不是故障）。

只有出现以下内容才是真故障：
- `循环出错` / `页面失联` / `恢复失败`
- Python 进程消失

---

## 七、注意事项

1. **必须有头模式**：无头会被飞书识别，导致掉登录或验证
2. **最好别关浏览器窗口**：被遮挡已能正常工作，但**直接关闭窗口**仍会导致失去登录页面（程序会尝试自动恢复）
3. **DOM 可能变化**：飞书前端升级后选择器若失效，参照"第五节"重新探测
4. **合规**：仅用于日常答疑场景，请勿用于考试作弊
5. **更新上下文**：换课程后记得跑一次 `fetch_history.py`
6. **清理残留进程**：异常中断后如遇浏览器被锁，执行
   `taskkill //F //IM msedge.exe` 再重启程序
7. **人机边界**：程序发出的消息末尾建议保持简洁；如担心"答太快像机器"，可在
   `reply.send_delay_min/max` 加大延迟

---

## 八、版本号与更新

### 命名规则

采用**语义化版本**，格式为 `主版本 . 次版本 . 补丁号`（如 `1.2.0`）：

| 位 | 何时 +1 | 举例 |
|---|---|---|
| **主版本** | 有不兼容改动（现有配置或数据需人工处理） | 1.2.0 → 2.0.0 |
| **次版本** | 向后兼容地**新增功能**，补丁位归零 | 1.1.1 → 1.2.0 |
| **补丁号** | 只**修问题**，不加功能、不改用法 | 1.2.0 → 1.2.1 |

补充约定：

- 当前处于 **1.x 阶段**，配置格式保持向后兼容，不轻易升主版本
- **已发布的版本号不修改**，发错了就发新版本
- 逐版改动明细见 [CHANGELOG.md](CHANGELOG.md)

### 两种升级方式

| 改了什么 | 怎么升 |
|---|---|
| 仅逻辑脚本（`step2_auto_reply.py` / `denoise.py` / `fetch_history.py` / `step1_check_login.py` / `settings.py`） | 下载**逻辑更新包**（几十 KB），在界面点「📦 更新包」导入 |
| 涉及**引擎**（`launcher.py` / `runtime.py`）或打包配置 | 必须换**完整绿色版**，逻辑包无法覆盖 |

> 判断依据：看 CHANGELOG 各版本末尾的「升级提示」。

### 数据与配置的位置

程序数据（配置 / 登录态 / 日志）存放在用户数据目录，**与解压位置无关**：

```
C:\Users\<用户名>\AppData\Local\飞书自动回复助手\
```

- 默认走用户目录，升级解压到任何文件夹都不会丢配置
- 想固定到某个位置（如 `C:\飞书自动回复助手`），用配置页的「更改数据目录」
- 换电脑或给别人用：用「导出配置」拿到 `config.yaml`，放到新机器**解压目录（exe 旁）**即自动接管，不会覆盖对方已填好的配置

