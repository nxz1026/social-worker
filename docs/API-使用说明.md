# 社工工作台 API 使用文档

> 面向客户端 Agent 的接口说明。Web 管理界面用另一套 `/api/*`，本文只讲 Agent 用的 `/api/sw/*`。
> 所有示例均为实跑验证过的输出。

## 1. 网络接入

### 1.1 地址与端口

| 项 | 值 |
|---|---|
| 协议 | **HTTPS**（TLS 1.2/1.3） |
| 主机 | `<host>`（公网 IP，无域名） |
| HTTPS 端口 | **443** |
| HTTP 端口 | **80**（仅做重定向，不建议 Agent 使用） |
| API Base URL | `https://<host>/sw-api` |
| Web 管理界面 | `https://<host>/sw/` |
| 后端上游 | `127.0.0.1:8001`（仅本机，**不对外暴露**） |

**链路**：`客户端 → nginx :443 → 反代 127.0.0.1:8001`

**`<host>` 是什么**：本文档用占位符代替真实地址（仓库有密钥扫描规则，禁止提交真实 IP）。
实际部署时查一下当前地址：

```bash
# 在服务器上查公网 IP
curl -s ifconfig.me
# 或查 nginx 监听
ss -ltnp | grep ':443'
# 或直接看服务端本地配置
cat /srv/social-work/config/server_endpoint.txt
```

### 1.2 证书（重要）

服务器用的是**自签证书**，不是公共 CA 签发：

```
subject = CN=<host>
issuer  = CN=<host>   （自签，无中间 CA）
有效期  = 2026-09-14 ~ 2036-09-11
```

**客户端必须处理证书校验**，否则所有请求报 `SSL: CERTIFICATE_VERIFY_FAILED`。三种处理方式：

| 方式 | 适用 | 命令 |
|---|---|---|
| 信任自签证书 | **推荐** | 把服务器证书导出后加入客户端信任库 |
| 跳过校验 | 仅内网/自用 | `curl -k`、Python `verify=False`、Node `rejectUnauthorized: false` |
| 指定证书文件 | 最稳 | `curl --cacert <证书路径>` |

导出服务器证书：

```bash
openssl s_client -connect <host>:443 </dev/null 2>/dev/null \
  | openssl x509 > sw.crt
curl --cacert sw.crt https://<host>/sw-api/api/sw/capabilities
```

### 1.3 通用请求头

| 头 | 值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer sk-sw-xxx` | API Key 认证（**首选**） |
| `x-api-key` | `sk-sw-xxx` | 同上，二选一 |
| `x-token` | `<session>` | Web 登录态，管理操作用 |
| `Content-Type` | `application/json` | 除文件上传外 |
| `Content-Type` | `multipart/form-data` | 文件上传时 |

### 1.4 文件上传限制

三条上传链路的上限不同，详见 **§11.7**。速查：

| 链路 | 单文件 | 单次 | 条数 |
|---|---|---|---|
| 活动文件 `POST /api/projects/{p}/activities/{a}/upload` | 20 MB | — | — |
| 照片 `POST /api/sw/photos/upload` | 20 MB | 120 MB | 60 张 |
| 压缩包 `POST /api/sw/photos/upload-archive` | 500 MB | 解压后 2 GB | 5000 条 |

活动文件允许扩展名：`.docx .xlsx .doc .xls .pdf .jpg .jpeg .png .gif .bmp .webp .txt .md .zip .7z`
nginx `client_max_body_size` 是 100 MB，API 层更严，超限由 API 回 413。

### 1.5 超时

| 项 | 值 |
|---|---|
| nginx 读超时 | 600 秒 |
| LLM 调用超时 | 240 秒（`/api/sw/run` 带 `task` 时可能接近该值） |
| 建议 Agent 客户端超时 | ≥ 300 秒 |

### 1.6 Base URL 速查

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
| `NEED_FORCE` | 409 | 目标文件已存在且可能被人工改过，需 `force=true` |
| `DUPLICATE_NAME` | 409 | 同名文件已存在（项目资料上传） |
| `FILE_EXISTS` | 409 | 目标路径已有同名文件（/api/sw/run 生成时） |
| `KEY_NOT_FOUND` | 404 | 要吊销的 API Key 不存在 |
| `DUPLICATE_BATCH` | 409 | 24 小时内同活动+同日期+同场景已传过（照片） |
| `TOO_MANY_FILES` | 400 | 单请求超 60 张（照片，§11.7） |
| `FILE_TOO_LARGE` | 413 | 单文件超限 |
| `PAYLOAD_TOO_LARGE` | 413 | 整批上传总量超限（照片 500 MB / zip 1 GB） |
| `IMAGE_TOO_LARGE` | 400 | 单图像素超过 8000 万（防解压炸弹） |
| `NOT_AN_IMAGE` | 400 | 扩展名不是图片，或图片无法解析 |
| `ARCHIVE_UNSUPPORTED` | 400 | 压缩格式不支持（只收 zip） |
| `ARCHIVE_EMPTY` | 400 | 压缩包里没有图片 |
| `ARCHIVE_TOO_MANY_ENTRIES` | 400 | zip 条目数超过 5000 |
| `ARCHIVE_TOO_LARGE` | 400 | zip 解压后总量超过 2 GB |
| `ARCHIVE_SUSPICIOUS` | 400 | zip 单条压缩比异常（疑似压缩炸弹） |
| `NO_MATCHED_ACTIVITY` | 400 | 压缩包内目录匹配不上任何活动 |
| `TOO_MANY_ATTEMPTS` | 429 | 登录失败过多（15 分钟内同账号 + IP 失败 10 次） |
| `TASKS_NOT_IMPLEMENTED` | 501 | 异步任务端点尚未接入 |

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
          "effective_ok_count":14,"signed_absent_count":0,
          "items":[{"no":1,"name":"活动计划","status":"ok","detail":"命中 01-...docx"}]}}
```

**15 项核对状态**：`ok` 齐 / `partial` 待补（如只有未签字 docx）/ `missing` 缺 / `na` 不适用。
`complete` 要求 `partial_count == 0` 且 `missing_count == 0`。

核对结果里每项还带 `review`（谁核对 / 何时 / 备注，未核对为 `null`）与 `signed`；
顶层带：

