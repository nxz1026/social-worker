---
name: social-work
description: 社工工作助手：检索 Cognee 知识库（政策文件、项目资料、发票 OCR）并生成社工文书（个案记录、家访提纲、服务计划、活动方案、项目申报、评估报告）。凡涉及政策、法规、项目资料、个案记录的提问，先用知识库检索再回答。
---

# social-work

## 知识库（Cognee）

> 下文命令中的 `ROOT` = 服务端根目录，默认 `/srv/social-work`，由 `config/config.yaml` 的 `root` 决定（环境变量 `SOCIAL_WORK_ROOT` 可覆盖）。

服务端 `http://127.0.0.1:8000`（容器 `social-work-cognee`）。客户端 `"$ROOT/scripts/cognee.py"` 包装官方 `cognee-cli`，自动带 API key。

**只索引 md。** 原始 Office/图片留在 `sources/` 存档，不进库。

| 数据集 | 内容 | md 位置 |
|---|---|---|
| `shared-policies` | 政策文件（全员共享） | `md-shared/` |
| `huadu` | 花都项目 | `vault-huadu/md/` |
| `huimin` | 惠民项目 | `vault-huimin/md/` |

每个项目的 vault 是独立 Obsidian 仓库，经 Syncthing 双向同步到客户端。

### 收到文件 → 归档

**不要手工拼归档流程。** 两条正路，按文件在哪选一条：

| 文件在哪 | 处置 |
|---|---|
| DSH 会话工作区 / 用户直接发来 / 机器人收到 | 放进飞书「未分类」→ 跑 `scan-inbox.py`（或等 15 分钟定时） |
| 已在 `sources/projects/<项目>/` | 跑 `convert-md.py --project <项目>` → `"$ROOT/scripts/cognee.py" sync <vault> --dataset <数据集>` |

- 飞书「未分类」token 取 `feishu-flow.yaml` 的 `feishu.inbox`；上传：`lark-cli drive +upload --file <相对路径> --folder-token <inbox>`（`--file` 只接受 cwd 下相对路径）
- **禁止手工往飞书「已分类」写文件**（含 `category_folders` 里任何 token）：「已分类」是 `scan-inbox.py` 的产物（`move_to_archive()`），手工放会绕过 `state/manifest.yaml` 台账、md 生成与 Cognee 索引。见 `资料存放说明.md:25`
- 手工往 `sources/` 丢文件只解决「原件落位」，**不含飞书归档**；要飞书副本仍走「未分类」
- `scan-inbox.py` 一条命令覆盖：分类 → 转 md → 原件落位 → 飞书归档 → 写台账 → Cognee 重建。手工分步做必漏项（飞书归档、台账、cognify 是三处常见漏点）

### 转换（原始文件 → md）

```bash
python3 "$ROOT/scripts/convert-md.py" --project all              # 增量
python3 "$ROOT/scripts/convert-md.py" --project huimin --force   # 强制重转
```

- docx → pandoc；xlsx/pptx/pdf/csv → docling（容器内）；图片/音视频直接复制到 `vault-<proj>/media/`
- 产物落 `vault-<proj>/md/<源文件相对目录>/<文档名>/<文档名>.md`
- 按 `entities.yaml` 自动插 `[[实体]]` 链接（改完名册重跑即可）
- **转完必须 `cognify`，否则检索不到**

### 飞书收件箱（自动）

用户往飞书「社工知识库 / 未分类」丢文件，服务器每 15 分钟自动：分类 → 转 md → 归档原件 → 建索引。

```bash
python3 "$ROOT/scripts/scan-inbox.py"             # 手动跑一次
python3 "$ROOT/scripts/scan-inbox.py" --dry-run   # 只报计划
```

- 分类先走代码规则层（`config/project_aliases.yaml` + `config/category_keywords.yaml` + `rule_weights.yaml`），判不了才调 LLM；规则判错率 0，改关键词不用改代码
- 分类结果落 `sources/projects/<项目>/<类别>/` + `vault-<项目>/md/<类别>/`
- 台账 `state/manifest.yaml`（file_token + sha256），重复扫描不重复入库
- 置信度 < 0.7 落 `_待确认/`，需人工归位后重跑
- 定时任务状态：`systemctl --user status social-work-scan.timer`

### 飞书机器人（dsh-im）

用户也可能直接私聊飞书机器人（Mimir）发文件/图片，或说一句「这个归花都」。

