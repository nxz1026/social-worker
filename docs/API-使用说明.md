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

| 项 | 值 |
|---|---|
| 单文件上限 | **50 MB**（API 层）；nginx 允许 100 MB |
| 允许扩展名 | `.docx .xlsx .doc .xls .pdf .jpg .jpeg .png .gif .bmp .webp .txt .md .zip .7z` |
| 上传端点 | `POST /api/projects/{p}/activities/{a}/upload` |

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
| `DUPLICATE_BATCH` | 409 | 24 小时内同活动+同日期+同场景已传过（照片） |
| `TOO_MANY_FILES` | 400 | 单请求超 200 张（照片） |
| `FILE_TOO_LARGE` | 413 | 单文件超限 |
| `NOT_AN_IMAGE` | 400 | 扩展名不是图片 |
| `ARCHIVE_UNSUPPORTED` | 400 | 压缩格式不支持（只收 zip） |
| `ARCHIVE_EMPTY` | 400 | 压缩包里没有图片 |
| `NO_MATCHED_ACTIVITY` | 400 | 压缩包内目录匹配不上任何活动 |

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
| `files` | ✅ | 同名字段重复 N 次，最多 200 张，单张 ≤50MB |
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

当前为桩实现，返回 `{"id":"...","status":"done","result":{}}`。
飞书同步、Cognee 入库等长任务后续接入。

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
```

**注意**：仓库 `/home/ubuntu/DSH/social-worker` 是开发目录，生产服务跑 `/srv/social-work` 副本。
**改代码不同步就没生效**——必须跑 `deploy.sh`。

## 12. 已知限制

| 限制 | 说明 |
|---|---|
| 活动目录内无删除 API | 只能删单个文件（移 `.trash`），不能删目录 |
| `tasks` 是桩 | 无真实异步任务 |
| 照片只收 zip | `.7z`/`.rar` 需客户端先解压 |
| 照片原图不保留 | 压到 2560px q95（约 1/3 体积） |
| 旧项目键歧义 | `huadu` 可能指激励或保障，读旧台账时按列表顺序落到保障资金 |
| 满意度分数需人工录 | 手写体不 OCR，Agent 可读写 `scores.json` |
