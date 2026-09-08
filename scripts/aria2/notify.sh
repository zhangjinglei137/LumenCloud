#!/bin/sh
# aria2 --on-download-complete hook 回调脚本（LumenCloud P6 事件驱动，设计文档 §6）。
#
# 部署到 aria2 主机（p3terx/aria2-pro 容器内）：/config/script/notify.sh
# aria2.conf 追加：on-download-complete=/config/script/notify.sh
#
# 职责（审阅 P1 修订：脚本只透传，不做 RPC 反查）：
#   - 取 $1=GID，构造 body {"gid": "<gid>", "ts": <毫秒时间戳>}
#   - 对 body 原文计算 HMAC-SHA256 签名（头 X-Aria2-Signature），POST 到后端
#     /internal/aria2/notify（后端按 DownloadQueue.aria2_gid 反查推进，幂等）
#   - curl --max-time 10；失败静默退出非 0 —— 不阻塞 aria2 主流程，
#     事件丢失由后端现有 tellStatus/tellStopped 轮询兜底（§6.2）。
#
# 依赖：curl、openssl、GNU date（+%s%3N 毫秒，Debian 基础镜像自带）。
set -u

# ---------------------------------------------------------------------------
# 配置：优先读环境变量，未设置时用脚本顶部常量兜底。
#   ARIA2_BACKEND_URL   后端基础 URL，如 http://10.0.0.5:8000（容器内需可达）
#   ARIA2_WEBHOOK_SECRET 签名密钥，须与后端 system_config.internal_aria2_webhook_secret
#                        （或 env ARIA2_WEBHOOK_SECRET）完全一致
# ---------------------------------------------------------------------------
BACKEND_URL="${ARIA2_BACKEND_URL:-http://127.0.0.1:8000}"
WEBHOOK_SECRET="${ARIA2_WEBHOOK_SECRET:-}"

GID="${1:-}"
if [ -z "$GID" ]; then
    echo "[notify.sh] 缺少 gid 参数（用法: notify.sh <gid>）" >&2
    exit 1
fi
if [ -z "$WEBHOOK_SECRET" ]; then
    echo "[notify.sh] 未配置 ARIA2_WEBHOOK_SECRET，跳过回调（轮询兜底）" >&2
    exit 1
fi

# 构造 body：gid 为 aria2 生成的十六进制串，无 JSON 转义风险
TS_MS="$(date +%s%3N 2>/dev/null)"
[ -z "$TS_MS" ] && TS_MS="$(date +%s)000"
BODY="{\"gid\":\"${GID}\",\"ts\":${TS_MS}}"

# 对 body 原文计算 HMAC-SHA256（hex），与后端 hmac.compare_digest 校验对齐
SIG="$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex 2>/dev/null | awk '{print $2}')"
if [ -z "$SIG" ]; then
    echo "[notify.sh] 计算 HMAC 签名失败（需 openssl），跳过回调" >&2
    exit 1
fi

# POST 回调：max-time 10s，失败仅记日志并退出非 0（不重试、不阻塞 aria2；
# 事件丢失由后端轮询补偿）
curl -s -o /dev/null \
    --max-time 10 \
    -X POST "${BACKEND_URL%/}/internal/aria2/notify" \
    -H "Content-Type: application/json" \
    -H "X-Aria2-Signature: ${SIG}" \
    --data "$BODY"
exit $?
