#!/usr/bin/env python3
"""统一业务错误类型。

`ApiError` 同时被 app.py（注册异常处理器）和 shared/* 子模块（抛错）使用，
单独成模块是为了避免子模块反向 import app.py 造成循环依赖。
"""
from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    """机器可读的业务错误。

    /api/sw/* 用 {"error": {code, message, hint}}；其余端点仍回 {"detail": ...}，
    保持 Web 界面原有契约不变。
    """

    def __init__(self, status: int, code: str, message: str, hint: str = "",
                 *, retry_after: int | None = None):
        super().__init__(status_code=status, detail=message)
        self.code = code
        self.message = message
        self.hint = hint
        # 限速类错误用：客户端按秒数退避，同时写 Retry-After 头
        self.retry_after = retry_after
