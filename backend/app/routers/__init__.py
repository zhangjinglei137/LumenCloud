"""业务 API 路由包。

- api.py 聚合全部子路由并导出 api_router（供 app.main 引入注册）
- media / queue / capacity 为阶段 2 最小验证 API（docs/实施计划.md §3.4）
- deps.py 提供 FastAPI 依赖（数据库会话、阶段 3 JWT 鉴权占位）
"""