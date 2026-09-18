## ADDED Requirements

### Requirement: 业务开关保存成功

设置页各业务开关（含 download_queue_paused）SHALL 能成功保存：前端渲染的每个可编辑/可切换键 SHALL 均在后端可编辑白名单内，保存后 PATCH 成功并生效，不得出现「前端可切换但保存返回 422」的失效状态。

#### Scenario: 队列暂停开关保存成功
- **WHEN** 管理员在设置页切换「下载队列暂停」开关
- **THEN** PATCH 保存成功（非 422），开关状态持久化并生效

#### Scenario: 其余业务开关保存正常
- **WHEN** 管理员修改设置页任意可编辑业务参数
- **THEN** 保存成功且立即生效，不因白名单缺失返回 422

### Requirement: 回调鉴权密钥遮蔽

回调鉴权密钥（如 aria2 webhook secret、NasTools webhook token）SHALL 视同敏感凭据：GET 设置接口不得明文回显（以 `***` 占位），前端凭据表单对应字段 SHALL 以密码框/「已配置」形式呈现，保存时留空表示不修改。

#### Scenario: 回调密钥不回显明文
- **WHEN** 管理员打开设置页查看服务凭据
- **THEN** 回调鉴权密钥字段显示 `***` 占位（或「已配置」），不返回明文

#### Scenario: 回调密钥留空不修改
- **WHEN** 凭据表单中回调鉴权密钥字段留空保存
- **THEN** 已配置的密钥保持不变，不被空值覆盖