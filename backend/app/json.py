"""docker-timezone：全局 UTC+Z 时间序列化。

FastAPI 在构造响应类之前已通过 jsonable_encoder 把 datetime 编码为字符串，
因此补 Z 的拦截点必须在 encoder 层而非响应 render 层。本模块提供
jsonable_encoder 的包装函数：naive datetime 按 UTC 解释追加 Z，
aware datetime 归一为 UTC+Z；其余类型原样交给原函数。
"""
import datetime as _dt

from fastapi.encoders import jsonable_encoder as _orig_jsonable_encoder


def jsonable_encoder_zulu(obj, *args, **kwargs):
    """datetime → UTC+Z ISO 字符串；其余类型委托原 jsonable_encoder。"""
    if isinstance(obj, _dt.datetime):
        if obj.tzinfo is None:
            return obj.isoformat() + "Z"
        return obj.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return _orig_jsonable_encoder(obj, *args, **kwargs)


def install_zulu_encoder() -> None:
    """替换 fastapi.routing 模块的 jsonable_encoder 引用（幂等）。"""
    import fastapi.routing as routing

    routing.jsonable_encoder = jsonable_encoder_zulu
