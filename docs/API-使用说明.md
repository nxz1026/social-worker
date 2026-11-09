# 社工文档服务 API 使用说明（客户端 Agent 版）

面向调用方 Agent / 程序。服务 `https://140.83.62.161/sw-api/api`，自签证书（客户端跳过校验）。所有响应为 JSON。

## 认证

```
POST /api/login  body: {"username","password"}
→ {"token": "...", "username": "...", "role": "admin|editor|viewer"}
```

后续每个请求带 Header `x-token: <token>`。token 12 小时有效；收到 401 → 重新登录。

## 端点契约

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/login` | — | 登录拿 token |
| GET | `/api/me` | 登录 | 当前用户 + role + projects |
| GET | `/api/docs?project=&category=&q=` | 登录 | 文档列表；不带 project 返回**全部授权项目** |
| GET | `/api/docs/health` | 登录 | 列表用批量健康状态（原件/md/飞书/索引四态） |
| GET | `/api/docs/{token}` | 登录 + 项目 | 详情（含 content）；已删除返回 404 |
| GET | `/api/docs/{token}/status` | 登录 + 项目 | 五态详情（含 sha256 / md5 / 飞书链接 / 实体数） |
| GET | `/api/docs/{token}/source` | 登录 + 项目 | 下载原件（二进制，逐次写审计） |
| GET | `/api/docs/{token}/relations` | 登录 + 项目 | 实体分组 + 反链 + 共享实体 |
| POST | `/api/docs` | editor/admin | 上传归档（multipart：`file`、可选 `project`） |
| PATCH | `/api/docs/{token}` | editor/admin | 修改内容（JSON：`{"content": "..."}`） |
| DELETE | `/api/docs/{token}` | **admin** | 删除（trash 模式，可恢复） |
| POST | `/api/docs/{token}/feishu` | admin | 旧副本改名 → 上传新副本 → 旧副本进飞书回收站 |
| DELETE | `/api/docs/{token}/feishu` | admin | 删除飞书副本（本地原件/md/索引不动） |
| POST | `/api/docs/{token}/reindex` | admin | 按项目数据集重建索引（同步，可能几十秒） |
| POST | `/api/meta/relink` | admin | 按当前 `entities.yaml` 给已有 md 补 `[[实体]]` 并重建索引（`{"project"?,"dry_run"?}`） |
| GET | `/api/issues` | admin | 待确认 + 已归档残留 + 今日失败 + 错误队列 |
| POST | `/api/pending/archive` | admin | 待确认文件重新归档（`{"path","project"?}`） |
| POST | `/api/pending/delete` | admin | 丢弃待确认文件到 `sources/.trash` |
| GET | `/api/trash` | admin | 回收站清单（md / 源文件） |
| POST | `/api/trash/restore` | admin | 恢复原位并自动对账（`{"path"}`） |
| POST | `/api/trash/purge` | admin | 彻底删除（不可恢复，`{"path"}`） |
| GET | `/api/search?q=&project=` | 登录 | 知识库检索；不带 project 检索全部授权项目 |
| POST | `/api/generate` | 登录 | 文书生成（JSON：`{"type","context","project"?}`） |
| GET | `/api/admin/users` | admin | 用户列表 |
| POST | `/api/admin/users` | admin | 建号（`{"username","password","role","projects"}`） |
| PATCH | `/api/admin/users/{id}` | admin | 改角色/项目/禁用 |
| GET | `/api/admin/audit?limit=` | admin | 审计日志 |

**权限模型**：admin 全部项目；editor 授权项目可增删改查；viewer 授权项目只读。跨项目访问返回 403。

## 行为约定（调用方必读）

| 操作 | 耗时 | 说明 |
|---|---|---|
| 上传归档 | 30–70s | 同步返回（含分类入库）；`project` 未传则按文件名+内容自动判 |
| 检索 | 10–90s / 项目 | 不带 project 时按项目**串行**检索再合并，多项目耗时叠加；全部超时返回 `{"ok":true,"output":"","note":"检索超时..."}` |
| 文书生成 | 2–6 分钟 | 先检索素材再调 LLM；超时（240s 无响应）返回 504 |
| PATCH | 1–3 分钟 | 触发索引重建（同步阻塞） |
| DELETE | 20–30s | trash 模式；删后详情 404；**仅 admin** |
| 下载原件 | <1s | 返回文件流，写一条 `download` 审计 |
| 重建索引 | 20–60s | 按项目数据集同步跑 reconcile（幂等） |
| 飞书重传 | 5–20s | 旧副本先改名（`.旧-时间戳`），新副本传成功后才删旧；返回 `old_trashed` |
| 补实体链接 | 1–4 分钟 | 改 md 后要重建索引，耗时随文档数增长；`dry_run=true` 只看会改几篇 |
| 待确认归档 | 30–70s | 同上传归档；成功后把兜底目录里那份移入回收站 |

**检索响应**：

```json
{"ok": true, "output": "### 文档名\n片段…", "datasets": ["huadu", "huimin"]}
```

`output` 是拼接后的纯文本，`###` 行是命中文档名；`datasets` 为本次实际检索的数据集。
`ok=false` 表示检索脚本异常（非超时），可重试。

