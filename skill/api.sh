#!/usr/bin/env bash
# 社工文档服务 API 包装（skill 专用）
# token 从 ~/.dsh/skills/social-work/.token 读
set -euo pipefail

API="${SOCIALWORK_API:-}"
TOKEN_FILE="${SOCIALWORK_TOKEN_FILE:-$HOME/.dsh/skills/social-work/.token}"
TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || true)"

if [ -z "$API" ]; then
  echo "错误：未设置 SOCIALWORK_API（例如 https://<host>/sw-api/api）" >&2
  exit 2
fi

# 默认校验证书；自签证书请把 CA 配到系统信任库，或用 SOCIALWORK_INSECURE=1 显式降级
CURL=(curl -sS)
[ "${SOCIALWORK_INSECURE:-0}" = "1" ] && CURL+=(-k)

if [ -z "$TOKEN" ]; then
  echo "错误：token 文件不存在或为空（$TOKEN_FILE）" >&2
  echo "请从 ${SOCIALWORK_WEB:-https://<host>/sw/} 登录后，把 token 写入该文件" >&2
  exit 2
fi

cmd="${1:-}"; shift || true

case "$cmd" in
  archive)
    # archive <文件路径> [项目]
    [ $# -ge 1 ] || { echo "用法: api.sh archive <文件路径> [项目]" >&2; exit 1; }
    file="$1"; project="${2:-}"
    args=(-X POST "$API/docs" -H "x-token: $TOKEN" -F "file=@$file")
    [ -n "$project" ] && args+=(-F "project=$project")
    "${CURL[@]}" "${args[@]}"
    ;;
  search)
    # search <查询> [项目]
    [ $# -ge 1 ] || { echo "用法: api.sh search <查询> [项目]" >&2; exit 1; }
    q="$1"; project="${2:-}"
    enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$q")
    url="$API/search?q=$enc"
    [ -n "$project" ] && url="$url&project=$project"
    "${CURL[@]}" "$url" -H "x-token: $TOKEN"
    ;;
  list)
    # list [项目]
    url="$API/docs"
    [ $# -ge 1 ] && url="$url?project=$1"
    "${CURL[@]}" "$url" -H "x-token: $TOKEN"
    ;;
  *)
    echo "用法: api.sh archive <文件> [项目] | search <查询> [项目] | list [项目]" >&2
    exit 1
    ;;
esac
