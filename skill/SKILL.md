---
name: social-work
description: 社工工作助手：检索 Cognee 知识库（政策文件、项目资料、发票 OCR）并生成社工文书（个案记录、家访提纲、服务计划、活动方案、项目申报、评估报告）。凡涉及政策、法规、项目资料、个案记录的提问，先用知识库检索再回答。
---

# social-work

`ROOT` = `/srv/social-work`（`SOCIAL_WORK_ROOT` 可覆盖）。
服务端 API：`https://140.83.62.161/sw-api/api`（token 存 `~/.dsh/skills/social-work/.token`）。

## 命令

### 对外（走 API，`api.sh` 包装）

| 命令 | 什么时候用 |
|---|---|
| `api.sh archive <文件路径> [项目]` | **收到文件只调这条** |
| `api.sh search "<查询>" [项目]` | 检索（项目 huadu/huimin） |
| `api.sh list [项目]` | 文档列表 |

### 系统内部（本机脚本，仅排障用）

| 命令 | 什么时候用 |
|---|---|
| `scan-inbox.py [--dry-run]` | 手动补跑（15 分钟自动） |
| `cognee.py sync <vault> --dataset <名>` | 同步 md → 库（幂等） |
| `audit.py --date <日期>` | 看审计（22:00 自动发飞书） |
| `fallback.py --list` | 看待兜底 |

数据集：`huadu` → `vault-huadu/md/`；`huimin` → `vault-huimin/md/`

## 用在哪

- 政策/法规/项目资料/个案记录提问 → 先 `api.sh search`，用检索结果，不凭记忆
- 用户发来文件 → `api.sh archive`
- 生成文书 → 读 `templates/<type>.md` + `review-checklists/<type>.yaml`，先 `api.sh search` 取政策与案例，按模板填充、引用标来源、不补造事实

路由：个案/服务记录→`case-record`｜家访提纲→`home-visit-outline`｜服务计划→`service-plan`｜活动方案→`activity-plan`｜项目申报→`project-application`｜评估报告→`evaluation-report`；匹配失败列出六种让用户选。

## 硬约束

- **禁止手工往飞书「已分类」写文件**（绕过台账与索引；`api.sh archive` 会自动补传一份）
- 图片默认**只存档**，**原图不进 Cognee**；M10 起按**文件名**取日期 + 单位，改名
  `<日期>-<单位>-<内容关键词>.<ext>` 并产一条「档案条目」md 进图谱；文件名里日期与单位都没有 →
  落 `sources/_待确认/<类别>/`，22:00 汇总提醒。要检索图片上的字，让用户放「未分类/要OCR/」
- 含真实姓名/证件号/电话/精确地址 → 提醒用户先脱敏，不代为脱敏
- 不编造政策文号、统计数据、案例细节；引用给可核查来源；正文 ≤14,000 字
- 文件权限 644、目录 755，**写盘后立刻 `chmod`**
- token 失效（401）→ 让用户重新登录 `https://140.83.62.161/sw/` 并更新 `.token`
