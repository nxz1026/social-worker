# 社工资料流程 review：哪些该用代码，哪些留给 LLM

> 范围：`/srv/social-work/` 服务端脚本 + `~/.dsh/skills/social-work/` DSH skill。
> 目的：把「规则可判定」的判断从 LLM 手里拿掉（省 token + 防漏项），把「需要语义理解」的判断留给 LLM。

## 0. 结论

- 整条链上**只有一个 LLM 判断点**：`scripts/scan-inbox.py` 的 `classify()`（项目 / 类别 / 置信度 / 图片主题词）。
- DSH 侧的文书生成（路由 → 检索 → 写作 → 复核）是 LLM 本职，**不该硬化**。
- 按收益排序，值得做的五件事：

| # | 事项 | 收益 | 类型 |
|---|---|---|---|
| 1 | 分类规则化（项目 / 类别 / 图片主题词） | 省掉每文件一次 LLM 调用 | 省 token |
| 2 | 新增 `archive.py` 一条命令归档 | 堵住手工分步漏项（本次事故根因） | 防错 |
| 3 | 修 `cognee.py sync` 同名去重 bug | 数据正确性 | 修 bug |
| 4 | `--type` 选择下沉进 `cognee.py` | 省 LLM 推理、避免选错 | 省 token |
| 5 | 归档后自动写 CHANGELOG | 省 LLM 输出、记录不丢 | 省 token |

- 比单点优化更重要的是 **4 个结构性问题**（见 §4）。

## 1. 全流程与判断点地图

```
用户 / 飞书机器人
   │
   ▼
飞书「未分类」  ──systemd timer 15min──▶  scripts/scan-inbox.py
                                            │
                                            ├─ lark-cli pull                    [代码]
                                            ├─ convert-md.py 格式转换           [代码]
                                            ├─ ★ classify()                     [LLM]  ← 唯一判断点
                                            ├─ 原件落 sources/ + md 落 vault/    [代码]
                                            ├─ lark-cli move → 已分类/           [代码]
                                            ├─ state/manifest.yaml 台账          [代码]
                                            └─ cognee.py sync + cognify          [代码]
   │
   ▼
Cognee 数据集（向量 + 图谱）
   │
   ▼
DSH skill（social-work）
   ├─ ★ 路由：关键词 → 6 类模板            [LLM，规则已写在 SKILL.md]
   ├─ ★ 检索：构造查询 + 选 --type + 选 dataset  [LLM]
   ├─ ★ 模板填充（写作）                    [LLM]
   └─ ★ 复核清单逐项确认                    [LLM]
```

判断点分两类：

- **A 类｜规则可判定**：有明确特征、可枚举、结果可验证 → 应该代码化。
- **B 类｜需要语义理解**：开放域、无固定答案、需要专业判断 → 保留 LLM + skill 约束。

## 2. A 类：可硬化为代码

### A1. 分类（项目 + 类别 + 图片主题词）—— 收益最大

**现状**：`scan-inbox.py:206-253` `classify()`，每个文件一次 LLM 调用。
输入 = `CLASSIFY_PROMPT`（约 450 字）+ 文件名 + 内容前 3000 字；输出 JSON（project / category / confidence / reason / title）。
这是整条链上**唯一**的 LLM 开销，也是 `title`（图片主题词）的唯一来源。

**可硬化部分**：

| 子判断 | 现状 | 硬化方案 | 预期覆盖率 |
|---|---|---|---|
| 项目 | LLM | `entities.yaml` 已有 `projects` 名册（花都/惠民）。补一张「社区名 / 街道名 / 地名 → 项目」别名表，在**文件名 + 正文前 500 字**里命中即定 | 高（文件名常带项目名） |
| 类别 | LLM | 六类各有强特征词表 + 正则：<br>发票票据：`发票\|收据\|报销\|税号\|开票\|金额\|费用明细`<br>项目计划：`方案\|申报\|预算\|立项\|年度计划\|策划`<br>项目执行：`活动\|会议\|推文\|总结\|记录\|走访\|进展`<br>日常工作佐证：`台账\|签到\|打卡\|值班\|走访记录`<br>政策文件：`通知\|办法\|规定\|实施细则\|民发\|成民发\|文号` | 中高 |
| 图片主题词 `title` | LLM 从 OCR 提炼 | ① OCR 文本里取前 2 个高频实词；② 文件名已含主题词则直接复用；③ 复用现成的 `entity_names()` | 高 |

**建议：双层判定**（保留 LLM 作兜底，不砍掉）

```
规则命中且唯一（项目、类别各只有一个候选）
    → 直接用，0 token
规则命中但冲突（多项目 / 多类别）
    → 算命中得分差；差 ≥ 阈值 → 直接用，否则 → LLM
规则未命中
    → LLM（现有 classify()）
```

