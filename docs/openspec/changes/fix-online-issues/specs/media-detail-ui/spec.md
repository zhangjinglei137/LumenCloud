## MODIFIED Requirements

### Requirement: 状态下拉完整可见

影视详情页「状态」下拉选择器 SHALL 提供且仅提供「订阅中」（tracking）与「已暂停」（paused）两个选项；后端 media.status 字段 SHALL 仅接受这两个合法值，下载等执行态不得写入该字段；状态下拉选中后所选项文本完整可见，不被截断为仅显示箭头。

#### Scenario: 状态下拉完整显示选项
- **WHEN** 用户在详情页打开「状态」下拉
- **THEN** 下拉仅展示「订阅中」「已暂停」两个选项，且选中项文本完整显示，无截断

#### Scenario: 状态字段仅两值
- **WHEN** 影视详情接口返回 media.status
- **THEN** status 值仅可能为 tracking 或 paused；若历史数据存在其他值（如 download/downloading），系统以中文展示兜底（不原样透传英文），且状态仍按两值语义处理
