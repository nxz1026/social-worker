# 社工工作台 API 使用文档

> 面向客户端 Agent 的接口说明。Web 管理界面用另一套 `/api/*`，本文只讲 Agent 用的 `/api/sw/*`。
> 所有示例均为实跑验证过的输出。

## 1. 基本信息

| 项 | 值 |
|---|---|
| Base URL | `https://<host>/sw-api` |
| 认证 | `Authorization: Bearer sk-sw-xxx` 或 `x-api-key: sk-sw-xxx` |
| 自描述 | `GET /api/sw/capabilities`（**免认证**，Agent 先探能力） |
| 内容类型 | `application/json`，文件上传用 `multipart/form-data` |

## 2. 认证与权限

### 2.1 签发 API Key

```bash
curl -X POST https://<host>/sw-api/api/sw/keys \
  -H "x-token: <管理员 session>" -H "Content-Type: application/json" \
  -d '{"user":"admin","name":"my-agent","expires_days":365}'
```

返回：

```json
{"key":"sk-sw-xxxxxxxx","id":5,"name":"my-agent",
 "prefix":"sk-sw-pnPz","expires_at":"2027-11-19T11:06:12+08:00"}
```

**明文 `key` 只返回这一次**，之后只能看到 `prefix`。丢失就重新签发。

### 2.2 权限模型

项目级，两级：

| access | 能做什么 |
|---|---|
| `readonly` | 查项目、查活动、看核对、检索、下载 |
| `write` | 以上 + 生成文件、上传、改状态、跑 LLM |

`admin` 角色对所有项目恒为 `write`。

查自己的权限：

```bash
curl -H "Authorization: Bearer sk-sw-xxx" https://<host>/sw-api/api/sw/me
```

```json
{"username":"admin","real_name":"聂聃","role":"admin",
 "projects":{"惠民社区保障资金项目":"write","花都社区保障资金项目":"write","花都社区激励资金项目":"write"},
 "auth_type":"api_key"}
```

### 2.3 Key 管理

```bash
GET    /api/sw/keys            # 列表（admin 看全部，普通人看自己）
DELETE /api/sw/keys/{key_id}   # 吊销（软删，写 revoked_at）
```

## 3. 错误格式

`/api/sw/*` 的错误统一为：

```json
{"error":{"code":"READONLY","message":"项目 惠民社区保障资金项目 只读，不能执行写操作",
          "hint":"向管理员申请该项目的 write 权限"}}
```

| code | HTTP | 含义 |
|---|---|---|
| `UNAUTHORIZED` | 401 | 未认证 / Key 无效 / 已吊销 / 已过期 |
| `PROJECT_NOT_FOUND` | 404 | 项目不存在 |
| `ACTIVITY_NOT_FOUND` | 404 | 活动目录不存在 |
| `FORBIDDEN` | 403 | 无该项目权限 |
| `READONLY` | 403 | 该项目只读，不能写 |
| `CODE_MISSING` | 400 | 项目编号未配置 |
| `TEMPLATE_NOT_FOUND` | 404 | 模板不存在 |
| `VALIDATION` | 400 | 参数校验失败 |
| `LLM_FAILED` | 502 | 上游 LLM 调用失败 |

## 4. 读操作

### 4.1 列项目

```bash
curl -H "Authorization: Bearer sk-sw-xxx" https://<host>/sw-api/api/sw/projects
```

```json
{"total":3,"items":[{"name":"惠民社区保障资金项目","yaml_path":"...","data":{"project":{...}}}]}
```

`data` 是项目 yaml 的完整内容，含 `project.name`、`code_rules`、`budget`、`indicators`、`indicators[].act_types`。

### 4.2 列活动

```bash
curl -H "Authorization: Bearer sk-sw-xxx" \
  "https://<host>/sw-api/api/sw/projects/惠民社区保障资金项目/activities"
```

```json
{"planned":[{"seq":1,"dirname":"01-走访调研-第一期","activity_budget":1100}],
 "existing":["01-走访调研-第一期"],
 "missing":[],"extra":[]}
```

- `planned`：按项目 yaml 的 `indicators` 拆出来的计划
- `existing`：磁盘上实际存在的目录
- `missing` / `extra`：两者差异（手工改过目录时会出现）

### 4.3 活动详情

```bash
curl -H "Authorization: Bearer sk-sw-xxx" \
  "https://<host>/sw-api/api/sw/projects/惠民社区保障资金项目/activities/11-共治行动-第一期"
```

```json
{"dirname":"11-共治行动-第一期","code":"CDYJR2026008-C-1110002","status":"imported",
 "files":[{"name":"01-计划书-共治行动.docx","size":12345,"mtime":"..."}],
 "dirs":[{"name":"11-活动照片资料","count":31}],
 "check":{"ok_count":13,"partial_count":1,"missing_count":0,"na_count":1,"complete":false,
          "items":[{"no":1,"name":"活动计划","status":"ok","detail":"命中 01-...docx"}]}}
```

**15 项核对状态**：`ok` 齐 / `partial` 待补（如只有未签字 docx）/ `missing` 缺 / `na` 不适用。
`complete` 要求 `partial_count == 0` 且 `missing_count == 0`。

### 4.4 检索

```bash
curl -H "Authorization: Bearer sk-sw-xxx" \
  "https://<host>/sw-api/api/sw/search?q=决算金额&project=惠民社区保障资金项目&limit=10"
```

## 5. 写操作：一步生成

### 5.1 `POST /api/sw/run`

两种模式，二选一。

**模式 A：填模板（确定性，不调 LLM）**