- 插件 `@xmanrui/dsh-im`；配置 `~/.dsh/integrations/dsh-feishu/config.json`
- 机器人收到的文件在 DSH 会话工作区，归档走上面「收到文件 → 归档」
- 排障：`journalctl --user -u dsh-web -n 50 | grep -i feishu`

### 命令

| 命令 | 用途 |
|---|---|
| `"$ROOT/scripts/cognee.py" search "<查询>" --dataset <名>` | 检索（**必须指定 dataset**） |
| `"$ROOT/scripts/cognee.py" add <文件> --dataset <名>` | 入库单个 md（**不支持目录**）。⚠️ 同一文件再 add 一次会**新建条目而非更新**，要更新用 `sync` |
| `"$ROOT/scripts/cognee.py" cognify --dataset <名>` | 重建知识图谱 |
| `"$ROOT/scripts/cognee.py" sync <vault> --dataset <名>` | 全量同步 md 目录（**幂等**，可反复跑；先加 `--dry-run` 看计划） |
| `"$ROOT/scripts/cognee.py" datasets` | 列出数据集 |

### `--type` 怎么选

| 类型 | 用在哪 |
|---|---|
| `CHUNKS` | 原文块（默认，最稳，不出答案） |
| `CHUNKS_LEXICAL` | 精确短语、日期、编号（如「2026年10月6日」） |
| `SUMMARIES` | 文档摘要 |
| `GRAPH_COMPLETION` | 自然语言回答（走 LLM，会引用来源） |
| `TEMPORAL` | 带时间的问题（如「10 月的活动」） |
| `HYBRID_COMPLETION` | 混合检索 + 图谱（不带 --type 时的默认路由结果） |

不带 `--type` 会自动路由：整句引号 → CHUNKS_LEXICAL；含绝对日期 → TEMPORAL；其余 → HYBRID_COMPLETION。

规则：

1. **检索必须指定 `--dataset`**：不带时 Cognee 只搜「最相关的一个」数据集（dataset routing，默认 top_k=1），跨项目问题会漏
2. 回答政策/法规/项目资料问题前必须先 `search`，用检索结果，不凭记忆
3. 新增/更新资料：先 `convert-md.py` 转 md，再 `"$ROOT/scripts/cognee.py" sync <vault> --dataset <数据集>`（**幂等**，自动删旧版本/孤儿/重复条目，未变的跳过）。`add` + `cognify` 只适合单文件应急
4. 文件权限 644、目录 755（容器 uid 1000 要能读）。**写盘后立刻 `chmod`**：`write` 工具默认产出 `-rw-------`，容器读不到
5. md/txt 直接复制；docx 走 pandoc；xlsx/pptx/pdf 走 docling；图片只复制不转
6. 图片入库会先过视觉模型（英文描述）再叠本地 OCR；含服务对象面部/证件的图片不要入库
7. 只要原文用 `CHUNKS`；`GRAPH_COMPLETION`/`TEMPORAL` 走 LLM 且默认多一次分析调用（`AUTO_FEEDBACK`），更慢更贵
8. 写入 vault 的文件同样要 `chmod 644`，否则客户端 Syncthing 同步过去、Cognee 也读不到

## 路由规则

| 关键词 | 类型 | 模板 |
|---|---|---|
| 个案记录、服务记录、接案、结案、访后记录 | case-record | templates/case-record.md |
| 家访提纲、探访提纲、访前准备 | home-visit-outline | templates/home-visit-outline.md |
| 服务计划、个案计划、干预方案 | service-plan | templates/service-plan.md |
| 活动方案、社区活动、小组活动 | activity-plan | templates/activity-plan.md |
| 项目申报、立项申请、资助申请 | project-application | templates/project-application.md |
| 评估报告、结项报告、成效评估 | evaluation-report | templates/evaluation-report.md |

匹配失败或未启用：列出六种类型及启用状态，让用户选。

## 生成流程

1. 路由 → 查 `enabled-skills.yaml`；未启用则问用户是否开启
2. 读 `templates/<type>.md` + `review-checklists/<type>.yaml`
3. 脱敏确认：含真实姓名/证件号/电话/精确地址 → 提醒用户先换编号，不代为脱敏
4. `search` 取政策与历史案例作依据
5. 按模板填充；引用标来源；不补造事实
6. 输出草稿 + 复核清单（用户逐项确认）

## 硬约束

- 正文上限 14,000 字
- 不编造政策文号、统计数据、案例细节
- 引用给可核查来源（标题+发布方+日期或 URL）
- 服务对象陈述与专业判断分段写
- 风险线索、待核实信息、后续计划单独成段
