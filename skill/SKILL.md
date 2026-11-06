---
name: social-work
description: 社工工作助手：检索 Cognee 知识库（政策文件、项目资料、发票 OCR）并生成社工文书（个案记录、家访提纲、服务计划、活动方案、项目申报、评估报告）。凡涉及政策、法规、项目资料、个案记录的提问，先用知识库检索再回答。
---

# social-work

`ROOT` = `/srv/social-work`（`SOCIAL_WORK_ROOT` 可覆盖）；服务端 `http://127.0.0.1:8000`。

## 命令（都在 `"$ROOT/scripts/`）

| 命令 | 什么时候用 |
|---|---|
| `archive.py --file <路径>` | **收到文件只调这条** |
| `scan-inbox.py [--dry-run]` | 手动补跑（15 分钟自动） |
| `cognee.py search "<查询>" --dataset <名>` | 检索（`--type` 默认 auto） |
| `cognee.py sync <vault> --dataset <名>` | 同步 md → 库（幂等，先 `--dry-run`） |
| `audit.py --date <日期>` | 看审计（22:00 自动发飞书） |
| `fallback.py --list` | 看待兜底 |

数据集：`huadu` → `vault-huadu/md/`；`huimin` → `vault-huimin/md/`

## 用在哪

- 政策/法规/项目资料/个案记录提问 → 先 `search`，用检索结果，不凭记忆
- 用户发来文件 → `archive.py`
- 生成文书 → 读 `templates/<type>.md` + `review-checklists/<type>.yaml`，先 `search` 取政策与案例，按模板填充、引用标来源、不补造事实

路由：个案/服务记录→`case-record`｜家访提纲→`home-visit-outline`｜服务计划→`service-plan`｜活动方案→`activity-plan`｜项目申报→`project-application`｜评估报告→`evaluation-report`；匹配失败列出六种让用户选。

## 硬约束

- **禁止手工往飞书「已分类」写文件**（绕过台账与索引）
- 图片默认**只存档**，不进 Cognee；要检索图片文字让用户放「未分类/要OCR/」
- 含真实姓名/证件号/电话/精确地址 → 提醒用户先脱敏，不代为脱敏
- 不编造政策文号、统计数据、案例细节；引用给可核查来源；正文 ≤14,000 字
- 文件权限 644、目录 755，**写盘后立刻 `chmod`**