| 字段 | 含义 |
|---|---|
| `required_count` | 必需项数（`status != na` 的项） |
| `reviewed_required_count` | **必需项里**已核对的项数 |
| `reviewed_count` | 所有已核对项数（含 na 项，仅供统计） |
| `signed_count` | 已签字项数 |
| `deliverable` | 必需项全部**齐备**（含签字确认的）即为 `true`，与 `complete` 同源 |
| `pending_upload_count` | 已人工签字确认、但文件还没上传的项数 |
| `effective_ok_count` / `effective_partial_count` / `effective_missing_count` | 叠加人工签字后的**有效**计数 |
| `signed_absent_count` | 物理缺失但已签字的项数 |

### 物理计数 vs 有效计数（2026-11-24 起）

`ok_count` / `partial_count` / `missing_count` **永远是物理事实**——文件到底在不在。

签字只影响 `effective_*` 与 `complete`：

| 场景 | `ok_count` | `effective_ok_count` | `complete` |
|---|---|---|---|
| 文件在 | +1 | +1 | — |
| 文件缺、已签字 | 不变 | +1 | 可完成 |
| 文件缺、未签字 | 不变 | 不变 | 不能完成 |

实测 惠民01-走访调研：物理 `ok=5 missing=8`，有效 `ok=13 missing=1`，
`signed_absent_count=7`。**这份活动有 7 项没有文件、只靠签字标记**，界面用
橙色「签字补位 7」标出，不给「完整」二字盖过去。

历史上曾把 `ok_count` 直接改成有效值，导致物理缺 8 项的活动显示「齐 14 / 缺 0」，
对甲方交付判断是失真。

`deliverable` 与 `complete` **同源**（都基于 `_effective_status == ok`）。
曾出现两者判据不同（一个把 `partial` 算齐备、另一个不算），
导致 `complete=False` 却 `deliverable=True` 的自相矛盾。

**`reviewed_required_count` 只数必需项**（`na` 项即使有核对记录也不计入）——
否则「13/14 必需项核对 + na 项凑数」会错位（审计 R-03）。

### 4.3.1 标记核对 / 签字

```bash
curl -X POST -H "x-token: $TOK" -H "Content-Type: application/json" \
  -d '{"item":1,"note":"金额已核对","signed":false,"scan":""}' \
  "https://<host>/sw-api/api/projects/惠民社区保障资金项目/activities/11-共治行动-第一期/review"
```

- `item`：材料序号 1–15；`note` 备注可选；`signed` 是否已签字；`scan` 可选，
  传活动目录内已存在的图片/PDF（不传表示人工确认、扫描件后补）；`clear: true` 撤销该序号记录。
- 权限：项目 write（admin/editor）。记录写进 `activity.yaml` 的 `reviews` 字段，并记 `audit_log`。

### 4.3.2 回收站：列出与恢复

删除活动内文件时不会真删，而是移到 `<活动目录>/.trash/<时间戳>/<原相对路径>`。

```bash
# 列出回收站
curl -H "x-token: $TOK" \
  "https://<host>/sw-api/api/projects/<p>/activities/<a>/trash"

# 恢复一个文件
curl -X POST -H "x-token: $TOK" -H "Content-Type: application/json" \
  -d '{"trash":".trash/20261121-120000/01-计划书.docx"}' \
  "https://<host>/sw-api/api/projects/<p>/activities/<a>/trash/restore"
```

列表返回：

```json
{"items":[{"trash":".trash/20261121-120000/01-计划书.docx",
           "original":"01-计划书.docx","deleted_at":"20261121-120000",
           "size":464913,"mtime":"2026-11-21T20:00:00+08:00"}]}
```

- `trash` 字段可直接回传给恢复接口，不用自己拼路径。
- 恢复时原位置已有同名文件 → **409**，不静默覆盖，需先改名或删除。
- 只能恢复 `.trash/` 下的文件；越界路径（`..`、绝对路径）返回 400。
- 恢复成功后时间戳目录若已空会自动清掉。
- 权限：列出需项目读权限；恢复需项目 write。

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
| `task` | LLM 任务：`brief`（简报）/`plan`（计划书）/`summary`（总结报告）/`supervision`（监督记录）。各 task 的输出字段见 5.2 |
| `style` | 文风：`务实`（默认）/`平实`/`党建` |
| `data` | 覆盖字段，key 就是占位符名，不带 `{{}}` |
| `output` | 输出文件名，默认 `<模板名>.docx` |
| `dry_run` | `true` 不落盘，只返回预览 |
| `force` | `true` 允许覆盖已存在且可能被人工改过的文件（见下） |

**返回**：

```json
{"ok":true,"path":"活动/11-共治行动-第一期/活动目录.docx",
 "filled":6,"missing":["归档人"],
 "preview":"共治行动（第一期）管理\n归档目录\n项目编号CDYJR2026008-C-1110002\n...",
 "self_check":{"ok":true,"score":100,"words":704,"rewrites":0}}
```

- `missing`：模板里没被填上的占位符（**不报错，只提示**）
- `self_check`：仅 `task` 模式有，**文风**自检（用词、字数）
- `delivery`：仅 `task` 模式有，**交付完整性**。以 task 的字段清单为白名单：

```json
"delivery": {
  "complete": true,
  "expected": ["简报导语", "诉求描述", "达到目标"],
  "unfilled": [],
  "other_missing": ["图注_1", "图注_2"]
}
```

| 字段 | 含义 |
|---|---|
| `complete` | 本次 task 该填的是否都填上了 |
| `unfilled` | 该填但没填的（非空即 `complete=false`） |
| `other_missing` | 别的环节负责的占位符（如图注归 caption），**不算失败** |

`self_check.ok` 只查文风，查不出「生成了但没填进模板」。**判交付看 `delivery.complete`。**

### 5.2 覆盖保护（重要）

`render` 直接覆盖目标文件，**没有覆盖就没有了**。为防止 Agent 静默盖掉人工修改过的材料，约定：

| 目标文件 | 活动 `activity.yaml` 的 `status` | 行为 |
|---|---|---|
| 不存在 | 任意 | 直接写 |
| 已存在 | `generated` 或未标注 | 直接写（纯生成件） |
| 已存在 | `reviewed` / `signed` / `edited` / `imported` | **409 `NEED_FORCE`** |

```json
{"error":{"code":"NEED_FORCE",
  "message":"目标文件已存在且活动状态为 reviewed，覆盖会丢失人工修改内容；确认覆盖请传 force=true",
  "hint":"确认无人工修改后重发请求并带上 force=true"}}
```

**调用方处理**：确认要覆盖时重发请求并带 `"force": true`。`dry_run=true` 不受此限制（它写临时目录）。

### 5.3 各 task 的输出字段

**字段名 = 模板占位符名**，所以 `task` 模式必须配 `template`（或用默认映射）：

