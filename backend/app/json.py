"""docker-timezone：全局 UTC+Z 时间序列化。

FastAPI 在构造响应类之前已通过 jsonable_encoder 把 datetime 编码为字符串，
因此补 Z 的拦截点必须在 encoder 层而非响应 render 层。本模块提供
jsonable_encoder 的包装函数：naive datetime 按 UTC 解释追加 Z，
aware datetime 归一为 UTC+Z；其余类型原样交给原函数。

修复轮（round 1）说明：fastapi.encoders.jsonable_encoder 内部对 dict/list 等
容器的递归引用的是 encoders 模块自身的全局名，仅替换
fastapi.routing.jsonable_encoder 无法覆盖嵌套结构（serialize_response 调用
时不传 custom_encoder，dict 内 datetime 会走默认 isoformat 输出无 Z）。
故包装函数在委托原函数时预注入 datetime → 补 Z 的 custom_encoder，原函数
递归路径会传导该映射，使任意嵌套深度的 datetime 统一补 Z，同时完整保留
include/exclude/by_alias/sqlalchemy_safe 等原函数语义，且无循环调用风险。
"""
import datetime as _dt

from fastapi.encoders import jsonable_encoder as _orig_jsonable_encoder


def _to_zulu(dt: _dt.datetime) -> str:
    """datetime → UTC+Z ISO 字符串（naive 按 UTC 解释；aware 归一 UTC）。"""
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def jsonable_encoder_zulu(obj, *args, **kwargs):
    """datetime → UTC+Z ISO 字符串；其余类型委托原 jsonable_encoder。

    委托时注入 datetime 的 custom_encoder，使原函数递归（dict/list/BaseModel
    展开后的 dict 等）内部的 datetime 同样命中补 Z 逻辑。调用方显式传入的
    custom_encoder 优先，不覆盖。
    """
    if isinstance(obj, _dt.datetime):
        return _to_zulu(obj)
    merged = dict(kwargs)
    user_custom = merged.get("custom_encoder") or {}
    if _dt.datetime not in user_custom:
        merged["custom_encoder"] = {**user_custom, _dt.datetime: jsonable_encoder_zulu}
    return _orig_jsonable_encoder(obj, *args, **merged)


def install_zulu_encoder() -> None:
    """替换 fastapi.routing 模块的 jsonable_encoder 引用（幂等）。"""
    import fastapi.routing as routing

    routing.jsonable_encoder = jsonable_encoder_zulu
