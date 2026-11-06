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
│   ├── feishu-flow.yaml.example   # 飞书目录 token / 项目 / 类别映射（填值后改名去掉 .example）
│   └── entities.yaml              # 实体名册：转换时自动插 [[链接]]
├── scripts/
│   ├── convert-md.py              # 原始文件 → vault 里的 md
│   ├── scan-inbox.py              # 飞书未分类 → 分类 → 转 md → 归档 → 入库（定时跑）
│   └── cognee.py                  # 包装 cognee-cli：search / add / cognify / sync
├── skill/                         # DSH skill（放 ~/.dsh/skills/social-work/）
│   ├── SKILL.md
│   ├── enabled-skills.yaml
│   ├── templates/*.md             # 6 套文书模板
│   └── review-checklists/*.yaml   # 6 份复核清单
├── docs/
│   ├── 服务器部署.md               # 部署与运维（docker / syncthing / 踩坑）
│   ├── 资料存放说明.md             # 飞书端 + 服务器端目录结构、数据流、FAQ
│   └── 流程review.md              # 流程 review：哪些该用代码，哪些留给 LLM
├── docker-compose.yml             # cognee + syncthing
├── Dockerfile                     # 官方镜像 + 本地 OCR + Office loader
├── syncthing-add-device.sh        # 把客户端注册进 vault 同步
├── .env.example                   # 服务端配置模板
├── .gitignore                     # 排除密钥、真实配置、运行时数据
└── tests/
```

## 数据流

```
飞书「未分类」（手机/电脑随时传）
   │  定时（15 分钟）scan-inbox.py 拉取
   ▼
分类（LLM 读内容 → 项目 + 类别）
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

## 部署

```bash
# 1. 配置
cp .env.example .env && $EDITOR .env
cp config/feishu-flow.yaml.example config/feishu-flow.yaml && $EDITOR config/feishu-flow.yaml

# 2. 起服务
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
| `shared-policies` | 政策文件（全员共享） | `md-shared/` |
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
- 运行时数据：`vault-*/`、`sources/`、`md-shared/`、`inbox/`、`state/`、`snapshots/`

**这些运行时目录含真实居民个人信息，任何情况下都不要提交。**
