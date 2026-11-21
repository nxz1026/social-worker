# social-worker · 社工工作台

一线社工的**活动材料生产线**：输入少量信息 → 出整套材料 → 归档。

不是知识库。核心链路是「项目 → 活动 → 材料」，检索是副产品。

## 一句话流程

```
项目申报书 → 拆出活动清单 → 按指标建活动目录
                              ↓
活动前：填活动信息 → 生成计划书/通知/签到表
活动后：传照片 → 生成简报/总结/图注
报账：  传发票 → 三表校验（决算=报销=发票）→ 生成明细表
收尾：  15 项核对 → 归档 → 同步 Obsidian/飞书
```

## 为什么不是知识库

早期版本是「飞书丢文件 → 自动分类 → 转 md → 建索引」，结果**归档完就不再打开**。
现在改成活动驱动：文件是为办活动产生的，不是先堆着等检索。

## 仓库结构

```
social-worker/
├── api/
│   ├── app.py             FastAPI 实例、CORS、异常处理、路由挂载（554 行）
│   ├── deps.py            认证依赖与权限校验（current_user / require_admin / check_project）
│   ├── auth.py            账号、会话、登录限速、API Key 管理
│   ├── admin.py           用户 CRUD、踢会话、审计查看（仅 admin）
│   ├── state.py           配置派生常量 + 进程内缓存
│   ├── errors.py          ApiError（机器可读错误码）
│   ├── apikey.py          API Key 认证（sk-sw- 前缀，sha256）
│   ├── keysafe.py         Key 明文加密（AESGCM，密钥单独 0600 文件）
│   ├── settings.py        配置加载
│   ├── status.py          文档状态推导
│   ├── shared/            跨域共用的纯逻辑
│   │   ├── paths.py       路径归一、越界防护、生成件覆盖保护
│   │   ├── domain.py      项目/活动定位、编号、模板解析
│   │   ├── audit.py       审计写入与脱敏
│   │   ├── delivery.py    交付完整性判断
│   │   └── parse.py       布尔解析
│   └── routes/            按业务域拆分的端点
│       ├── projects.py    项目 CRUD、活动清单、核对、编号
│       ├── activities.py  按指标建目录、改名、详情
│       ├── materials.py   核对、索引、模板填充、LLM 生成、图注
│       ├── assets.py      附件、上传下载、发票、照片、满意度
│       ├── agent.py       /api/sw/* Agent 接口
│       ├── docs.py        /api/docs/* 文档归档与检索
│       └── misc.py        检索、生成、模板、飞书同步、Cognee、仪表盘
├── engine/                业务逻辑，不依赖 FastAPI
│   ├── projects.py        项目主数据读写
│   ├── activities.py      按 indicators 拆活动、建目录
│   ├── coding.py          编号生成 CDYJR{年}{月:03d}-C-{类型}{活动年}{月}{流水}
│   ├── ingest.py          活动包归档入库
│   ├── fill.py            模板占位符填充
│   ├── activity.py        数据合成（项目 yaml + activity.yaml + 用户输入）
│   ├── calc.py            人民币大写、金额格式化、求和
│   ├── llm.py             LLM 唯一出口（三层 prompt：角色+文风样例+facts骨架）
│   ├── checks.py          活动 15 项核对
│   ├── project_checks.py  项目 19 项核对
│   ├── invoice.py         发票解析、三表金额校验
│   ├── caption.py         挑图 + 生成图注
│   ├── markdown.py        docx/xlsx/pdf → Markdown
│   ├── doc_mirror.py      docx → 同名 .md 骨架（Obsidian 图谱用）
│   ├── readme.py          活动/项目索引 md
│   ├── cognee_index.py    图谱入库
│   └── llm_styles/        文风样例（务实/平实/党建）
├── scripts/               运维脚本 + 旧流程归档链路
├── config/
│   ├── api.yaml           API 配置（LLM、PG DSN）
│   ├── llm_style.yaml     文风规则
│   └── server_endpoint.txt 对外地址（不进 git）
├── templates/library/     15 个模板，占位符统一 {{字段}} 格式
├── projects/              三个项目的资料
├── web/                   Web 管理界面（纯原生 JS）
├── tests/                 pytest 用例（数量以 pytest 输出为准）
├── deploy.sh              同步到生产并重启
└── docs/
    ├── API-使用说明.md     面向 Agent 的接口文档
    ├── 照片上传协议.md      照片批量上传约定与三层防重复
    ├── 架构设计-v2.md      多用户 + 权限 + API 架构
    └── 服务器部署.md       运维手册
```

## 四个任务闭环

