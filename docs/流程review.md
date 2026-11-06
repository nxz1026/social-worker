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

> ✅ **已修（M2，2026-11-06）**：新增 `scripts/classify_rules.py` + `config/project_aliases.yaml`
> + `config/category_keywords.yaml` + `config/rule_weights.yaml`。`scan-inbox.py` 先跑规则，
> 判不了才调 `classify()`。**判错率 0**（判不了的返回 `None` 交 LLM，绝不猜）。
> 回放 4 条真实归档文件：判对 3、判不了 1、判错 0，命中率 75%。
> 测试见 `tests/test_classify_rules.py`。
> 图片主题词 `title` 仍走 LLM（用户 2026-11-05 拍板，未改）。

**现状**：`scan-inbox.py:206-253` `classify()`，每个文件一次 LLM 调用。
输入 = `CLASSIFY_PROMPT`（约 450 字）+ 文件名 + 内容前 3000 字；输出 JSON（project / category / confidence / reason / title）。
这是整条链上**唯一**的 LLM 开销，也是 `title`（图片主题词）的唯一来源。

**可硬化部分**：

| 子判断 | 现状 | 硬化方案 | 预期覆盖率 |
|---|---|---|---|
| 项目 | LLM | `entities.yaml` 已有 `projects` 名册（花都/惠民）。补一张「社区名 / 街道名 / 地名 → 项目」别名表，在**文件名 + 正文前 500 字**里命中即定 | 高（文件名常带项目名） |
| 类别 | LLM | 六类各有强特征词表 + 正则：<br>发票票据：`发票\|收据\|报销\|税号\|开票\|金额\|费用明细`<br>项目计划：`方案\|申报\|预算\|立项\|年度计划\|策划`<br>项目执行：`活动\|会议\|推文\|总结\|记录\|走访\|进展`<br>日常工作佐证：`台账\|签到\|打卡\|值班\|走访记录`<br>政策文件：`通知\|办法\|规定\|实施细则\|民发\|成民发\|文号` | 中高 |
| 图片主题词 `title` | LLM 从 OCR 提炼 | ① OCR 文本里取前 2 个高频实词；② 文件名已含主题词则直接复用；③ 复用现成的 `entity_names()` | 高 |

**实现（M2 实际落地）**：

规则层用的是**加权计分 + 唯一最高分**，不是「得分差阈值」：

```
得分 = 文件名命中次数 × filename权重(10) + 正文预览命中次数 × preview权重(1)
项目、类别各自取唯一最高分；并列最高或全 0 → 返回 None 交 LLM
```

- 权重差 10:1 的意图：**文件名是人工起的标签，一个命中就应压过正文里的偶然提及**。
  实测 `2026-11-05-公众号推文-惠民.docx` 正文里「走访」5 次、「台账」3 次，
  若按原计划的「得分差阈值」，很容易翻盘判成「日常工作佐证」——那是错的。
- 项目判不出来时，正文预览可以补（`2026-11-03-公众号推文-B版-温暖生活.docx` 文件名无项目名，
  靠正文里的「惠民」补上）。
- 「共享」不靠关键词，走 `fallback`：没判出项目但类别是「政策文件」→ 共享。

回放命中率 75%（3/4），唯一判不了的是 `2026-11-04-宣传栏-微网实格.jpg`
（纯图片、文件名无项目名）—— 正确落回 LLM。

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

**修法**：~~sync = 全量重建。先删数据集里**所有**条目，再重加所有 md。~~
→ ✅ **已修（M6）**，但没采用「删所有条目」的做法，改为按 `(name, contentHash)` 增量比对：

- 关键发现：Cognee 条目自带 `contentHash`，**等于文件内容的 md5**（已用生产数据核对：3 条全部吻合）。所以不需要靠「删光重加」来判断内容是否变化。
- 只删：① 同名但 hash 对不上的旧版本；② 本地已无对应 md 的孤儿；③ 同名同 hash 的重复条目（只留一条）。
- 未变的 md **跳过不重加**（省掉 add + cognify 的开销）。
- **非文本条目（图片等）一律不动**——M1 会把 OCR 过的图片加进同一个数据集，「删所有条目」会把它们清掉。
- 新增 `--dry-run` 只报计划。
- 纯函数 `plan_sync()` 抽出来单测，`tests/test_sync.py` 9 项（含「连跑两次第二次无操作」）。

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

### A9. 图片处置闸门（M1）—— 成本最高的浪费

> ✅ **已修（M1，2026-11-06）**：新增 `scripts/image_policy.py` + `config/image_policy.yaml`，
> `scan-inbox.py` 在 `extract()` **之前**按目录分流。测试见 `tests/test_image_policy.py`。

**现状（改前）**：`scan-inbox.py:extract()` 对每个图片都调 `ocr_image()`——
`docker exec` 进容器 + 跑一次 OCR 模型推理。而 `日常工作佐证` 类图片走 `work_photo` 分支
**只归档不生成 md**，OCR 结果直接丢弃。

