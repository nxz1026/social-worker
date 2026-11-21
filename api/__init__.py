"""社工文档服务 API 包。

`api` 是包（而非散落的顶层模块），目的是让 app.py 与 shared/* 子模块
能用 `api.*` 绝对导入共享同一份模块实例，同时兼容两种启动方式：
  - 生产：`python3 /srv/social-work/api/app.py`（脚本模式）
  - 测试/工具：`import api.app`（包模式）
"""