| task | 默认模板 | 输出字段 |
|---|---|---|
| `brief` | 简报.docx | `简报导语` `诉求描述` `达到目标` |
| `plan` | 计划书.docx | `活动背景内容` `活动目的内容` `活动目标内容` `目标人群` `宣传方式` `筛选方式` `环节`（数组）`预计困难`（数组）`物资`（数组） |
| `summary` | 总结报告.docx | 19 个：`策划目标` `策划问题` `策划行动` `准备目标` `准备问题` `准备行动` `执行目标` `执行内容` `执行表现` `执行挑战` `执行行动` `宣传目标` `宣传成效` `总结目标` `总结挑战` `总结行动` `工作员表现` `工作员反思` `跟进工作` |
| `supervision` | 监督记录.docx | `活动内容` |

**数组字段**（`环节` / `预计困难` / `物资`）由代码展开成带序号的 key：

```json
{"环节": [{"内容": "...", "物资": "...", "人员": "..."}],
 "预计困难": [{"困难": "...", "方案": "..."}],
 "物资": [{"类别": "广告宣传费", "名称": "横幅", "规格": "5米", "单价": 12}]}
```

展开后对应 `{{环节内容_1}}`…`{{环节内容_5}}`、`{{预计困难_1}}`…、`{{物资名称_1}}`…。
**超出模板行数会截断**并在 `missing` 里体现。

### 5.4 数据来源

`data` 不传时，engine 自动从项目 yaml + `activity.yaml` 组装：

| 字段 | 来源 |
|---|---|
| 项目名称、项目社工、项目负责人、帐号、开户行 | 项目 yaml |
| 项目编号、活动名称 | `activity.yaml` |
| 归档人 | 当前用户的 `real_name` |
| 年月日、填表日期 | 当天 |

## 5.5 材料生成与维护（活动级）

以下端点都在 `/api/projects/{p}/activities/{a}/` 下，`{p}`/`{a}` 为 URL 编码的项目名与活动目录名。
除标注外均需项目 write 权限。

| 端点 | 说明 |
|---|---|
| `POST .../fill` | 模板填充：`{"template":"决算表","data":{...}}` → 写盘，返回 `{path,filled,missing}` |
| | **缺字段清空**（2026-11-23）：成品里绝不留 `{{占位符}}`；缺了什么只在 `missing` 里报告 |
| | **默认命名带序号**（2026-11-23）：不给 `output` 时按「序号-模板名」命名，如 `01-计划书.docx` |

**不可生成的模板**（`api/shared/paths.py` 的 `NO_GENERATE`）：

| 模板 | 原因 |
|---|---|
| 决算表 | 需按实际票据人工填写，系统只做校验不生成 |
| 报销单 | 同上 |

`GET /api/templates` 每项返回 `fillable` + `reason`；`POST .../fill`、
`POST .../llm-fill`、`POST /api/sw/run` **三个入口都会 400 拒绝**——
只在界面隐藏不够，API 仍能造出空壳让人拿去归档。

**模板 → 默认文件名**（`api/shared/paths.py` 的 `TEMPLATE_SERIAL`）：

| 模板 | 默认名 | 模板 | 默认名 |
|---|---|---|---|
| 计划书 | `01-计划书.docx` | 决算表 | `08-决算表.docx` |
| 活动通知 | `02-活动通知.docx` | 监督记录 | `09-监督记录.docx` |
| 签到表 | `03-签到表.docx` | 报销单 | `21-报销单.xlsx` |
| 服务对象满意度调查表 | `04-...docx` | 发票明细表 | `22-...xlsx` |
| 满意度测评汇总表 | `05-...docx` | 劳务领取表 | `23-...docx` |
| 总结报告 | `06-总结报告.docx` | 志愿者补贴领取表 | `24-...docx` |
| 简报 | `07-简报.docx` | 物资领取表 | `25-...docx` |
| 支出证明单 | `26-...xlsx` | 对公付款申请单 | `27-...docx` |

活动目录 / 项目管理目录 是归档索引，不带序号。传了 `output` 就完全按它来。
| | **活动通知**（2026-11-23 新增）：11 个占位符自动填，`missing` 为空 |
| | **签到表**（2026-11-23 新增）：5 个占位符（活动名称两处 / 活动编号 / 活动时间 / 项目社工 / 页眉项目名称） |
| `POST .../llm` | LLM 生成，**只返回不落盘**：`{"task":"summary","style":"务实"}`。实测 2–4 分钟 |
| `GET  .../llm-progress` | 生成进度：`{running, stage, percent}`。前端轮询它显示阶段 |
| `GET  .../llm-cache?task=` | 读上次生成结果（留档在 `_llm/<task>.json`）。不传 task 列出全部留档 |
| `POST .../llm-fill` | LLM 生成后合并进模板并落盘 |
| `POST .../captions` | 生成图注；`picked` 为空时程序按文件名挑图 |
| `POST .../captions/fill` | 把图注填进模板 `{{图注_N}}` |
| `POST .../readme` | 重生成单个活动的索引 md；内容没变返回 `written=false` |
| `GET  .../download?path=` | 下载活动内文件；`path` 相对活动目录，越界 → 400 |
| `POST .../invoice/report` | 生成发票明细表，返回 `{path,count,total}` |
| `GET  .../satisfaction` | 读 `scores.json`（读权限即可） |
| `PUT  .../satisfaction` | 写回分数：`{"scores":[{"file","score","satisfied"}]}` |
| `POST .../satisfaction/list` | 扫问卷照片生成/更新 `scores.json`（保留已录分数） |
| `POST .../satisfaction/fill` | 把分数统计填进汇总表模板 |
| `GET  .../photos` | 列照片：文件名/大小/mtime/缩略图/是否程序默认选中 |

### 5.5.1 LLM 端点的两个形态

- `.../llm` 用来**预览**：生成结果直接返回，用户看过再决定落不落盘。
- `.../llm-fill` 直接写盘。两者都用同一个 `engine.llm.write()`。

真实调一次 **100~240 秒**（2026-11-23 实测：生成 ~100s + 文风自检不达标再重写 ~60s），
**调用方超时至少设 300 秒**。

生成期间轮询 `GET .../llm-progress` 拿阶段：

```json
{"running": true, "stage": "rewriting", "percent": 70, "task": "plan"}
```

stage 依次为 `generating`（首次生成，约占 2/3 时间）→ `checking`（自检）→
`rewriting`（不达标重写）→ 完成后 `{"running": false}`。
没有正在跑的任务也是 `{"running": false}`。

### 5.5.2 活动通知模板

`templates/library/活动通知.docx`，11 个占位符：