更糟的是 `.env` 里的 `IMAGE_OCR_ENABLED` / `IMAGE_EXTRACTION_ENABLED`
（`.env:41-42`）**在代码里没有任何引用**（`grep -rn` 两侧 scripts 都是 0 条）——
是死配置，OCR 实际上一直开着。

**实现（M1 实际落地）**：

```
飞书「未分类」/<目录>/<文件>
  要OCR/   → ocr          ：本地 OCR 后转 md 入库（可检索文字）
  要识图/  → vision       ：视觉模型（默认 enabled: false）
  其他(含根) → archive_only ：只存档，不 OCR、不调视觉模型、不生成 md
```

- **目录即授权**：要花钱的处理必须用户显式放进对应目录，根目录默认零成本。
- 非图片文件完全不受影响（`decide_action` 只对 `IMAGE_EXTS` 生效）。
- 策略文件缺失/损坏 → 兜底 `archive_only`（成本最低的一侧）。
- `archive_only` 图片仍走分类（规则优先、LLM 兜底）以决定落位，但**不产 md、不入 Cognee**。
- 顺带修了一个隐患：只存档图片按「拍摄日期」命名，同一天多张会撞名互相覆盖 →
  新增 `unique_dest()`，同名不同内容时加 `-2`/`-3` 序号。

**验收（已跑）**：根目录 10 张图 → OCR 调用 **0 次**，审计日志 10 条 `action=archive_only`。

⚠️ **与计划的偏差**：计划写「不调用任何模型」，实际保留了分类用的 LLM 兜底。
理由：没有分类结果就不知道该落哪个项目，图片会全部堆进 `_待确认/`，
反而增加人工负担；而分类 LLM 调用（约 1-2k token）远小于 OCR 推理。
**判不了项目的图片仍会落 `_待确认/`**，不会瞎猜。

### A10. 每日审计与通知（M3）—— 从「静默失败」到「看得见」

> ✅ **已修（M3，2026-11-06）**：新增 `scripts/audit.py` + `scripts/notify.py`，
> 写入口是 M1 就位好的 `scripts/audit_log.py`。测试见 `tests/test_audit.py`。

**现状（改前）**：`scan-inbox.py` 全程只 `log()` 打到 stdout，没有任何持久记录。
定时任务跑完就没了 —— 文件处理失败、落 `_待确认`、转换报错，用户完全不知道，
只能等「怎么还没出现」才发现。更糟的是 `extract()` 抛异常时直接 `continue`，
**连日志都没有**。

**实现（M3 实际落地）**：

- **审计日志**：每处理一个文件写一条 `state/audit/YYYY-MM-DD.jsonl`，字段
  `ts / file / sha256 / size / converter / project / category / confidence /
  action / md_path / data_id / duration_ms / status`。
  **转换失败也会记一条** `status=error`，不再静默。
- **人读摘要**：`audit.py --date <日期>` 输出「收到 N 个文件 / 文档 N（成功 N，转换器分布）/
  图片 N（只存档/OCR/识图）/ 项目分布 / 类别分布 / 失败清单 / 待兜底清单」。
  项目名显示中文标签（读 `feishu-flow.yaml` 的 `projects.<key>.label`）。
- **每天 22:00 自动发飞书**：`social-work-audit.timer`
  （`OnCalendar=*-*-* 22:00:00 Asia/Shanghai`，systemd 自己换算成 UTC —— 系统时区是 `Etc/UTC`）。
- **通知统一出口** `notify.py`：`send()` 失败只返回 `False`，**不抛异常** ——
  通知挂掉不该拖垮归档主流程。收件人取 `config/notify.yaml`，缺失时回落到
  `~/.dsh/integrations/dsh-feishu/config.json` 的 `ownerOpenIds`。

**保留期**：`audit_log.prune()` 默认 90 天（`config/config.yaml` 的 `audit_retention_days`）。

**顺带修的 bug**：审计写在 `src.unlink()` **之后**，成功归档的文件会因
`src.stat()` 抛 `FileNotFoundError` 而丢记录（异常被吞掉只剩一行「审计写入失败」）。
改为在循环开头取好 `size`。

### A11. 错误处理与 LLM 兜底（M4）—— 不静默失败

> ✅ **已修（M4，2026-11-06）**：新增 `scripts/retry.py` / `scripts/error_queue.py` /
> `scripts/fallback.py`。测试见 `tests/test_retry.py`、`tests/test_error_queue.py`、
> `tests/test_fallback.py`。

**现状（改前）**：转换失败只在 stdout 打一行 `! 转换失败`，然后 `continue`。
没有落盘、没有告警、没有重试。用户只会看到「文件一直没出现」，无从查起。
而飞书 API 超时、Cognee 502 这类**暂时性**故障，一次失败就被当成永久失败。

**实现（M4 实际落地）**：

- **重试**（`retry.py`）：异常消息里出现 `timed out` / `502` / `503` / `504` /
  `connection` / `reset` / `refused` / `rate limit` → 指数退避重试 3 次（2s / 4s）。
  **格式不支持、文件损坏、权限不足**不在列 —— 立刻抛，不浪费重试。
  包住了 `pull_inbox` / `move_to_archive` / `cognee sync` 三处。