预期 **80%+ 文件零 token**，剩下 20% 走 LLM。

⚠️ **需要用户拍板**：2026-11-05 已定「图片关键词由 scan-inbox.py 分类调用里 LLM 从 OCR 提炼」。改这部分等于改用户拍过的板。

### A2. 归档动作 —— 本次事故根因，最该补

**现状**：`scan-inbox.py:archive_one()`（:319-409）已把「分类 → 落位 → 飞书归档 → 写台账 → 触发 cognify」全做完。
但**手工路径完全绕开它**：手工 `cp` 到 `sources/` + `convert-md.py` + `cognee.py add` + `lark-cli +upload`，每一步都要 LLM 自己记着做，本次就漏了飞书归档。

**硬化**：新增 `scripts/archive.py`，一条命令覆盖全链：

```bash
python3 scripts/archive.py --file <路径> --project huimin
# 内部：上传飞书未分类 → 调 scan-inbox.py → 校验（md 存在 / 台账有记录 / 检索命中）
```

收益：DSH 侧**不再有任何手工判断空间**，漏项不可能发生。这比省 token 更重要。

### A3. `cognee.py sync` 同名去重 bug —— 应修

**现状**：`cognee.py:cmd_sync()`（:175-221）只删「`name` 不在 md stems 里」的条目。
docstring 写的是「先按文件名删掉旧条目再重加」，但实现没删同名条目 → **重加后同名旧条目残留**。
2026-11-06 已实际踩到：数据集里同时存在 6726 字节和 6573 字节两份「2026-11-05-公众号推文-惠民」，只能手工 `DELETE /api/v1/datasets/{id}/data/{data_id}` 清掉。

**修法**：sync = 全量重建。先删数据集里**所有**条目，再重加所有 md。
数据集只有个位数条目，全量重建代价可忽略，且与 docstring 语义一致。

**同类问题**：`state/manifest.yaml` 只记 scan-inbox 处理过的文件，**手工归档不写台账** → 手工件无幂等保护。`archive.py` 应强制写 manifest。

### A4. 检索 `--type` 选择 —— 可下沉

**现状**：SKILL.md:79-90 用自然语言写规则让 LLM 判断（引号 → `CHUNKS_LEXICAL`；绝对日期 → `TEMPORAL`；其余 → `HYBRID_COMPLETION`）。
Cognee 本身不带 `--type` 时已自动路由，LLM 的判断是重复劳动，且可能选错（`GRAPH_COMPLETION` 会多一次 LLM 调用，更慢更贵）。

**硬化**：把选择逻辑放进 `cognee.py search`：

```python
def pick_type(q: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if re.search(r'\d{4}年\d{1,2}月\d{1,2}日', q) or re.search(r'\d{4}-\d{2}-\d{2}', q):
        return "TEMPORAL"
    if q.count('"') >= 2 or q.count('「') >= 1:
        return "CHUNKS_LEXICAL"
    return "CHUNKS"      # 默认最稳、最便宜
```

SKILL.md 只保留一句：「检索用 `cognee.py search`；默认 `CHUNKS`，要自然语言回答才显式传 `GRAPH_COMPLETION`」。

### A5. CHANGELOG 自动记录

**现状**：归档后由 LLM 手工写 `/srv/social-work/CHANGELOG.md`（本次由我写）。
**硬化**：`scan-inbox.py` 归档成功后自动追加一条，字段全都有（文件名、项目、类别、md 路径、转换器、data_id、检索 score）。
收益：省 LLM 输出 token，且记录不会漏。

### A6. dataset 选择

**现状**：SKILL.md:94 规则「检索必须指定 `--dataset`」，由 LLM 判断该搜哪个数据集。
**硬化**：`cognee.py search` 加 `--auto`，按问题里的项目关键词（花都/惠民）选 dataset；命中不了再要求显式指定。
或加 `--all` 一次搜全部数据集（Cognee 的 dataset routing 默认 `top_k=1`，跨项目问题会漏）。

### A7. `_待确认` 自动归位

**现状**：置信度 < 0.7 落 `sources/_待确认/`，等人手工归位（`scan-inbox.py:466-468`）。
**硬化**：扫描 `_待确认/`，对文件名 + 内容重跑 A1 规则，能定的自动归位；仍不定的输出清单提醒。

### A8. 已经是代码且做对了的（不用动）