| 占位符 | 数据来源 |
|---|---|
| `{{项目名称}}` | 项目 yaml `project.name` |
| `{{活动名称}}` | activity.yaml 活动名称 / 目录名 |
| `{{社区名称}}` | `parties.funder.name` 去掉省市区街道前缀 →「惠民社区居民委员会」 |
| `{{活动时间}}` / `{{活动时间_cn}}` | activity.yaml 活动时间；后者转成「2026年11月11日」 |
| `{{活动地点}}` / `{{服务对象}}` | activity.yaml 同名「字段，没填就留空 |
| `{{活动简介}}` / `{{活动简介_suffix}}` | 可选；有内容时 suffix 自动补「，」 |
| `{{项目社工}}` / `{{联系电话}}` / `{{年月日}}` | 项目 yaml / activity.yaml / 当天 |

**社区名与电话是本次新增的数据字段**：`build_data` 之前只读 `parties.executer`，
现在补了 `funder`（社区全称）+ `staff.leader.phone`（项目 yaml 里的电话），
没有 phone 时回退到活动里的「联系电话」。

### 5.5.3 签到表模板

`templates/library/签到表.docx`，正文 24 行 5 列（序号/姓名/联系电话/单位/备注）。

| 占位符 | 出现位置 | 来源 |
|---|---|---|
| `{{活动名称}}` | 标题 + 「活动名称：」两处 | activity.yaml 活动名称 |
| `{{活动时间}}` | 日期/时间栏 | activity.yaml 活动时间 |
| `{{项目社工}}` | 负责社工栏 | activity.yaml 执行社工 / 项目 yaml |
| `{{活动编号}}` | 档案编号行 | activity.yaml `code`（与「项目编号」同一个值，别名） |
| `{{项目名称}}` | 页眉（右对齐） | 项目 yaml `project.name` |

**注意**：页眉原来是 67 个空格做右对齐，项目名长时会顶破版心；
改成段落右对齐（`WD_ALIGN_PARAGRAPH.RIGHT`），`test_template_layout` 的
前导空格与宽度检查才通过。

### 5.5.4 计划书物资（必备内容）

`plan` 任务的 schema 本来就要求输出「物资」列表（类别/名称/规格/单价），
但 `PLAN_ROW_SPECS` 里没有物资项，导致模板的 28 个物资占位符填不上。

2026-11-23 补上：

| 占位符 | 来源 |
|---|---|
| `{{物资类别_1..5}}` / `{{物资名称_N}}` / `{{物资规格_N}}` / `{{物资单价_N}}` | LLM 输出的物资列表 |
| `{{物资小计_N}}` | **代码算**：单价 × 规格开头的数量（不让模型算） |
| `{{环节阶段_N}}` / `{{环节时间_N}}` / `{{环节备注_N}}` | LLM 环节子字段（2026-11-23 补，原来 `lists.环节.item` 白名单只有内容/物资/人员，模型生成的阶段时间会被丢弃） |
| `{{合计金额}}` | 各行小计之和 |

实测：横幅 2条×60 + KT板 4个×80 + 矿泉水 3箱×45 → 小计 120/320/135，
合计 575。

### 5.5.5 字段回读（生成材料不再空壳）

`activity.yaml` 通常只有 `code` / `活动时间` / 执行社工 几个键，
地点、对象、人数、背景、目的都**不在里面**——但在已归档的计划书和总结报告里。

`build_data` 生成前会调 `backfill_from_materials()` 从原件回读：

| 来源 | 回读字段 |
|---|---|
| `01-计划书`（首份） | 活动地点、服务对象、预计人数、活动背景内容、活动目的内容 |
| `06-总结报告` | 实际人数 |

**优先级：`activity.yaml` > 原件回读**，人工填过的值不被覆盖。

标签匹配允许字符间有散空格（真实模板用 `人　　数：` `对　　象：` 全角对齐）。

实测 13-爱邻议事会：回读前四项全空，回读后
`世纪光华物业二楼党群办公室 / 社区议事代表… / 11 / 11`。

### 5.5.6 活动编号：补写 / 自动分配

活动缺 `code` 时所有生成接口返回 400 `CODE_MISSING`。三个修编号的入口：

```http
PATCH /api/projects/{name}/activities/{dirname}/code
      {"code": "CDYJR2026008-C-1111004"}      # 显式指定
      {"auto": true}                          # 按项目规则自动生成
POST  /api/projects/{name}/activities/backfill-code
      {"dry_run": true}                       # 预演；false 才真写
```

`backfill-code` 扫本项目下所有缺 code 的活动，按编号规则批量补齐，
预演模式返回 `would_fill` 列表。

**`PATCH .../activities/{dirname}` 是重命名端点**（`new_name` 必填），
不能用来改编号——这是 2026-11-24 客户端踩的坑，已把 `CODE_MISSING`
的 hint 改指向 `/code`。

**新建活动自动带编号**：`POST .../activities/split` 建目录时会写最小
`activity.yaml`（含 code + `status: created`），不再产出无编号活动。
编号生成失败不阻断建目录，可事后用 `/code` 补。

**`act_year` 口径**：编号格式只存活动年**个位**（`act_year % 10`），
所以 `parsed.act_year` 是 `6` 不是 `2026`——这是格式约束不是截断。
API 另返回 `act_year_full`（完整年份）与 `act_year_note` 说明。
传 `2026` / `26` / `0`（取当前年）三种写法都接受。

### 5.5.6b 新建活动目录（开放的建目录 API）

```http
POST /api/projects/{name}/activities
{"dirname": "12-安全隐患专项排查-七彩花都B1",
 "code": "CDYJR2026005-C-16010017",          // 可选，不给就自动分配
 "fields": {"活动时间": "2026-12-01 下午"}}   // 可选，写进 activity.yaml
```

**命名由服务端强校验**，不合规直接 400、不建目录：

| 规则 | 要求 | 违反 |
|---|---|---|
| 三段式 | `<两位序号>-<活动名>-<期次或小区>` | 400「格式不符」 |
| 序号 | 恰好两位数字（`12-` 可以，`1-` 不行） | 400 |
| 活动名 / 期次段 | 不能为空 | 400 |
| 路径安全 | 不含 `/` `\` `..` | 400 |
| 非法字符 | 不含 `:*?"<>\|` 与制表换行 | 400 |
| 长度 | ≤ 80 字符 | 400「过长」 |

**软性告警**（返回 `warnings`，不拒绝）：

- 去掉序号后与已有目录重名 → 「同活动已存在：…」（方案 A 允许换序号重建）
- 序号已被其它目录使用 → 「序号 12 已被 N 个目录使用」

