"""全局状态：配置派生常量与进程内缓存。

拆出来的目的：这些被多个子模块共用，放在 app.py 会形成循环 import。
所有字段都是「读多写少」或「带锁的进程内缓存」，无业务逻辑。
"""
from __future__ import annotations

import threading
from datetime import timedelta, timezone
from pathlib import Path

# 配置本身仍由 settings.py 负责加载，这里只做派生，不新增配置读取逻辑。
from .settings import SETTINGS

ROOT = SETTINGS.root
SCRIPTS = ROOT / "scripts"
# engine 是仓库根下的包，不在 scripts 里
REPO_ROOT = Path(__file__).resolve().parent.parent

PG_DSN = SETTINGS.db_dsn
TZ = timezone(timedelta(hours=SETTINGS.timezone_offset_hours))
TOKEN_TTL_HOURS = SETTINGS.token_ttl_hours
ALL_PROJECTS = SETTINGS.projects

TEMPLATES_DIR = SETTINGS.templates_dir
TYPE_MAP = {
    "个案记录": "case-record", "家访提纲": "home-visit-outline",
    "服务计划": "service-plan", "活动方案": "activity-plan",
    "项目申报": "project-application", "评估报告": "evaluation-report",
}

# ---------- 进程内缓存 ----------
_ALIAS_CACHE: dict[str, dict[str, list[str]]] = {}
COGNEE_BASE = "http://127.0.0.1:8000"
_COGNEE_CACHE: dict[str, tuple[float, list[dict] | None]] = {}
_COGNEE_TTL = 15
_SUGGEST_CACHE: dict[tuple[str, float, int], tuple[str, str]] = {}
_SCRIPT_CACHE: dict[str, object] = {}

# 登录失败限速：内存计数，key = 用户名|客户端 IP。
# 单机部署 + 多线程（uvicorn 线程池），用 Lock 保护；进程重启后清零可接受。
_LOGIN_FAIL_WINDOW = 15 * 60          # 窗口 15 分钟
_LOGIN_FAIL_MAX = 10                  # 同 key 窗口内失败 10 次后拒绝
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_FAILS_LOCK = threading.Lock()

# 项目级权限取值（readonly/write）
_PROJECT_ACCESS = ("readonly", "write")

# 图片 / PDF 经 API 上传时必须规范命名，否则拒收（服务端不再替客户端 OCR/识图）
_API_OCR_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".pdf"}

# 审计明细里绝不能出现口令 / token / 个案正文；只保留排查需要的字段。
_SENSITIVE_DETAIL_KEYS = {
    "password", "passwd", "pwd", "token", "api_key", "apikey", "key",
    "secret", "context", "content",
}

# API Key 只用于 Agent 干活，不能接管用户管理与审计（审计 P0-1）。
# 用户此前明确「除用户管理外所有功能」，这里把管理面单独关掉。
API_KEY_BLOCKED_PREFIX = "/api/admin/"

# ---------- 活动照片批量上传（docs/照片上传协议.md） ----------
_PHOTO_MAX_FILES = 200
_PHOTO_MAX_BYTES = 50 * 1024 * 1024
_PHOTO_MAX_TOTAL_BYTES = 500 * 1024 * 1024   # 单次多文件上传整批上限
# 单图像素上限（宽 × 高）：防「小文件解压出巨图」的解压炸弹
_PHOTO_MAX_PIXELS = 80_000_000
# 与 engine.photos.IMAGE_SUFFIXES 保持一致；这里单独写一份便于错误提示
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# zip 上传预算：先看中央目录，再决定读不读，避免解压炸弹打爆内存/磁盘
_ARCHIVE_MAX_BYTES = 1024 * 1024 * 1024           # 压缩包本身 1 GB
_ARCHIVE_MAX_ENTRIES = 5000                        # 条目数上限
_ARCHIVE_MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # 解压后总量 2 GB
_ARCHIVE_RATIO_LIMIT = 200                         # 单条压缩比上限
_ARCHIVE_RATIO_MIN_BYTES = 10 * 1024 * 1024        # 只对 >10MB 的条目查压缩比
