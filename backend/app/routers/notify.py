"""aria2 下载完成 hook 回调端点（P6 事件驱动，docs/影视下载两队列重设计.md §6）。

- `POST /internal/aria2/notify`：aria2 主机 notify.sh 只透传 `$1=gid` 回调。
  端点**不部署在 /api 前缀下**（/internal/* 为内部回调专用，见 api.py 顶层聚合）。
- 鉴权：HMAC-SHA256（对 body 原文签名，请求头 `X-Aria2-Signature`），密钥 =
  `system_config.internal_aria2_webhook_secret`（DB 优先，settings/env
  `ARIA2_WEBHOOK_SECRET` 兜底），不使用 JWT 登录鉴权。
- 防重放：body 带 `ts`（毫秒时间戳），`|now_ms - ts| > 15min` 拒绝（401）——
  容差放宽防后端重启/积压误拒（§6.2）；重复推进由幂等校验兜底。
- 反查：按 `DownloadQueue.aria2_gid` 反查任务（comment 仅作 GID 来源校验辅助，
  由 P5 `trigger_download_complete` 内部完成）。
- 推进：延迟导入 `app.tasks.transfer.trigger_download_complete(gid)`（P5 实现，
  签名已冻结 `async (gid: str) -> bool`）；函数未就绪 → 503「回调处理未就绪」。
  幂等（仅 downloading→scrape 条件更新推进）由该函数内部保证，端点不重复逻辑。
- 事件丢失兜底：现有 tellStatus/tellStopped 轮询不变（§6.2，事件丢失由轮询补偿推进）。
"""
import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

# 路由前缀 = /internal（顶层聚合时不经 /api 前缀，见 api.py）
router = APIRouter(prefix="/internal/aria2", tags=["notify"])

# 时间戳防重放容差（毫秒）：±15min（§6.2 放宽防后端重启/积压误拒）
_TS_TOLERANCE_MS = 15 * 60 * 1000

_SIGNATURE_HEADER = "X-Aria2-Signature"


def _webhook_secret() -> str:
    """读取签名密钥：system_config.internal_aria2_webhook_secret（DB 优先），
    其次 settings/env `ARIA2_WEBHOOK_SECRET` 兜底；未配置返回空串。"""
    return (
        config_store.get("internal_aria2_webhook_secret", settings.ARIA2_WEBHOOK_SECRET)
        or ""
    ).strip()


@router.post("/notify")
async def aria2_notify(request: Request):
    """aria2 `on-download-complete` hook 回调入口。

    body JSON：`{"gid": str, "ts": int(毫秒时间戳)}` + 头 `X-Aria2-Signature`。
    校验顺序（§6.2）：
      1. secret 缺失                      → 503；
      2. `|now_ms - ts| > 15min`（含 ts 缺失）→ 401「签名/时间戳无效」；
      3. 签名不匹配（含头缺失）            → 401；
      4. gid 为空                          → 400。
    通过后延迟导入调用 `trigger_download_complete(gid)` 推进任务，返回 `{"ok": true}`；
    函数未就绪（P5 未部署/被移除）→ log warning + 503「回调处理未就绪」。
    """
    # 1) secret 缺失 → 503（fail-closed：未配置签名密钥一律拒绝，防无鉴权回调）
    secret = _webhook_secret()
    if not secret:
        logger.warning("[notify] internal_aria2_webhook_secret 未配置，拒绝回调")
        raise HTTPException(status_code=503, detail="回调签名密钥未配置")

    # body 原文（HMAC 签名对象 = 原样字节，防序列化歧义）
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    # 2) 时间戳防重放（±15min）
    now_ms = int(time.time() * 1000)
    ts = payload.get("ts")
    if not isinstance(ts, int) or abs(now_ms - ts) > _TS_TOLERANCE_MS:
        raise HTTPException(status_code=401, detail="签名/时间戳无效")

    # 3) HMAC-SHA256 签名校验（常数时间比较，防时序侧信道）
    signature = request.headers.get(_SIGNATURE_HEADER, "")
    expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        logger.warning("[notify] HMAC 签名校验失败（gid=%s）", payload.get("gid"))
        raise HTTPException(status_code=401, detail="签名无效")

    # 4) gid 必填
    gid = (payload.get("gid") or "").strip()
    if not gid:
        raise HTTPException(status_code=400, detail="gid 不能为空")

    # 5) 延迟导入触发推进（P5 实现；函数未就绪 → 503，不伪装成功）
    try:
        from app.tasks.transfer import trigger_download_complete  # noqa: PLC0415
    except (ImportError, AttributeError) as exc:  # noqa: BLE001  P5 未部署时优雅降级
        logger.warning("[notify] trigger_download_complete 未就绪，回调暂存由轮询兜底: %s", exc)
        raise HTTPException(status_code=503, detail="回调处理未就绪")

    # 推进成功与否（bool）不在此区分：幂等由 trigger_download_complete 内部条件
    # 更新保证（仅 downloading→scrape 推进），回调本身已受理即应答成功。
    await trigger_download_complete(gid)
    logger.info("[notify] aria2 下载完成回调已受理（gid=%s）", gid)
    return {"ok": True}
