# systemd user 单元

社工工作台的定时任务与常驻服务。**这些文件在仓库里，但实际生效的是 `~/.config/systemd/user/` 下的副本。**

## 安装

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now social-work-api.service \
  social-work-readme.timer social-work-feishu.timer social-work-audit.timer
```

## 单元清单

| 单元 | 频率 | 作用 |
|---|---|---|
| `social-work-api.service` | 常驻 | FastAPI 后端（127.0.0.1:8001） |
| `social-work-readme.timer` | 15 分钟 | 刷新活动/项目索引 md + docx 骨架 |
| `social-work-feishu.timer` | 1 小时 | 把 projects/ 镜像到飞书云文档 |
| `social-work-audit.timer` | 每天 22:00 | 审计摘要 |

## 注意

- 单元里写死了 `/srv/social-work` 路径，换部署根目录要同步改
- `social-work-feishu` 依赖 `lark-cli` 的 user 身份，token 过期需重新授权：
  `lark-cli auth login --domain drive --no-wait --json`，再把 verification_url 交给用户
