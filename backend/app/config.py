"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "实时数据服务总线平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 平台当前值班人：请求没有显式携带操作人时按此身份处理，
    # 与前端顶部的「当前值班」保持一致。
    default_operator: str = "值班管理员"


settings = Settings()
