# aria2 下载完成回调部署（P6 事件驱动）

> 对应设计文档：`docs/影视下载两队列重设计.md` §六（aria2 下载完成回调）。
> 交付物：后端端点 `POST /internal/aria2/notify`（`backend/app/routers/notify.py`）+
> aria2 主机脚本 `scripts/aria2/notify.sh`（部署时置于容器 `/config/script/`）。

## 一、回调链路

```
aria2 下载完成
  └─ notify.sh（aria2 主机，$1=gid）
       └─ POST http://<backend>/internal/aria2/notify
            Headers: X-Aria2-Signature = HMAC-SHA256(body原文, secret)
            Body: {"gid": "<gid>", "ts": <毫秒时间戳>}
                 └─ 后端校验签名/时间戳 → 延迟导入 trigger_download_complete(gid)
                      ├─ 按 DownloadQueue.aria2_gid 反查任务（comment 仅作 GID 来源校验辅助）
                      ├─ 幂等推进（仅 downloading→scrape 条件更新，防重复回调）
                      └─ download_complete 通知照旧（notifier：站内 + PushPlus）
```

- **脚本只透传 `$1=GID`**：不在脚本侧做 aria2 RPC 反查（避免脚本持 RPC token / 多一跳网络），
  任务反查由后端完成。
- **事件丢失兜底**：现有 `tellStatus/tellStopped` 轮询保留，事件丢失 → 轮询补偿推进；
  回调与轮询并发推进 → 条件更新幂等（不重复计数/通知）。
- **幂等**：回调推进由 `trigger_download_complete` 内部保证（仅 downloading→scrape 条件
  更新），端点上不重复任何推进逻辑；重复回调天然无害。

## 二、后端侧配置（签名密钥）

端点不使用 JWT 登录鉴权，改用 **HMAC-SHA256 签名**（body 原文签名，请求头
`X-Aria2-Signature`，hex）。密钥读取顺序（DB 优先，env 兜底）：

1. `system_config.internal_aria2_webhook_secret`（推荐，settings 页面可配置，保存即生效）；
2. 环境变量 `ARIA2_WEBHOOK_SECRET`（旧部署平滑迁移兜底，`backend/app/config.py`）。

> 密钥未配置时端点返回 `503`（fail-closed：未配置签名密钥一律拒绝回调，防无鉴权回调）。

### 设置方式

- 方式 A（推荐）：登录后端管理后台 → 设置页 → 配置 `internal_aria2_webhook_secret`
  （任意强随机串，如 `openssl rand -hex 32`）；
- 方式 B（env fallback）：后端容器/进程设置环境变量 `ARIA2_WEBHOOK_SECRET=<值>`。

> 同一密钥须在 **后端** 与 **aria2 主机的 notify.sh** 两处保持一致（见下）。

## 三、aria2 主机部署清单（p3terx/aria2-pro）

1. **放置脚本**：将 `scripts/aria2/notify.sh` 复制到容器内 `/config/script/notify.sh`，
   并确认可执行：
   ```sh
   docker cp scripts/aria2/notify.sh <aria2容器>:/config/script/notify.sh
   docker exec <aria2容器> chmod +x /config/script/notify.sh
   ```

2. **aria2.conf 追加 hook**（`/config/aria2.conf` 由 `-e` 或文件映射注入）：
   ```
   on-download-complete=/config/script/notify.sh
   ```
   重启 aria2 容器使配置生效（仅当 aria2.conf 未以 `on-download-complete` 实时读取时）。

3. **配置后端地址与签名密钥**（环境变量或脚本顶部常量二选一）：
   ```sh
   # 容器环境变量（推荐，避免改动脚本）
   ARIA2_BACKEND_URL=http://10.0.0.5:8000   # 后端地址，容器内需可达
   ARIA2_WEBHOOK_SECRET=<与后端一致的密钥>
   ```
   > 也可直接编辑脚本顶部常量 `BACKEND_URL` / `WEBHOOK_SECRET`（不推荐，易随镜像漂移）。

4. **容器出网 / 后端可达性验证**：
   ```sh
   docker exec <aria2容器> sh -c \
     "curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://10.0.0.5:8000/api/health"
   ```
   返回 `200` 即容器可出网访问后端。若后端在 Docker 同一自定义网络内，用服务名/容器名；
   跨主机用宿主机 IP 或内网域名。

5. **签名密钥一致性检查**：后端 `internal_aria2_webhook_secret` 的值必须与容器内
   `ARIA2_WEBHOOK_SECRET` 完全一致（含无前后空格），否则回调返回 401「签名无效」，
   事件由轮询兜底（功能不受影响，但回调失效）。

### 验证回调端到端

在 aria2 主机容器内手动模拟一次回调（gid 可用任一 hex 串）：

```sh
GID="DEADBEEF00000001"
TS_MS="$(date +%s%3N)"
BODY="{\"gid\":\"${GID}\",\"ts\":${TS_MS}}"
SIG="$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print $2}')"
curl -s --max-time 10 -X POST "http://10.0.0.5:8000/internal/aria2/notify" \
  -H "Content-Type: application/json" \
  -H "X-Aria2-Signature: ${SIG}" \
  --data "$BODY"
# 预期: {"ok": true}
```

真实 GID 完成后触发：后端日志应出现 `[notify] aria2 下载完成回调已受理（gid=...）`，
任务按 `DownloadQueue.aria2_gid` 反查后从 downloading 推进到 scrape（幂等条件更新）。

## 四、安全说明

- **防重放**：body 带 `ts`（毫秒时间戳），后端校验 `|now_ms - ts| ≤ 15min`
  （容差放宽防后端重启/积压误拒）；超窗返回 401「签名/时间戳无效」。
- **签名对象 = body 原文**：HMAC 对实际发送的 body 字节计算，避免 JSON 序列化歧义。
- **为何不用 JWT**：该端点是 aria2 主机脚本回调，无用户上下文、不经过登录流程；
  HMAC 共享密钥 + 时间戳防重放是内部服务间回调的标准做法（aria2 脚本无环境获取/刷新 token）。
- **密钥泄漏面**：密钥写入后端 system_config 与 aria2 容器环境变量，均属受信主机侧；
  泄漏后攻击者仅能触发「下载完成推进」（幂等、无数据读取能力），并受 15min 时间窗限制。

## 五、排障速查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 回调无日志 | hook 未生效 / 脚本未执行 | 检查 aria2.conf `on-download-complete` 与脚本权限、容器重启 |
| 401 签名无效 | 两端密钥不一致 / body 序列化差异 | 核对 `ARIA2_WEBHOOK_SECRET`，确认脚本用 `--data` 原文 |
| 401 签名/时间戳无效 | 容器时钟漂移 >15min | 校准 aria2 主机时间（NTP） |
| 503 密钥未配置 | 后端未配置 `internal_aria2_webhook_secret` | 设置页配置或设 env `ARIA2_WEBHOOK_SECRET` |
| 503 回调处理未就绪 | `trigger_download_complete` 尚未部署（P5 未上线） | 待 P5 部署；事件由轮询兜底，不丢失 |
| 任务未推进但回调成功 | GID 反查不到（轮询已先行推进 / 任务非 downloading） | 幂等语义，无需处理 |