| 动作 | 位置 |
|---|---|
| 格式转换（pandoc / docling / 复制） | `convert-md.py:convert_pandoc/convert_docling` |
| 实体链接插入 | `convert-md.py:insert_links()`（:108-167） |
| 增量判断 / prune / 并发 | `convert-md.py:build_jobs/prune` |
| 文件权限 chmod 644 | `scan-inbox.py:357,447` |
| 容器路径映射 | `cognee.py:to_container()` |
| 台账幂等（file_token + sha256） | `scan-inbox.py:449-453` |
| 图片 EXIF 日期命名 | `scan-inbox.py:auto_image_name/exif_date` |
| 只对产出 md 的文件触发 cognify | `scan-inbox.py:476-477` |

## 3. B 类：应保留 LLM 判断 + skill 约束

| 环节 | 为什么不能代码化 |
|---|---|
| 文书生成（6 类模板填充） | 需要语义组织、专业判断、措辞；无固定答案 |
| 检索后的回答（`GRAPH_COMPLETION`） | 开放域综合 |
| 分类的最终兜底 | 规则覆盖不到的长尾文件（扫描件、手写、多项目混合） |
| 脱敏判断 | 需语义识别真实姓名/证件号/电话；且是责任问题，必须保留人工确认 |
| 复核清单逐项确认 | 需专业判断，且是质量门禁 |
| 路由（关键词 → 6 类模板） | 已写在 SKILL.md:105-114，规则清晰，LLM 执行成本极低，不必硬化 |

## 4. 结构性问题（比单点优化更重要）

1. ~~**代码分两处**：`cognee.py` 在 `~/.dsh/skills/social-work/scripts/`，`convert-md.py` / `scan-inbox.py` 在 `/srv/social-work/scripts/`。~~
   → ✅ **已修（M8）**：统一到 repo `scripts/`；部署时 cognee.py 落到 `/srv/social-work/scripts/`，skill 里不再放脚本，SKILL.md 一律写 `"$ROOT/scripts/cognee.py"`。
2. ~~**skill 写死服务器绝对路径**（`/srv/social-work/...`，SKILL.md 里 8 处）→ 换机器即失效。~~
   → ✅ **已修（M8）**：路径收口到 `config/config.yaml`，SKILL.md 用 `$ROOT`，支持 `SOCIAL_WORK_ROOT` 覆盖。
3. **密钥与代码混放**：`.env`、`.cognee-key`、`.cognee-token`、`.syncthing-key` 与脚本同目录。
   → repo 化必须剥离，提供 `.env.example`。
4. **`feishu-flow.yaml` 含真实 folder token**，`资料存放说明.md` 含真实文件夹 URL。
   → **repo 是 public 的话不能进**，需出 `.example` 版本。
5. **运行时数据与代码同目录**：`vault-*/`、`sources/`、`md-shared/`、`inbox/`、`state/`、`snapshots/`。
   → `.gitignore` 明确排除（这些含真实居民个人信息，**绝不能进 public repo**）。
6. **无测试**：分类、归档、去重都没有回归测试。
   → repo 化时加 `tests/`，用样本文件跑 `--dry-run` 断言路径与台账。

## 5. 建议的 repo 结构

```
social-worker/
├── README.md                      # 项目说明（去内网 IP、去真 token）
├── .gitignore                     # 排除 .env / keys / vault / sources / state / inbox / snapshots
├── .env.example                   # 配置模板（值留空）
├── docker-compose.yml             # cognee + syncthing
├── Dockerfile                     # 官方镜像 + 本地 OCR + Office loader
├── config/
│   ├── feishu-flow.yaml.example   # 目录 token 占位
│   └── entities.yaml              # 实体名册
├── scripts/
│   ├── convert-md.py
│   ├── scan-inbox.py
│   ├── cognee.py
│   └── archive.py                 # 新增（A2）
├── skill/
│   ├── SKILL.md
│   ├── enabled-skills.yaml
│   ├── templates/*.md             # 6 套文书模板
│   └── review-checklists/*.yaml   # 6 份复核清单
├── docs/
│   ├── 资料存放说明.md
│   └── 流程review.md              # 本文
└── tests/                         # 新增
```

## 6. 建议落地顺序

| 阶段 | 内容 | 理由 |
|---|---|---|
| 1 | 修 `cognee.py sync` 去重（A3） | 影响现有数据正确性，先修 |
| 2 | 新增 `archive.py`（A2） | 直接堵住本次事故根因 |
| 3 | 分类规则化（A1） | 收益最大，但要用户先拍板图片主题词 |
| 4 | `--type` / dataset 下沉（A4/A6） | 小改动，立刻省 token |
| 5 | repo 化 + 测试 + `.gitignore` | 结构性收口 |
| 6 | CHANGELOG 自动记（A5）、`_待确认` 自动归位（A7） | 锦上添花 |
