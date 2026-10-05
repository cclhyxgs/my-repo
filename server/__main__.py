# -*- coding: utf-8 -*-
"""`python -m server` 入口（等价于 uvicorn 命令，显式 workers=1）。"""

import uvicorn

from server import settings


def main() -> None:
    uvicorn.run(
        "server.main:app",
        host=settings.HOST,
        port=settings.PORT,
        workers=1,  # C1#2 规格 S 单副本，禁止调大（R-09）
        reload=False,
    )


if __name__ == "__main__":
    main()