返回 `{ok, dirname, code, warnings, activity_yaml}`。目录已存在返回 **409**。
`PATCH .../activities/{dirname}`（改名）同样受这套命名规则约束。

**期数与小区**：在项目 yaml 的指标里登记 `count` 与可选 `periods`
（每期 `{period, community, placeholder}`），`POST .../activities/split`
即可按小区名批量生成目录名，不再只产出「第N期」。

### 5.5.6c `POST .../activities/split` 的匹配规则（方案 A）

**按「去掉序号后的活动名」配对，序号不参与比对。**

```http
POST /api/projects/{name}/activities/split
{"dry_run": true}      // 默认 true，只预演
```

返回：

| 字段 | 含义 |
|---|---|
| `would_create` | 计划里有、磁盘上没有的目录名 |
| `would_skip` | 计划里有、磁盘上已存在（活动名对上）的目录名 |
| `extra_on_disk` | 磁盘上有、指标里没登记的人工活动目录 |

**为什么忽略序号**：已有目录的序号是历史遗留，与按指标顺序算出的序号未必一致。
同一个活动可能叫 `12-安全隐患专项排查-七彩花都B1`（手建）或
`13-安全隐患专项排查-七彩花都B1`（按序算出）。比全名会把已有活动判成新的、
重复建目录——实测曾一次建议新建 10 个，其中 7 个已存在。

**用精确匹配不用模糊包含**：`爱邻议事会-第一期` 与 `爱邻议事会-第十期`
必须是两个活动，包含匹配会让它们互相误配。

对应实现：`engine.activities.activity_key()` 去序号、
`match_existing()` 配对，split 端点两处（预演与执行）都走它。

### 5.5.7 三表校验（决算 / 报销 / 发票）

```http
POST /api/projects/{name}/activities/{dirname}/invoice/scan    # 校验并返回结果
POST /api/projects/{name}/activities/{dirname}/invoice/report  # 写明细表
```

三份材料交叉比对，返回中文键：

| 字段 | 含义 |
|---|---|
| `决算` / `报销` / `发票` | `{value, source, count}`，各自合计金额与来源 |
| `diff` | `{"决算-报销": 0.0, "报销-发票": 260.9}` |
| `amount_consistent` | 三表金额是否两两相等 |
| `evidence_valid` | 票据证据是否可信（格式/金额冲突） |
| `issues` | 中文问题清单，直接展示给用户 |

**支持的票据格式**（2026-11-24 补）：

| 类型 | 命名示例 | 说明 |
|---|---|---|
| 标准电子发票 | `2026-11-13_电子发票_160元_听澜集益_茶饮服务_26512000005052287086` | 20 位票号 |
| 定额发票 | `12-1-20261104_联园水果店_水果收据及定额发票01786710_100.00元_...` | 8 位票号 |
| 收据 / 采购订单 | `12-3-20261105_..._毛线采购订单_恒源祥_37.90元` | 无票号也认 |

日期支持三种写法：`2026-11-04` / `20261104` / `12-1-20261104`（归档序号前缀）。
**有日期 + 金额即可提取**，票号缺失不阻断。

**决算表 `.doc` 也支持**：真实数据约四成是老 `.doc`，走 soffice 转换后解析
（原先只认 `.docx`，这些活动的三表校验永远 `amount_consistent=False`）。

**金额大写校验**：报销单若有「人民币大写」栏，按合计金额转大写比对，
不一致写进 `issues`。实测 13-爱邻议事会：合计 340.90，大写写
「叁佰肆拾元玖角肆分」(340.94)，差 0.04 被检出。

### 5.5.8 自动整理（材料归位）

```http
GET  /api/projects/{name}/activities/{dirname}/tidy   # 出方案（只读）
POST /api/projects/{name}/activities/{dirname}/tidy   # 执行 {"actions":[...]}
```

判定复用 `engine.checks` 的核对结果——「核对认为该在哪」就是「整理该搬到哪」：

| 情况 | 动作 |
|---|---|
| 根目录 + 规范名 | 不动 |
| 文件名对得上但缺序号前缀 | 改名到根目录规范名 |
| 放在子目录里 | 移到根目录并改名 |
| 已带补充编号（21-/25-…） | **不动**，列进 skipped |
| 目标名被占用 | 不覆盖，加 `-2` 后缀 |

只搬动和改名，**不改内容**；同名 `.md` 骨架跟着搬。执行前必定先出方案给用户确认。

**三种不搬的情况**（2026-11-24 补，前两种原来会破坏数据）：

| 情况 | 原因 |
|---|---|
| 已在 `03-签到表/` 这类**编号目录**内 | 多页扫描件放目录里是正常做法，文件不必再带序号 |
| 同目录有 **≥2 份同类扫描件**（成套件） | 单搬一份会把整套拆散（实测 10 份问卷只搬第 7 份） |
| 已带补充编号 `21-`~`27-` | 那是补充编号，本身就规范 |

补齐后全仓 45 个活动的待整理动作数为 **0**——文件原本就在合理位置。

实测：`11-活动照片资料/签到表.pdf` → `03-签到表.pdf`（`.md` 一并搬）。

### 5.5.9 LLM 冷化（项目级隔离）

`/llm` 与 `/llm-fill` 生成前自动取事实注入 prompt，避免模型凭空编内容：

| 来源 | 路径 | 上限 |
|---|---|---|
| 项目书/协议/报告 | `projects/<项目>/{方案,协议,报告}/*.docx\|md` | 2500 字 |
| 知识库 | Cognee 数据集（**数据集名 = 项目名**，不跨项目） | 2000 字 |

**只读项目级目录，不读 `活动/`** —— 单场材料不该混进产出的依据。
两个来源都失败时返回空，生成照常进行（冷化是增强，不阻塞主流程）。
返回体带 `grounding` 字段说明用了多少材料。

### 5.5.10 生成入口已合并

「生成材料」页内的**「使用 LLM 生成内容」勾选框**取代了原来的独立「LLM 生成」按钮：

| 模板 | 勾选框 |
|---|---|
| 计划书 / 简报 / 总结报告 / 监督记录 | 默认勾选且**置灰不可取消**（共 4 个 task：plan / brief / summary / supervision） |
| 其余 16 个模板 | 可自由勾选 |

勾选 → 必然走 LLM。改过字段时先读 `_llm` 留档补 LLM 内容段（秒出），
没有留档才真调 LLM。不勾选 → 纯数据填充。

**`LLM_TASK_BY_TPL` 必须与后端 task 一一对应**：漏了哪个模板，它会被
`|| 'plan'` 静默降级成计划书任务，生成一堆填不进模板的字段。