- **错误队列**（`error_queue.py`）：`state/errors/open.yaml`（未处理）+
  `state/errors/YYYY-MM-DD.jsonl`（全量流水）。同一 `(file, error)` 只入队一次。
- **主动告警**：本轮出现**新**错误就立刻发飞书（`_alert_errors`），不等用户来查。
  重复运行不会重复告警。
- **坏文件不无限重试**：已有未处理错误的文件下一轮直接跳过 —— 否则一个坏文件
  每 15 分钟重试一次，永久刷日志。
- **兜底清单**（`fallback.py`）：把当天 `status ∈ {pending_review, error, unsupported,
  failed}` 的审计记录 + 未处理错误汇总成一份 md，写死处理规则（只能判断/改名/写说明，
  不能决定隐私、不能直接往 `sources/` 写原件），LLM 处理完用 `--log` 写回
  `state/fallback_log.jsonl`。

**验收（已跑）**：真放一个损坏 PDF 进飞书「未分类」→ 全流程走通
「报错 → 审计 → 入队 → **飞书告警**」；第二次跑 → `= 跳过（已有未处理错误）`；
`fallback.py --list` / `--date` 正确列出待兜底项并给出真实文件位置。

**顺带修的观感问题**：docling 的异常消息带着整段 traceback（几十行），
直接进错误队列和飞书会把人淹没 → `error_queue.brief_error()` 只取第一行（真正原因所在）。

### A12. `archive.py` 一条命令归档（M5）—— 堵手工漏洞

> ✅ **已修（M5，2026-11-06）**：新增 `scripts/archive.py`，并把 `inbox/` 改成
> 统一的待处理队列。测试见 `tests/test_archive.py`（13 项）。

**现状（改前）**：DSH 侧归档一个文件要手工分四步 —— 放飞书「未分类」、跑 `scan-inbox.py`、
（有时）补 `cognee sync`、（有时）手工往飞书「已分类」放。2026-11-06 那次
「漏了飞书归档」就是这么来的：skill 里写了规定，但规定藏在「飞书机器人」节的一句话里，
没有任何东西在架构上阻止手工分步。

**实现（M5 实际落地）**：

- `archive.py --file <路径> [--project] [--action]`：定项目 → 放进 `inbox/` 对应位置
  （`要OCR/` `要识图/` 目录即授权）→ 跑 `scan-inbox.py` → 校验台账与 md 存在。
  **定项目判不了就报错退出，不猜**。
- **`inbox/` 成为统一待处理队列**：`scan-inbox.py` 除了拉飞书「未分类」，还会扫
  `inbox/` 里所有未处理文件。手工放进去的用 `local-<sha256前16位>` 当台账 key。
- 本地文件的 md 顶部**不带飞书链接**（没有 file_token），也不去飞书「已分类」找对应条目 ——
  避免了 `url_template.format(token="local-xxx")` 拼出一个假链接。

**验收（已跑）**：把一个 docx 放进 `inbox/` → `archive.py` → md 生成、台账有记录、
Cognee 能检索到；`--dry-run` 对 docx / 图片 / 定不了项目的文件分别给出正确预览；
测试 **92 项**全过。

### A13. 检索参数下沉（M7）—— 省 token

> ✅ **已修（M7，2026-11-06）**：`cognee.py search` 的 `--type` 默认改成 `auto`，
> 新增 `--auto-dataset`。测试见 `tests/test_search.py`（11 项）。

**现状（改前）**：DSH 每次检索都要自己判断该传哪个 `--type`，SKILL.md 里一张 6 行的
类型表就是为此存在的。判断错了（比如把带日期的查询交给 CHUNKS）会漏召回，判断对了也是
白烧 token。

**实现（M7 实际落地）**：

- `pick_type(query)`：含日期（`2026年` / `11月` / `11-05` 等）→ `TEMPORAL`；带引号 →
  `CHUNKS_LEXICAL`；其余 → `CHUNKS`。日期优先于引号。
- `pick_dataset(query, cfg, config_dir)`：读 `config/project_aliases.yaml`，按关键词出现次数
  计分，**唯一最高分**才返回对应数据集；并列或没命中返回 `None`（搜全部）。
  项目 key 与数据集名按约定同名，`config.yaml` 的 `datasets` 映射可覆盖。
- SKILL.md 的 `--type` 表从 6 行压成 2 句话。

**实测**：默认 → `CHUNKS`；`2026年11月的活动` → `TEMPORAL`；`"亲邻商居共治联盟"` →
`CHUNKS_LEXICAL`；`花都的入户名单 --auto-dataset` → 自动选中 `huadu`。
⚠️ `TEMPORAL` 比 `CHUNKS` 慢很多（一次 >60s），文档里已标注。

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

**实际落地（2026-11-06）**：M8 配置收口 → M6 `sync` 去重 → M2 分类规则层 → M1 图片闸门 → M3 审计摘要 + 22:00 飞书汇总 → M4 错误处理/告警/兜底 → M5 `archive.py`。
剩余：M7 检索参数下沉 → M9 测试补全 + skill 整体压缩。
