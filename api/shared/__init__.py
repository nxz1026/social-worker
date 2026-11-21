"""跨模块共用的底层工具。

子模块职责：
  - paths.py    路径归一、越界防护、生成件覆盖保护
  - parse.py    入参解析（严格布尔值等）
  - audit.py    审计写入与明细剔敏
  - delivery.py 生成结果的交付完整性判断

这些模块只依赖 api.errors / api.state / engine，不 import app.py。
"""
