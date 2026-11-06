# social-worker

社工知识库：**原始文件存档 + 转 md 入 vault 与知识库**，Obsidian 浏览，DSH skill 生成文书。

- 服务端：Cognee（向量 + 图谱，全本地模型，个案资料不出本机）
- 入口：飞书「未分类」→ 定时脚本自动分类、转 md、归档、建索引
- 客户端：两个项目各自独立的 Obsidian vault，Syncthing 双向同步
- DSH skill：检索知识库 + 按 6 类模板生成社工文书

## 仓库结构

```
social-worker/
├── config/
│   ├── config.yaml                # M8 服务端配置：root / 容器名 / 数据集 / LLM
│   ├── feishu-flow.yaml.example   # 飞书目录 token / 项目 / 类别映射（填值后改名去掉 .example）
│   ├── entities.yaml              # 实体名册：转换时自动插 [[链接]]
│   ├── project_aliases.yaml       # M2 规则层：项目 → 关键词（改词不用改代码）
│   ├── category_keywords.yaml     # M2 规则层：六类 → 关键词
│   ├── rule_weights.yaml          # M2 规则层：文件名/正文预览权重
│   ├── image_policy.yaml          # M1 图片闸门：目录 → 动作（要OCR/要识图/只存档）
│   └── notify.yaml.example        # M3 通知收件人 open_id（填值后改名去掉 .example）
├── scripts/
│   ├── config.py                  # 配置加载：config/config.yaml + 环境变量覆盖
│   ├── classify_rules.py          # M2 分类规则层：判不了才走 LLM（纯函数，无路径依赖）
│   ├── image_policy.py            # M1 图片闸门：按目录决定 ocr/vision/只存档（纯函数）
│   ├── audit_log.py               # 审计日志写入口：state/audit/YYYY-MM-DD.jsonl
│   ├── audit.py                   # M3 审计摘要：读审计日志出人读汇总，--send 发飞书
│   ├── notify.py                  # M3/M4 飞书通知统一出口（失败只返回 False）
│   ├── retry.py                   # M4 重试封装：暂时性故障指数退避，不可重试的立刻抛
│   ├── error_queue.py             # M4 错误队列：state/errors/open.yaml + 每日 jsonl
│   ├── fallback.py                # M4 待兜底清单：汇总待处理项成 md，供 LLM 处理
│   ├── convert-md.py              # 原始文件 → vault 里的 md
│   ├── archive.py                 # M5 一条命令归档：定项目 → 放 inbox/ → 跑 scan-inbox → 校验
│   ├── scan-inbox.py              # inbox/ 队列 → 分类 → 转 md → 归档 → 入库（定时跑）
│   └── cognee.py                  # 包装 cognee-cli：search / add / cognify / sync
├── skill/                         # DSH skill 源（部署到 ~/.dsh/skills/social-work/，改完要同步）
│   ├── SKILL.md
│   ├── enabled-skills.yaml
│   ├── templates/*.md             # 6 套文书模板
│   └── review-checklists/*.yaml   # 6 份复核清单
├── docs/
│   ├── 服务器部署.md               # 部署与运维（docker / syncthing / 踩坑）
│   ├── 资料存放说明.md             # 飞书端 + 服务器端目录结构、数据流、FAQ
│   └── 流程review.md              # 流程 review：哪些该用代码，哪些留给 LLM
├── docker-compose.yml             # cognee + syncthing
├── Dockerfile                     # 官方镜像 + 本地 OCR + Office loader + 模型预热
├── syncthing-add-device.sh        # 把客户端注册进 vault 同步
├── .env.example                   # 服务端配置模板
├── .gitignore                     # 排除密钥、真实配置、运行时数据
└── tests/
    ├── test_repo.py               # 结构自检：脚本可编译、配置可解析、无密钥泄漏
    ├── test_sync.py               # M6 sync 幂等 / 去重 / 非文本条目保留
    ├── test_classify_rules.py     # M2 规则层 + 真实归档文件回放
    ├── test_image_policy.py       # M1 图片闸门 + 审计（含端到端）
    ├── test_audit.py              # M3 审计摘要 + 通知
    ├── test_retry.py              # M4 重试
    ├── test_error_queue.py        # M4 错误队列
    ├── test_fallback.py           # M4 待兜底清单
    ├── test_archive.py            # M5 一条命令归档（端到端）
    └── test_search.py             # M7 检索参数自动选择
```

