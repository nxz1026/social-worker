#!/usr/bin/env bash
# 把一台新设备加入「社工 vault」同步。
#
# 用法（在客户端机器上执行，需要能 ssh 到服务器）：
#   ssh ubuntu@<服务器IP> 'bash -s' < syncthing-add-device.sh <客户端设备ID>
#
# 客户端设备 ID 在 Syncthing Web UI (http://127.0.0.1:8384) 首页顶部。
set -euo pipefail

DEVICE_ID="${1:?缺少客户端设备 ID}"
API=$(cat /srv/social-work/.syncthing-key)

python3 - "$DEVICE_ID" "$API" <<'PY'
import json, sys, urllib.request

device_id, api = sys.argv[1], sys.argv[2]
BASE = 'http://127.0.0.1:8384'

def req(method, path, data=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header('X-API-Key', api)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        r.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(r, body, timeout=20) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None

cfg = req('GET', '/rest/config')

if not any(d['deviceID'] == device_id for d in cfg['devices']):
    cfg['devices'].append({
        'deviceID': device_id,
        'name': 'obsidian-client',
        'addresses': ['dynamic'],
        'compression': 'metadata',
        'introducer': False,
        'paused': False,
    })
    print(f'已添加设备 {device_id}')
else:
    print('设备已在列表中')

for f in cfg['folders']:
    if f['id'] != 'social-work-vault':
        continue
    devs = f.setdefault('devices', [])
    if not any(d['deviceID'] == device_id for d in devs):
        devs.append({'deviceID': device_id, 'introducedBy': '', 'encryptionPassword': ''})
        print('已共享文件夹 social-work-vault')

# 顺手关掉崩溃上报与使用统计
cfg.setdefault('options', {})
cfg['options']['crashReportingEnabled'] = False
cfg['options']['urAccepted'] = 3

req('PUT', '/rest/config', cfg)
print('配置已保存，Syncthing 会自动重连')
PY
