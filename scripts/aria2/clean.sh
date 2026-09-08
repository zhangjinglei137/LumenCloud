#!/usr/bin/env bash
#
# aria2 --on-download-complete hook 融合版（LumenCloud P6 事件驱动回调 + P3TERX clean）
#
# 组成（自上而下）：
#   ① mv 修复块（原有，代码未修改）：保留目录层级，把 $3=下载文件移入 /downloads/done
#   ② 回调通知块（新增）：把 $1=GID 透传给 LumenCloud 后端 /internal/aria2/notify
#      （HMAC-SHA256 签名，事件驱动推进 downloading→scrape），失败静默 ——
#      不阻塞后续 clean 流程；事件丢失由后端轮询兜底。
#   ③ P3TERX clean 原逻辑（原有，全部保留不动）：core 加载 + 参数校验 + 清冗余
#      （.aria2 控制文件等）。
#
# aria2.conf 只需一行（沿用你现有的绑定即可）：
#   on-download-complete=/config/script/clean.sh
# 依赖：curl、openssl、GNU date（+%s%3N；Debian 基础镜像自带）。
#
# Copyright (c) 2018-2021 P3TERX <https://p3terx.com>
# 本文档融合了 P3TERX clean.sh（MIT License）与 LumenCloud notify.sh 逻辑。

# ======================== 修复：保留目录层级移动文件 ========================
# 原文件完整路径（如 /downloads/凡人修仙传/Season 1/xxx.mp4）
SRC_FILE="$3"
DEST_ROOT="/downloads/done"

# 1. 提取原文件目录
SRC_DIR=$(dirname "$SRC_FILE")

# 2. 修复后的逻辑：把开头的 /downloads 换成 /downloads/done
# 这里的 #/downloads 意思是：如果开头是 /downloads 就匹配它
DEST_DIR="/downloads/done${SRC_DIR#/downloads}"

# 3. 执行
mkdir -p "$DEST_DIR"
mv "$SRC_FILE" "$DEST_DIR/"

echo "[$(date)] 移动文件：$SRC_FILE → $DEST_DIR/" >> /config/script/mv_log.log
# ======================== mv 部分修复结束 ========================

# ======================== ② 回调通知块（新增，请改配置） ========================
# 只透传 $1=GID，不做 RPC 反查；后端按 DownloadQueue.aria2_gid 反查并幂等推进。
# 配置：优先读环境变量，未设置时用下方常量兜底（需与后端
#   system_config.internal_aria2_webhook_secret / env ARIA2_WEBHOOK_SECRET 一致）
NOTIFY_BACKEND_URL="${ARIA2_BACKEND_URL:-http://127.0.0.1:8000}"
NOTIFY_SECRET="${ARIA2_WEBHOOK_SECRET:-}"

_notify_backend() {
    local gid ts_ms body sig
    gid="${1:-}"
    [ -z "$gid" ] && return 0                      # 无 GID：跳过（不阻塞）
    [ -z "$NOTIFY_SECRET" ] && return 0            # 未配置密钥：跳过（轮询兜底）
    ts_ms="$(date +%s%3N 2>/dev/null)"
    [ -z "$ts_ms" ] && ts_ms="$(date +%s)000"
    body="{\"gid\":\"${gid}\",\"ts\":${ts_ms}}"
    sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$NOTIFY_SECRET" -hex 2>/dev/null | awk '{print $2}')"
    [ -z "$sig" ] && return 0                      # 签名失败：跳过
    curl -s -o /dev/null \
        --max-time 10 \
        -X POST "${NOTIFY_BACKEND_URL%/}/internal/aria2/notify" \
        -H "Content-Type: application/json" \
        -H "X-Aria2-Signature: ${sig}" \
        --data "$body" \
        || true                                      # 失败静默，不阻塞 clean
    # 成功/失败都不影响主流程（事件丢失由后端轮询补偿）
}
_notify_backend "$1"
# ======================== ② 回调通知块结束 ========================

CHECK_CORE_FILE() {
    CORE_FILE="$(dirname $0)/core"
    if [[ -f "${CORE_FILE}" ]]; then
        . "${CORE_FILE}"
    else
        echo "!!! core file does not exist !!!"
        exit 1
    fi
}

CHECK_CORE_FILE "$@"
CHECK_PARAMETER "$@"
CHECK_FILE_NUM
CHECK_SCRIPT_CONF
GET_TASK_INFO
GET_DOWNLOAD_DIR
CONVERSION_PATH
CLEAN_UP
exit 0