| # | 输入 | 输出 |
|---|---|---|
| T1 活动前 | 活动信息表单 | 计划书、活动通知、签到表、物资清单、预算表 |
| T2 活动后 | 现场照片 | 简报、总结报告、满意度汇总表、图注 |
| T3 报账 | 发票 | 决算表、报销单、发票明细表（含三表校验） |
| T4 归档 | — | 15 项核对 + 归档目录 |

**覆盖保护**：`/api/sw/run` 目标文件已存在、且活动 `status` 为 `reviewed`/`signed`/`edited`/`imported`
时返回 409 `NEED_FORCE`，需显式传 `force=true` 才覆盖。纯生成件（`generated` 或未标注）直接覆盖。

## 代码 vs LLM 的分工

**写数字的归代码，写文字的归 LLM。**

| 归代码 | 归 LLM |
|---|---|
| 项目编号、档案编号 | 活动目的、活动目标 |
| 金额、合计、人民币大写 | 简报正文（导语/目的/过程/成效） |
| 15/19 项核对 | 总结报告 19 段 |
| 占位符填充 | 监督记录活动内容 |
| 日期、项目名、归档人 | 图注 |
| 表格行数推算 | 发票字段抽取（需人工核对） |

**硬约束**：`fill.py` 只接数据不调 LLM。要 LLM 写文字时先由 `llm.py` 产出，塞进 data 再填。

## 模板

15 个模板，占位符统一 `{{字段名}}`，多行表格带序号 `{{字段_1}}`/`{{字段_2}}`。

| 模板 | 谁填 |
|---|---|
| 计划书、简报、总结报告、监督记录 | LLM + 代码 |
| 活动目录、项目管理目录、对公付款申请单、服务对象满意度调查表 | 纯代码 |
| 决算表、报销单、支出证明单、对公付款申请单 | **人工签字，不生成** |
| 物资领取表、志愿者补贴领取表、劳务领取表 | **人工填写，不生成** |

## 权限

- **项目级**：`readonly` / `write`
- **角色级**：`admin` / `editor` / `viewer`
- **Agent 认证**：`Authorization: Bearer sk-sw-xxx`（长期有效，可吊销）

## 安装

```bash
pip install -r requirements.txt
```

版本已按当前部署环境锁定；OCR / PDF 转换依赖（rapidocr、docling 等）体积较大，只在 Docker 镜像里安装，见 `Dockerfile`。

## 快速开始

```bash
# 测试
python3 -m pytest tests/ -q

# 资产验收（模板与业务数据不入库，跳过不算通过）
python3 scripts/verify_assets.py

# 部署（改完代码必跑；内含路由数 + 资产验收两道门禁）
./deploy.sh

# Agent 查能力
curl -H "Authorization: Bearer sk-sw-xxx" https://<host>/sw-api/api/sw/capabilities
```

> 模板文件与项目资料**不入 git**（合规要求）。干净检出下资产类测试会 skip，
> `pytest` 仍显示通过——`verify_assets.py` 覆盖 9 个文件 99 项，把跳过当失败。
> `deploy.sh` 把它放在**同步之前**：验收不过就不部署。
>
> 有资产环境下完整测试：**644 passed, 0 skipped**（2026-11-22 实测）。

## 文档

| 文档 | 性质 | 内容 |
|---|---|---|
| [API-使用说明](docs/API-使用说明.md) | 当前契约 | Agent 接口：认证、错误码、端点、工作流 |
| [照片上传协议](docs/照片上传协议.md) | 当前契约 | 照片批量上传约定、三层防重复、压缩规格 |
| [服务器部署](docs/服务器部署.md) | 当前契约 | 服务清单、部署、数据库、同步、排障 |
| [架构设计-v2](docs/架构设计-v2.md) | 架构参考 | 多用户、权限矩阵、数据模型；含 2026-11-08/09 历史实施记录 |

**端点数与测试数会随迭代变化**，以 `python3 scripts/stats.py` 与 `pytest` 输出为准。

## 环境要求

- Python 3.14
- PostgreSQL 18
- LibreOffice（docx 转 PDF 预览）
- Docker（Cognee + Syncthing）
- openpyxl / python-docx / psycopg / cryptography

## 已知限制

| 限制 | 说明 |
|---|---|
| 活动目录无删除 API | 只能删单个文件（移 `.trash`），不能删目录 |
| `tasks` 端点返回 501 | 无真实异步任务，所有操作同步完成 |
| 满意度分数需人工录 | 手写体不 OCR |
| 旧项目键歧义 | `huadu` 可能指激励或保障，读旧台账按列表顺序 |
