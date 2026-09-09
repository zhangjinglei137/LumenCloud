# Tasks: settings-credentials-ui

## 1. 前端：清除按钮位置与必填文案

- [x] 1.1 SettingsView.vue `.cred-field` 改 flex 布局，清除按钮移入输入框同一行（紧跟输入框后；quark「验证 folderId」保留同字段内），验证清除按钮与输入框同行且窄屏不挤压
- [x] 1.2 settingsMeta.ts 精简 default 文案：含「必填（否则…）」的字段统一为「必填」、可选字段为「可选」短标签（长描述保留在 desc），验证设置页必填标签简洁
- [x] 1.3 前端测试补充：必填标签渲染、清除按钮位置结构用例，验证 `vitest` 通过

## 2. 移除废弃配置项

- [x] 2.1 删除 settingsMeta.ts 中 `scan_interval_minutes` 条目，验证设置页不再出现「扫描间隔（分钟）（已废弃）」
- [x] 2.2 backend/app/routers/settings.py editable_keys 白名单移除 `scan_interval_minutes`，验证该键不再可编辑且其余键不受影响
- [x] 2.3 全仓 grep 确认「已废弃，不再生效」展示文案无残留（保留 media 模型列与详情页 per-media 字段），验证 grep 干净

## 3. 验证

- [x] 3.1 手动验证设置页：无废弃项、必填标签简洁、清除按钮跟随输入框、保存/清除凭据功能正常，验证 `npm run build` 与后端启动无报错
