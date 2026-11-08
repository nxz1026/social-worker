#!/usr/bin/env bash
# 社工文档服务 API 包装（skill 专用）
# token 从 ~/.dsh/skills/social-work/.token 读
set -euo pipefail

API="${SOCIALWORK_API:-https://140.83.62.161/sw-api/api}"
TOKEN_FILE="${SOCIALWORK_TOKEN_FILE:-$HOME/.dsh/skills/social-work/.token}"
TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || true)"

if [ -z "$TOKEN" ]; then
  echo "错误：token 文件不存在或为空（$TOKEN_FILE）" >&2
  echo "请从 https://140.83.62.161/sw/ 登录后，把 token 写入该文件" >&2
  exit 2
fi

cmd="${1:-}"; shift || true

case "$cmd" in
  archive)
    # archive <文件路径> [项目]
    [ $# -ge 1 ] || { echo "用法: api.sh archive <文件路径> [项目]" >&2; exit 1; }
    file="$1"; project="${2:-}"
    args=(-sk -X POST "$API/docs" -H "x-token: $TOKEN" -F "file=@$file")
    [ -n "$project" ] && args+=(-F "project=$project")
    curl "${args[@]}"
    ;;
  search)
    # search <查询> [项目]
    [ $# -ge 1 ] || { echo "用法: api.sh search <查询> [项目]" >&2; exit 1; }
    q="$1"; project="${2:-}"
    enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$q")
    url="$API/search?q=$enc"
    [ -n "$project" ] && url="$url&project=$project"
    curl -sk "$url" -H "x-token: $TOKEN"
    ;;
  list)
    # list [项目]
    url="$API/docs"
    [ $# -ge 1 ] && url="$url?project=$1"
    curl -sk "$url" -H "x-token: $TOKEN"
    ;;
  *)
    echo "用法: api.sh archive <文件> [项目] | search <查询> [项目] | list [项目]" >&2
    exit 1
    ;;
esac
