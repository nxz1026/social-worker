# 社工文档服务 API 使用说明

面向本机 Agent / 外部程序。服务地址 `https://140.83.62.161/sw-api/api`（自签证书，curl 加 `-k`）。

## 认证

```bash
# 登录拿 token（12 小时有效）
TOKEN=$(curl -sk -X POST https://140.83.62.161/sw-api/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<用户名>","password":"<密码>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 后续请求带 Header
curl -sk https://140.83.62.161/sw-api/api/me -H "x-token: $TOKEN"
```

## 端点

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/login` | 登录 → token | — |
| GET | `/api/me` | 当前用户 + 角色 + 项目 | 登录 |
| GET | `/api/docs?project=&category=&q=` | 文档列表 | 登录 |
| GET | `/api/docs/{token}` | 文档详情（含内容） | 登录 + 项目 |
| POST | `/api/docs` | 上传归档（multipart） | editor/admin |
| PATCH | `/api/docs/{token}` | 修改内容 | editor/admin |
| DELETE | `/api/docs/{token}` | 删除（trash 模式） | editor/admin |
| GET | `/api/search?q=&project=` | 知识库检索 | 登录 |
| POST | `/api/generate` | 文书生成 | 登录 |
| GET | `/api/admin/users` | 用户列表 | admin |
| POST | `/api/admin/users` | 建号 | admin |
| PATCH | `/api/admin/users/{id}` | 改角色/项目/禁用 | admin |
| GET | `/api/admin/audit` | 审计日志 | admin |

## 权限模型

| 角色 | 查 | 增 | 改 | 删 | 项目 |
|---|---|---|---|---|---|
| admin | ✓ | ✓ | ✓ | ✓ | 全部 |
| editor | ✓ | ✓ | ✓ | ✓ | 授权项目 |
| viewer | ✓ | ✗ | ✗ | ✗ | 授权项目 |

跨项目访问返回 403。

## 示例

### 归档文件
```bash
curl -sk -X POST https://140.83.62.161/sw-api/api/docs \
  -H "x-token: $TOKEN" \
  -F "file=@/path/文件.docx" -F "project=huimin"
```

### 检索
```bash
curl -sk "https://140.83.62.161/sw-api/api/search?q=%E4%BD%8E%E4%BF%9D&project=huadu" \
  -H "x-token: $TOKEN"
```

### 生成文书
```bash
curl -sk -X POST https://140.83.62.161/sw-api/api/generate \
  -H "x-token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"活动方案","context":"惠民社区为老服务活动"}'
# 类型：个案记录/家访提纲/服务计划/活动方案/项目申报/评估报告
# 耗时约 60 秒
```

### 建号（admin）
```bash
curl -sk -X POST https://140.83.62.161/sw-api/api/admin/users \
  -H "x-token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"初始密码","role":"editor","projects":["huimin"]}'
```

## 错误码

| 码 | 含义 |
|---|---|
| 400 | 参数错误 |
| 401 | 未登录 / token 过期 |
| 403 | 无权限（角色不足或跨项目） |
| 404 | 文档不存在 |
| 409 | 用户名已存在 |
| 500 | 服务端错误 |

## 注意

- 自签证书：curl 必须加 `-k`
- 文书生成耗时约 60s（longcat 推理模型）
- 删除是 trash 模式（md 移入 `.trash`，可恢复）；彻底删除需本机 `reconcile --purge`
- 归档的文件名要起描述性名（日期+项目+内容关键词），便于检索
