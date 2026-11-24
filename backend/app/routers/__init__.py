"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

检查点恢复的固定路径优先注册，避免被通用资源路由 /api/{module} 抢先匹配。
"""
from __future__ import annotations

from app.routers import recovery, resource, chains

ROUTERS = [recovery, resource, chains]