### 5.5.11 文件预览（Web 界面）

活动页点文件名 → 弹窗预览；点行内「下载」按钮才下载。

| 扩展名 | 行为 |
|---|---|
| jpg/jpeg/png/gif/bmp/webp | `<img>` 预览 |
| pdf | `<iframe>` 预览 |
| md / markdown | 轻量渲染（标题/列表/代码块） |
| txt | `<pre>` 原样 |
| docx / xlsx / doc / xls / ppt | **服务端 LibreOffice 转 PDF 预览**（版式 100% 保真） |
| zip / rar / 7z | 不预览，直接下载 |

实现复用 `fetch` + `x-token` + `URL.createObjectURL`，**前端零第三方依赖**
（不引入 mammoth/SheetJS/pdf.js，md 渲染自写），关闭时 `revokeObjectURL`。

Office 预览走 `GET .../preview`：服务端用 LibreOffice 转 PDF，浏览器 iframe
原生显示。选这个方案而不是前端库，是因为：版式完全一致（mammoth 会丢页眉
页脚/分栏/编号）、新增体积为 0、Excel 合并单元格与公式都保留。

实测：455KB docx → 4 页 PDF **1.0s**（缓存命中 0.03s）；xlsx → 1 页 0.7s；
独立 profile 并发 3 个 1.8s 内完成。

转好的 PDF 缓存到活动目录 `.preview/`，键含 `(mtime, size)`，源文件一改自动失效；
每次转换顺带清理旧缓存（保留最近 20 个）。

### 5.5.12 活动文件列表的显示规则

活动页的文件列表（`GET .../files` 的渲染）会过滤三类条目：

| 过滤 | 依据 | 原因 |
|---|---|---|
| md 骨架 | 后端在每个 office 文件上带 `mirror=<对应 md 名>`，被引用的 md 隐藏 | md 是给 Obsidian 与关系图谱用的，和 docx/xlsx 混在一起太乱 |
| 内部元数据 | `activity.yaml` / `_photos.json` / `scores.json` | 不是给用户看的 |
| 生成的索引 | `_X-资料索引.md` / `_X-项目资料索引.md` | 自动生成的目录清单，只有文件名和核对状态 |
| 目录清单 | `活动目录.docx` / `项目管理目录.docx` | 同上 |
| 财务凭据 | 文件名含「支出证明」 | 纯金额与单位名，进图谱只制造噪音 |
| 隐藏目录 | `.trash/` `.preview/` 等 `.` 开头路径 | 回收站与预览缓存，含同名副本 |

判定收敛在 `engine/readme.py::is_generated_nav()` **一处**，
索引脚本引用它——新增生成物只改那边，不必维护第二份规则。
`tests/test_index_exclusion.py` 有一条
`test_readme_filename_still_matches_marker`，改命名规则时会失败提醒同步。

**`.md` 去重**：同目录有同名 `.docx/.doc/.xlsx/.pdf` 时跳过 `.md`
（Obsidian 骨架，内容由原件代表）；只有 `.md` 没有原件时才索引。

**只有「有对应 office 文件的 md」被隐藏**；纯 md（无同名 docx/xlsx）照常显示。
office 文件名后面的 `↗ md` 徽标仍保留，鼠标悬停能看到对应的 md 名。

### 5.5.13 侧边栏导航（菜单-子菜单）

项目做成**可折叠子菜单**：3 个项目 × 2 个子项平铺太乱。

| 行为 | 说明 |
|---|---|
| 默认 | 全部收起，只显示项目名 + `▸` |
| 点击 `▸/▾` | 展开/收起该项目的「概览 / 活动列表」 |
| 折叠状态 | 存 `localStorage['sw_nav_fold']`，刷新后保持 |
| 进入项目页 | 该项目**强制展开**（不管折叠状态），离开后恢复用户设置 |

侧边栏渲染在 `route()` 之前，所以当前项目的展开判断放在 `updateActiveNav()`
里补一次 —— 那里 hash 已就绪。

### 5.5.14 占位符清理（所有生成的文档通用）

`fill.render` 的 `_substitute` 对**找不到值的占位符**一律替换成空串，
不再原样留在成品里。docx / xlsx 共用这段逻辑，所以对全部生成文档生效。

| 情况 | 成品 | `missing` |
|---|---|---|
| 字段有值 | 写入值 | 不含该字段 |
| 字段缺失 | 清空 | 含该字段名 |
| 字段值为 `None` / 空串 | 清空 | 不含（视为「显式清空」） |

docx 额外做一步：**填完变空的顶层段落直接删除**（例如整行只有一个
`{{环节备注_1}}` 而它没值）。只删「本来有占位符、替换后空了」的段落；
模板自带空行、表格单元格里的空段落、含图片/分页符的段落都不动。

实测：计划书模板用 `{'活动名称': '测试活动'}` 渲染 → 原本残留 80 个占位符，
现在 0 个，段落 31→28；真实活动数据 filled=73 时残留同样为 0，
`志愿者人数：` 标签保留、值清空。xlsx 同样 0 残留。

## 6. 照片上传

**完整约定见 [照片上传协议](照片上传协议.md)。** 本节只列端点契约。

核心：**一次上传 = 一个批次 = 一个活动**，归属由调用方指定；服务端不读 EXIF、不 OCR、不做图片识别。

### 6.1 哈希预检

```
POST /api/sw/photos/precheck
```

```json
{"project":"...","activity":"...","hashes":["9f86d0...","3c59dc..."]}
```

```json
{"new":62,"duplicated":["9f86d0..."],"duplicate_batch":null}
```

只比对不落盘。**建议先预检再上传**，避免传完才发现全是重复。

### 6.2 批量上传

```
POST /api/sw/photos/upload
Content-Type: multipart/form-data
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `project` | ✅ | 项目名 |
| `activity` | ✅ | 活动目录名 |
| `shot_date` | ✅ | 拍摄日期 `YYYY-MM-DD`，不得晚于今天 |
| `scene` | 可选 | 场景，如「主会场」 |
| `files` | ✅ | 同名字段重复 N 次，**最多 60 张，单张 ≤20MB，整批 ≤120MB**（§11.7） |
| `allow_duplicate` | 可选 | `true` 跳过 L2/L3 去重 |

```json
{"ok":true,"batch_id":"20261120-143025","saved":62,
 "skipped":[{"original":"IMG_001.jpg","reason":"duplicate","existing":"20261110_005.jpg"}],
 "paths":["11-活动照片资料/20261110_062.jpg"]}
