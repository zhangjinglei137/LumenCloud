## Purpose

为系统设置提供设置项生命周期管理能力：识别并移除已废弃配置项（如下载队列最大并发数），保证设置页仅展示当前生效的设置项，避免残留引用干扰使用与维护。

## ADDED Requirements

### Requirement: 废弃设置项从设置页移除

系统 SHALL 不在设置页展示已废弃且失效的设置项（下载队列最大并发数 `download_queue_max_concurrent`）；前端设置元数据中 SHALL 不再包含该废弃项定义。

#### Scenario: 设置页不含废弃项
- **WHEN** 用户打开设置 → 业务参数
- **THEN** 页面不展示「下载队列最大并发数」相关条目

### Requirement: 废弃键不再参与读写与透传

系统 SHALL 不再读取、写入或透传已废弃配置键（download_queue_max_concurrent）；代码中不得残留对该键的引用。

#### Scenario: 废弃键无代码引用
- **WHEN** 检索代码中 download_queue_max_concurrent 引用
- **THEN** 除存量数据兼容与文档说明外无引用

### Requirement: 存量数据不误伤

已经从 system_config 写入过废弃键值的存量数据 SHALL 被保留不受影响，不做破坏式删除。

#### Scenario: 存量键值保留
- **WHEN** 系统升级后旧 system_config 中已有该键值
- **THEN** 该键值保留在存储中，不影响其他设置读写