```bash
curl -X POST -H "Authorization: Bearer sk-sw-xxx" -H "Content-Type: application/json" \
  -d '{"project":"惠民社区保障资金项目","activity":"11-共治行动-第一期",
       "template":"活动目录","data":{},"dry_run":false}' \
  https://<host>/sw-api/api/sw/run
```

**模式 B：LLM 生成文字再填**

```bash
curl -X POST -H "Authorization: Bearer sk-sw-xxx" -H "Content-Type: application/json" \
  -d '{"project":"惠民社区保障资金项目","activity":"11-共治行动-第一期",
       "task":"brief","style":"务实"}' \
  https://<host>/sw-api/api/sw/run
```

**参数**：

| 参数 | 说明 |
|---|---|
| `project` | 项目名（必填） |
| `activity` | 活动目录名（必填） |
| `template` | 模板名，如 `活动目录`、`决算表`、`简报` |
| `task` | LLM 任务：`brief`（简报）/`plan`（计划书）/`summary`（总结报告）/`supervision`（监督记录） |
| `style` | 文风：`务实`（默认）/`平实`/`党建` |
| `data` | 覆盖字段，key 就是占位符名，不带 `{{}}` |
| `output` | 输出文件名，默认 `<模板名>.docx` |
| `dry_run` | `true` 不落盘，只返回预览 |

**返回**：

```json
{"ok":true,"path":"活动/11-共治行动-第一期/活动目录.docx",
 "filled":6,"missing":["归档人"],
 "preview":"共治行动（第一期）管理\n归档目录\n项目编号CDYJR2026008-C-1110002\n...",
 "self_check":{"ok":true,"score":100,"words":704,"rewrites":0}}
```

- `missing`：模板里没被填上的占位符（**不报错，只提示**）
- `self_check`：仅 `task` 模式有，文风自检结果

### 5.2 数据来源

`data` 不传时，engine 自动从项目 yaml + `activity.yaml` 组装：

| 字段 | 来源 |
|---|---|
| 项目名称、项目社工、项目负责人、帐号、开户行 | 项目 yaml |
| 项目编号、活动名称 | `activity.yaml` |
| 归档人 | 当前用户的 `real_name` |
| 年月日、填表日期 | 当天 |

## 6. 批量操作

```bash
curl -X POST -H "Authorization: Bearer sk-sw-xxx" -H "Content-Type: application/json" \
  -d '{"action":"check","project":"惠民社区保障资金项目","activities":[]}' \
  https://<host>/sw-api/api/sw/batch
```

| action | 说明 |
|---|---|
| `check` | 批量 15 项核对 |
| `generate` | 批量按模板生成（`params.template` 指定） |
| `upload` | 批量上传（`params.files` 指定路径列表） |

返回：

```json
{"action":"check","total":17,"ok":3,"failed":0,
 "results":[{"activity":"01-走访调研-第一期","status":"ok","detail":{"ok_count":0,"missing_count":14}}]}
```

## 7. 异步任务

```bash
curl -H "Authorization: Bearer sk-sw-xxx" https://<host>/sw-api/api/sw/tasks/{task_id}
```

当前为桩实现，返回 `{"id":"...","status":"done","result":{}}`。
飞书同步、Cognee 入库等长任务后续接入。

## 8. 典型工作流

### 8.1 办完一场活动，生成全部材料

```bash
# 1. 看活动现状
curl -H "Authorization: Bearer sk-sw-xxx" \
  "$BASE/api/sw/projects/惠民社区保障资金项目/activities/11-共治行动-第一期"

# 2. 生成简报
curl -X POST -H "Authorization: Bearer sk-sw-xxx" -H "Content-Type: application/json" \
  -d '{"project":"惠民社区保障资金项目","activity":"11-共治行动-第一期","task":"brief","style":"务实"}' \
  "$BASE/api/sw/run"

# 3. 生成总结报告
curl -X POST ... -d '{"...":"...","task":"summary","style":"务实"}' "$BASE/api/sw/run"

# 4. 生成活动目录（填模板）
curl -X POST ... -d '{"...":"...","template":"活动目录"}' "$BASE/api/sw/run"

# 5. 核对 15 项
curl -H "Authorization: Bearer sk-sw-xxx" \
  "$BASE/api/sw/projects/惠民社区保障资金项目/activities/11-共治行动-第一期"
```

### 8.2 新项目立项

```bash
# 1. 建项目目录
curl -X POST -H "x-token: <管理员>" -H "Content-Type: application/json" \
  -d '{"name":"项目名","short":"项目名","code_rules":{"establish_month":8}}' \
  "$BASE/api/projects"

# 2. 按指标建活动目录
curl -X POST -H "x-token: <管理员>" -H "Content-Type: application/json" \
  -d '{"dry_run":false}' "$BASE/api/projects/项目名/activities/split"
```

## 9. 运维

```bash
# 部署（改完代码必跑）
cd /home/ubuntu/DSH/social-worker && ./deploy.sh

# 预演
./deploy.sh --dry-run

# 测试
python3 -m pytest tests/ -q
```

**注意**：仓库 `/home/ubuntu/DSH/social-worker` 是开发目录，生产服务跑 `/srv/social-work` 副本。
**改代码不同步就没生效**——必须跑 `deploy.sh`。

## 10. 已知限制

| 限制 | 说明 |
|---|---|
| 活动目录内无删除 API | 只能删单个文件（移 `.trash`），不能删目录 |
| `tasks` 是桩 | 无真实异步任务 |
| 旧项目键歧义 | `huadu` 可能指激励或保障，读旧台账时按列表顺序落到保障资金 |
| 满意度分数需人工录 | 手写体不 OCR，Agent 可读写 `scores.json` |
