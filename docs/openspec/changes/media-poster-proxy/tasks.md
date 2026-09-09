## 1. 后端海报代理端点

- [x] 1.1 新增 GET /api/poster 路由：校验 p 参数必须以 /t/p/ 开头且无 ../ 与协议段，非法返回 400；验证新增代理端点单测（合法路径/非法路径/SSRF 向量）通过
- [x] 1.2 代理回源逻辑：httpx 拉取官方图床，成功返回图片 + Content-Type + Cache-Control max-age；失败返回 502 并节流告警；验证回源成功/失败用例通过

## 2. 图床镜像配置

- [x] 2.1 新增配置键 tmdb_poster_proxy（config_store 优先，env TMDB_POSTER_PROXY 兜底），复用 tmdb_proxy 的误填防御校验；验证配置读写与防御校验用例通过
- [x] 2.2 代理端点支持镜像地址（配置后从镜像拉取，未配置回退官方）；验证镜像/回退切换用例通过

## 3. 前端统一走代理

- [x] 3.1 新增 utils/poster.ts 封装 posterUrl(path) 返回 /api/poster?p=...；验证单元测试覆盖编码与 null 处理
- [ ] 3.2 替换全部使用点：MediaListView（卡片+表格）、MediaDetailView、TmdbSearch、MediaAddView、ApprovalsView、EmbyLibraryView 订阅场景；验证前端 npm run build 通过
- [ ] 3.3 保留海报加载失败兜底（imgErrors 标题占位）；验证手工场景（代理不可达时不破页面）

## 4. 缓存与性能

- [ ] 4.1 可选内存 TTL 缓存（条目上限 + 过期），高频列表页减少回源；验证缓存命中/过期用例通过
- [ ] 4.2 全量后端测试 + 前端构建通过；端到端验证影视库海报经代理正常显示