## 数据流

```
飞书「未分类」（手机/电脑随时传）
   │  定时（15 分钟）scan-inbox.py 拉取
   ▼
图片闸门（M1，按所在目录）
   ├─ 要OCR/   → 本地 OCR 后转 md（可检索文字）
   ├─ 要识图/  → 视觉模型（默认关闭）
   └─ 其他(含根) → 只存档：不 OCR、不调视觉模型、不生成 md
   ▼
分类（M2 规则层先判：文件名×10 + 正文×1，唯一最高分才用；
       正文「自报家门」的词如发票/红头文件直接定类别；判不了才走 LLM）
   │
   ├─ 原件 → sources/projects/<项目>/<类别>/
   ├─ md   → vault-<项目>/md/<类别>/<文档名>.md（顶部带原件链接 + [[实体]]）
   └─ 飞书 → 移到「已分类/<项目>/<类别>/」
   │
   ▼
Cognee 入库 + 建图          →  检索/生成时引用
   │
   ▼
Obsidian（客户端）           →  人看、人改（Syncthing 双向）
```

两个 vault 物理隔离：**项目的所有东西（原始文件 + 笔记 + 生成的文书）都在各自项目里**。

> 图片默认只存档：OCR / 视觉模型都是成本，必须用户把图片放进「要OCR/」才跑。
> 详见 `config/image_policy.yaml` 与 `docs/流程review.md` A9。

## 部署

```bash
# 1. 配置
cp .env.example .env && $EDITOR .env
cp config/feishu-flow.yaml.example config/feishu-flow.yaml && $EDITOR config/feishu-flow.yaml
# config/config.yaml 是服务端配置（root / 容器名 / 数据集 / LLM），默认值已指向 /srv/social-work

# 2. 起服务（首次构建会下载并预热 docling + cognee 模型，镜像约 7 GB）
docker compose up -d --build

# 3. 健康检查
curl http://127.0.0.1:8000/health

# 4. 手动跑一次归档（正常由 systemd timer 每 15 分钟跑）
python3 scripts/scan-inbox.py --dry-run     # 只看计划
python3 scripts/scan-inbox.py               # 真跑
```

依赖：Docker、Python 3.11+、`pandoc`、`lark-cli`（飞书）。详见 [docs/服务器部署.md](docs/服务器部署.md)。

## 目录与数据集对照

| 数据集 | 内容 | md 位置 |
|---|---|---|
| `huadu` | 花都项目 | `vault-huadu/md/` |
| `huimin` | 惠民项目 | `vault-huimin/md/` |

## 重要约束

- **只索引 md。** 原始 Office / 图片留在 `sources/` 存档，不进库。
- **禁止手工往飞书「已分类」写文件。** 那是 `scan-inbox.py` 的产物，手工放会绕过台账、md 生成与索引。
- **含服务对象面部 / 证件的图片不要入库。**
- 文件权限 644、目录 755（Cognee 容器 uid=1000 要能读）。

## 安全

`.gitignore` 已排除：

- 密钥：`.env`、`.cognee-key`、`.cognee-token`、`.syncthing-key`
- 真实配置：`config/feishu-flow.yaml`（含真实 folder token）
- 运行时数据：`vault-*/`、`sources/`、`inbox/`、`state/`、`snapshots/`

**这些运行时目录含真实居民个人信息，任何情况下都不要提交。**