```

文件由服务端重命名为 `{shot_date}_{序号:03d}.jpg`，原名记入 `_photos.json`。

**三层去重**：

| 层 | 依据 | 结果 |
|---|---|---|
| L1 | 目标路径同名 | `skipped`，reason=`exists` |
| L2 | **sha256 内容相同** | `skipped`，reason=`duplicate`，带 `existing` |
| L3 | 24h 内同活动+同日期+同场景 | **409 `DUPLICATE_BATCH`** |

### 6.3 压缩包上传

```
POST /api/sw/photos/upload-archive
Content-Type: multipart/form-data
```

| 字段 | 说明 |
|---|---|
| `project` | ✅ |
| `archive` | ✅ `.zip` 文件，**只收 zip** |
| `shot_date` | 可选 |

压缩包**一级目录名即活动目录名**（支持前缀匹配，`11` 可匹配 `11-共治行动-第一期`）。

预算（读取前按 zip 中央目录校验，超限直接 400/413，不解压）：
压缩包 ≤500MB、条目 ≤5000、解压后总量 ≤2GB；单条压缩比 >200 且原始大小 >10MB
视为可疑压缩炸弹，返回 `ARCHIVE_SUSPICIOUS`。

**图片像素闸门同样生效**：逐条读取后、落盘前会做像素检查，
超过 8000 万像素返回 400 `IMAGE_TOO_LARGE`。这项针对「小体积巨图」——
一张 9000×9000 的纯色 PNG 压缩后只有几十 KB，字节预算拦不住，
只能靠像素判定。zip 与直接上传走同一套校验。

```json
{"ok":true,"matched":{"11-共治行动-第一期":42},"unmatched":{"其他照片":8},"total_saved":42}
```

## 7. 项目资料上传

不属于任何活动的项目级文档（中期评估、管理制度、合同等）。

```
POST /api/sw/project-files/upload
Content-Type: multipart/form-data
```

| 字段 | 必填 |
|---|---|
| `project` | ✅ |
| `file` | ✅ 单文件 |

**二元判断**：命中 19 项清单就归到对应项，落在 `报告/`；不命中落 `其他/` 并给改名建议。

```json
{"stored":"报告/花都保障资金中期质效评估报告.docx","size":3450883,
 "matched":{"item":17,"name":"项目中期（结项）自评报告"}}
```

不命中时：

```json
{"stored":"其他/工作总结.docx","size":12,"matched":null,
 "suggestion":{"item":null,"item_name":null,
               "rename_to":"清单项名称-工作总结-20261120.docx",
               "keywords":[],
               "hint":"文件名未命中任何清单项关键词，请按「清单项关键词-内容-日期」命名"}}
```

**不拒绝、不猜、不自动分类。** 文件名（2026-11-20 放宽后第 17 项关键词）：
`自评报告` `中期质效评估` `中期评估` `质效评估` `中期检查` `结项自评`

## 8. 批量操作

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
 "results":[{"activity":"01-走访调研-第一期","status":"ok","detail":{
   "complete":false,"generatable":false,
   "issues":[{"code":"CODE_MISSING","message":"activity.yaml 缺 code，无法生成材料"}]}}]}
```

**`generatable`**：活动有没有编号。缺编号时 `generate` 必然失败，据此可预判。
`issues` 现在也带生成前置条件，不只是 15 项缺失。

## 9. 异步任务

```bash
curl -H "Authorization: Bearer sk-sw-xxx" https://<host>/sw-api/api/sw/tasks/{task_id}
```

**当前未实现，固定返回 501 `TASKS_NOT_IMPLEMENTED`。** 本机所有操作（生成、上传、
批量）都是同步完成，不需要轮询任务状态；此端点只为将来接入飞书同步、Cognee 入库
等长任务保留。

```json
{"error":{"code":"TASKS_NOT_IMPLEMENTED","message":"异步任务尚未接入",
          "hint":"本机所有操作都是同步完成，无需轮询；此端点保留给未来长任务"}}
```

## 10. 典型工作流

### 10.1 办完一场活动，生成全部材料

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

### 10.2 新项目立项

```bash
# 1. 建项目目录
curl -X POST -H "x-token: <管理员>" -H "Content-Type: application/json" \
  -d '{"name":"项目名","short":"项目名","code_rules":{"establish_month":8}}' \
  "$BASE/api/projects"

# 2. 按指标建活动目录
curl -X POST -H "x-token: <管理员>" -H "Content-Type: application/json" \
  -d '{"dry_run":false}' "$BASE/api/projects/项目名/activities/split"
```

## 11. 运维

```bash
# 部署（改完代码必跑）
cd /home/ubuntu/DSH/social-worker && ./deploy.sh

# 预演
./deploy.sh --dry-run

# 测试
python3 -m pytest tests/ -q

# 资产验收（部署机必跑；模板/项目数据不入库，跳过不算通过）
python3 scripts/verify_assets.py
```

`deploy.sh` 已内置两道门禁：路由数 ≥30、资产验收无跳过。
资产类测试在干净检出环境会因为缺模板/样本而 skip，`pytest` 仍显示通过；
`verify_assets.py` 把跳过当失败，避免「全绿」假象。详见
[服务器部署](服务器部署.md) 的「为什么资产验收要单独一道」。

**注意**：仓库 `/home/ubuntu/DSH/social-worker` 是开发目录，生产服务跑 `/srv/social-work` 副本。
**改代码不同步就没生效**——必须跑 `deploy.sh`。

## 11.5 管理与系统端点

### 11.5.1 认证

| 端点 | 说明 |
|---|---|
| `POST /api/login` | `{"username","password"}` → `{token, user}`。连续 11 次失败 → 429 |
| `POST /api/logout` | 吊销当前 session |
| `GET  /api/me` | 当前用户与项目权限 |

### 11.5.2 用户管理（仅 admin）

| 端点 | 说明 |
|---|---|
| `GET  /api/admin/users` | 用户列表（含 `real_name`、`projects` 字典） |
| `POST /api/admin/users` | 建用户：`{username,password,role,real_name,projects}` |
| `PATCH /api/admin/users/{uid}` | 改 role / 项目 / 启用状态 |
| `DELETE /api/admin/users/{uid}` | 删用户 |
| `DELETE /api/admin/users/{uid}/sessions` | 踢掉该用户全部会话 |
| `GET  /api/admin/audit` | 审计日志查询 |

**API Key 不能访问 `/api/admin/*`**（403，错误信息含「API Key」），必须用 session。

### 11.5.3 文档归档（/api/docs）

