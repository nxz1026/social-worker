#!/usr/bin/env bash
# 部署到生产目录 /srv/social-work 并重启服务。
#
# 背景：仓库 /home/ubuntu/DSH/social-worker 是开发目录，
# 生产服务跑的是 /srv/social-work 下的副本，两者独立。
# 每次改完代码必须跑本脚本，否则 systemd 服务还是旧代码。
#
# 用法：
#   ./deploy.sh            # 同步 + 重启
#   ./deploy.sh --dry-run  # 只列出会改哪些文件
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD="/srv/social-work"
DIRS=(api engine scripts config web)
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

# 资产验收放在同步之前：验收不过就什么都不做，生产仍是旧版本。
# 放在同步之后的话，新代码已经对外服务了，验收失败只能报错、拦不住。
echo "== 部署前资产验收（模板/项目数据不入库，跳过不算通过）=="
SOCIAL_WORK_TEST_REPO="${TEST_REPO:-$REPO}" \
  python3 "$REPO/scripts/verify_assets.py"
va=$?
if [ "$va" -ne 0 ]; then
    echo "  ! 资产验收未通过，已中止部署（生产未改动）"
    exit 1
fi

echo "== 同步 $REPO → $PROD =="
for d in "${DIRS[@]}"; do
    src="$REPO/$d"
    [ -d "$src" ] || { echo "  跳过 $d（不存在）"; continue; }
    echo "-- $d"
    rsync -a --itemize-changes $DRY \
        --exclude '__pycache__' --exclude '*.pyc' --exclude '*.bak-*' \
        "$src/" "$PROD/$d/"
done

if [ -n "$DRY" ]; then
    echo "== dry-run，未重启服务 =="
    exit 0
fi

echo "== 重启服务 =="
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus
export XDG_RUNTIME_DIR=/run/user/1001
systemctl --user restart social-work-api.service
sleep 4
systemctl --user is-active social-work-api.service

echo "== 端点数校验 =="
n=$(curl -s http://127.0.0.1:8001/openapi.json \
    | python3 -c "import sys,json;print(len(json.load(sys.stdin)['paths']))" 2>/dev/null || echo 0)
echo "  路由数: $n"
[ "$n" -ge 30 ] || { echo "  ! 路由数异常，服务可能没起来"; exit 1; }

echo "== 完成：https://<host>/sw/ =="