**生成响应**：

```json
{"ok": true, "content": "……", "truncated": false, "finish_reason": "stop"}
```

- `truncated=true`（`finish_reason=length`）说明正文被模型输出上限截断，应缩短 context 或拆分文书后重试；
- 素材检索固定取原文片段（CHUNKS），正文中引用素材会标注 `（来源：<文档名>）`；
- `project` 省略时合并该账号全部授权项目作为素材，指定时只用该项目（无权限返回 403）。

**幂等性**：
- 重复上传同内容文件 → 同一 token（内容哈希）
- 重复登录 → 新 token（旧 token 仍有效）
- PATCH/DELETE 幂等

## 错误码

| 码 | 含义 | 调用方处理 |
|---|---|---|
| 400 | 参数错误 | 检查 body/字段 |
| 401 | 未登录 / token 过期 | 重新登录 |
| 403 | 角色不足 / 跨项目 / 账号已禁用 | 换账号或申请权限 |
| 404 | 文档不存在 / 已删除 | 视为已删除 |
| 409 | 用户名已存在 | 换用户名 |
| 500 | 服务端错误（含 LLM 未返回正文） | 看 `detail` 后重试或报告 |
| 504 | 生成超时 | 重试 |

## 调用示例（Python 标准库）

```python
import json, urllib.request, urllib.error, ssl, urllib.parse

BASE = "https://140.83.62.161/sw-api/api"      # 已含 /api，路径不要再带 /api 前缀
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def call(method, path, token=None, body=None, raw=None, timeout=300):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
    if token:
        headers["x-token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

# 登录
st, d = call("POST", "/login", body={"username": "xxx", "password": "xxx"})
token = d["token"]

# 检索（不带 project = 全部授权项目）
q = urllib.parse.quote("民乐北巷 治理公约 协商议事")
st, d = call("GET", f"/search?q={q}&project=huimin", token=token)
print(d["datasets"], d["output"][:500])

# 生成文书（带 project 时素材只取该项目）
st, d = call("POST", "/generate", token=token,
             body={"type": "活动方案", "project": "huimin",
                   "context": "惠民社区为老服务活动"})
print(d["finish_reason"], d["truncated"])
print(d["content"])
```

**上传（curl）**：
```bash
curl -sk -X POST "$BASE/docs" -H "x-token: $TOKEN" \
  -F "file=@/path/文件.docx" -F "project=huimin"
```

## 常见任务

**归档一份文件**
1. `POST /api/docs`（带 file + 显式 project）
2. 判定成功：`GET /api/docs?project=<项目>&q=<文件名关键词>` 能查到该文档（`name` / `project` 正确）
3. 带 project 时分类置信度低**不会**再落 `_待确认`，只影响 `category`；不带 project 且置信度不足才会转人工

**检索并生成文书**
1. `GET /api/search?q=<主题>&project=<项目>` 取素材（看 `output` 里的 `###` 文档名判断命中）
2. `POST /api/generate` body `{type, context, project}` → `content`
3. 检查 `truncated`：为 `true` 时正文被截断，缩小 context 或拆成多份文书

**删除文档**
1. `DELETE /api/docs/{token}`（仅 admin）
2. 详情返回 404（已删）；彻底删除需本机 `reconcile --purge`

**处理待确认文件**
1. `GET /api/issues` → `pending[]` 每项带 `path`、`status`（`pending` / `stale`）、`suggested_project`
2. 归档：`POST /api/pending/archive` body `{"path", "project"?}`；不传 `project` 就用推荐值
3. 丢弃：`POST /api/pending/delete` body `{"path"}`（移到 `sources/.trash`，可恢复）
4. `stale` 表示这份文件后来已归档成功（`reason` 里有归档位置），只需清理，不用再处理

## 注意

- 归档文件名起描述性名（日期+项目+内容关键词），便于检索与溯源
- 显式 `project` 优先于内容判定，归档后可用详情里的 `detected_project` 对照内容识别结果
- 检索/生成失败时先看 `note` 字段（超时提示）；`ok=false` 且无 `note` 才是真异常
- 生成接口耗时长（2–6 分钟），客户端超时不要低于 360s
- 服务端为单机部署，检索与生成都较吃资源；高并发下耗时上升，建议串行调用