| 端点 | 说明 |
|---|---|
| `GET  /api/docs` | 文档列表，可按项目/类别/关键词过滤 |
| `POST /api/docs` | 上传归档（multipart）。同内容幂等返回原 token；**跨项目不泄露** |
| `GET  /api/docs/{token}` | 文档详情与 Markdown 正文 |
| `PATCH /api/docs/{token}` | 改分类 / 项目 / 备注 |
| `DELETE /api/docs/{token}?purge=true` | 下架（默认进回收站）或彻底删除 |
| `GET  /api/docs/{token}/source` | 下载原件 |
| `GET  /api/docs/health` | 各项目归档健康度 |

**归档是同步执行**：上传后直接返回最终 `status`（`done` / `pending`）。
大文件要走飞书上传，实测 30KB 约 10 秒、7MB 约 140 秒，**客户端超时建议 300 秒**。

### 11.5.5 仪表盘的完整性口径

`GET /api/dashboard` 的 `activity_complete` 与活动页 `/check` 使用**同一个口径**
（`merge_review_status` 之后的结果），即签字确认过的项按齐备计入。

如果两处不一致，说明有一方漏了合并 reviews —— 2026-11-23 仪表盘就漏过，
导致活动页显示完整、仪表盘「已完整活动」恒为 0。

### 11.5.4 系统维护（仅 admin）

| 端点 | 说明 |
|---|---|
| `GET  /api/dashboard` | 仪表盘：项目/活动数、完整性、最近活动 |
| `GET  /api/templates` | 模板清单（名称 / 类型 / 占位符） |
| `POST /api/readme/refresh` | 全量重写所有活动索引 md。**有全局副作用**，耗时长 |
| `POST /api/sync/feishu` | 启动飞书镜像后台任务，返回 `{started:true}` |
| `POST /api/cognee/index` | 启动 Cognee 入库后台任务，返回 `{started:true}` |
| `GET  /api/cognee/datasets` | Cognee 数据集列表 |

后三个是**后台任务**：端点只受理并返回 `started`，进度与结果看服务日志。

## 11.6 删除文件与回收站（Agent API）

```http
DELETE /api/sw/projects/{p}/activities/{a}/files
{"path": "09-监督记录.docx", "confirm": "DELETE"}
```

| 检查 | 行为 |
|---|---|
| `confirm` 不是 `DELETE` | 400 CONFIRM_REQUIRED（防误删） |
| 无写权限 / 只读 API Key | 403 |
| 文件不存在 | 404 |
| 路径在 `.trash/` 里 | 400（回收站文件不能再删） |

成功后文件移到 `.trash/<时间戳>/<原相对路径>`，**可恢复**：

```http
GET  /api/sw/projects/{p}/activities/{a}/trash     # 列回收站
POST /api/projects/{name}/activities/{dirname}/trash/restore  # 恢复
```

**API Key 权限（2026-11-23 修复）**：`api_keys.scopes` 列此前从未被读取，
标了 `readonly` 的 key 实际拥有 owner 的全部权限。现在 `scopes` 含
`readonly` 的 key 一切写操作与删除都返回 403；**空 scopes 保持继承 owner**
（历史行为不变）。现有 2 个活跃 key 均为空 scopes，不受影响。

## 11.7 上传限制（2026-11-23 收紧）

| 接口 | 单文件 | 单次总量 | 条数 | 其他 |
|---|---|---|---|---|
| 活动文件上传 | **20MB**（原 50） | — | — | 同名 409、sha256 |
| 照片上传 | **20MB**（原 50） | **120MB**（原 500） | **60 张**（原 200） | 像素 8000 万 |
| zip 上传 | **500MB**（原 1GB） | 解压后 2GB | 5000 条 | 压缩比 200x |

收紧理由（2026-11-23）：项目方明确「那么多图片不上传」，一次活动实际照片
在 50 张上下；单次几千张散图不是支持场景，超了应先筛选或打 zip 走归档。
单文件 20MB 对活动材料足够——实测最大的计划书/总结报告在 600KB 量级，
扫描件 PDF 也远小于 20MB。

超限响应：API 回 `413`（`FILE_TOO_LARGE` / `PAYLOAD_TOO_LARGE` /
`TOO_MANY_FILES`），不会留半截文件。

## 12. 已知限制

| 限制 | 说明 |
|---|---|
| 删除只到文件级 | 可删单个文件（软删到 `.trash`，见 §11.6）；**不能删整个活动目录** |
| `tasks` 端点返回 501 | 无真实异步任务，所有操作同步完成 |
| 照片只收 zip | `.7z`/`.rar` 需客户端先解压 |
| 照片原图不保留 | 压到 2560px q95（约 1/3 体积） |
| 旧项目键歧义 | `huadu` 可能指激励或保障，读旧台账时按列表顺序落到保障资金 |
| 满意度分数需人工录 | 手写体不 OCR，Agent 可读写 `scores.json` |

## 13. Agent 常见误判（实测踩过的）

| 误判 | 实际情况 |
|---|---|
| 以为 `PATCH .../activities/{dirname}` 能改编号 | 那是**重命名**端点，`new_name` 必填。改编号用 `PATCH .../code`（§5.5.6） |
| 以为 `ok_count` 是"有效齐备数" | `ok_count` 是**物理**计数；签字补位的项只在 `effective_ok_count` 里体现（§4.3） |
| 以为 `deliverable=false` 说明缺材料 | 它只表示必需项没全齐；核对进度看 `reviewed_required_count` |
| 以为决算表/报销单能生成 | 两者 `fillable=false`，三个生成入口都返回 400（§5.5.10） |
| 以为 `act_year` 是四位数 | 编号只存活动年**个位**（`6` 不是 `2026`）；完整年份看 `act_year_full` |
| 以为 `其他/` 目录的资料搜不到 | 已入 Cognee（§5.5.9 的数据源含项目级 `其他/`） |
| 用 `GET` 调三表校验 | 是 `POST .../invoice/scan`，`GET .../invoice` 不存在 |
| 以为 `.doc` 不支持 | 决算表/票据都支持 `.doc`（走 soffice 转换，§5.5.7） |
| 以为索引页会进知识库 | `_X-资料索引.md`、`活动目录.docx`、支出证明一律排除 |
| 以为 `split` 会按序号比对 | 按**活动名**比对、忽略序号（方案 A），否则已有活动会被重复建 |
| 以为编号能手工指定任意值 | `POST /activities` 可传 `code`，但全项目必须唯一；不给则自动分配 |
| 以为目录名可随意起 | 必须 `两位序号-活动名-期次或小区`，服务端 400 拒绝（§5.5.6b） |
