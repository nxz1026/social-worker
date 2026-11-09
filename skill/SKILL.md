---
name: social-work
description: 社工工作助手：检索知识库（政策文件、项目资料、个案记录）并生成社工文书（个案记录、家访提纲、服务计划、活动方案、项目申报、评估报告）。凡涉及政策、法规、项目资料、个案记录的提问，先用知识库检索再回答。
---

# social-work

服务端 API：由 `SOCIALWORK_API` 指定（形如 `https://<host>/sw-api/api`，token 存 `~/.dsh/skills/social-work/.token`）。

## 命令

| 命令 | 什么时候用 |
|---|---|
| `api.sh archive <文件路径> [项目]` | **收到文件只调这条** |
| `api.sh search "<查询>" [项目]` | 检索（项目 huadu/huimin） |
| `api.sh list [项目]` | 文档列表 |

项目：huadu（花都）/ huimin（惠民）。

## 生成文书

读 `templates/<type>.md` + `review-checklists/<type>.yaml`，先 `api.sh search` 取政策与案例，按模板填充、引用标来源、不补造事实。

路由：个案/服务记录→`case-record`｜家访提纲→`home-visit-outline`｜服务计划→`service-plan`｜活动方案→`activity-plan`｜项目申报→`project-application`｜评估报告→`evaluation-report`；匹配失败列出六种让用户选。

## 硬约束

- 含真实姓名/证件号/电话/精确地址 → 提醒用户先脱敏，不代为脱敏
- 不编造政策文号、统计数据、案例细节；引用给可核查来源；正文 ≤14,000 字
- token 失效（401）→ 让用户重新登录 `${SOCIALWORK_WEB:-https://<host>/sw/}` 并更新 `.token`
