# Tasks: emby-library-all-poster

## 1. 后端：全部类型空列表根因与修复

- [ ] 1.1 诊断全部场景空列表：核查 library_id 归属、ParentId 解析（服务端 /Items 调用）、IncludeItemTypes 映射、错误归一路径，定位空返回根因并记录诊断结论
- [ ] 1.2 services/emby.py 修复全部聚合查询：确保 mixed 等 collection_type 库条目被聚合（必要时调整 list_library 的 IncludeItemTypes 默认与 ParentId 解析），验证对真实 Emby 环境「全部」返回非空
- [ ] 1.3 补充后端测试：全部聚合去重、mixed 库收录、Emby 不可达/未配置错误映射，验证 `pytest` 通过

## 2. 后端：Emby 封面代理

- [ ] 2.1 services/poster.py + routers/poster.py 扩展代理支持 Emby 封面（校验 emby Image 参数合法性、复用登录态与 SSRF 防护、进程内 TTL + Cache-Control），验证代理返回图片且非法参数被拒
- [ ] 2.2 services/emby.py 封面 URL 生成改造：Emby 条目 poster_url 改为后端代理地址（不再内嵌 api_key），验证返回地址为代理格式且无 api_key 泄露
- [ ] 2.3 补充测试：代理合法/非法路径、登录态要求、api_key 不外泄，验证 `pytest` 通过

## 3. 前端：封面加载切换

- [ ] 3.1 frontend/src/utils/poster.ts 或工具函数支持 Emby 代理 URL 组装（复用现有 poster 工具约定），验证 URL 生成正确
- [ ] 3.2 EmbyLibraryView.vue 封面 img src 切换为代理地址并保留加载失败兜底逻辑，验证封面经代理显示、失败时占位不破版
- [ ] 3.3 前端测试补充：Emby 封面代理 URL 生成与兜底，验证 `vitest` 通过

## 4. 集成验证

- [ ] 4.1 真实 Emby 环境验证：全部类型列表非空、封面经代理加载且刷新无大量并发回源；`npm run build` 通过