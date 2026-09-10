## Purpose

为 Emby 影视库提供按媒体库类型（CollectionType）分类的展示能力：获取媒体库列表（含类型），按媒体库查询库内影片/剧集条目，前端去分页浏览，分类准确反映 Emby 真实库结构。

## ADDED Requirements

### Requirement: 获取媒体库列表含类型

系统 SHALL 通过 Emby 官方接口（`GET /Library/MediaFolders`）获取媒体库列表，返回每个媒体库的名称、Id、CollectionType（movies/tvshows/mixed 等）与动漫标记 is_anime；tvshows 媒体库的 is_anime SHALL 由后端按库级关键词识别（库名含动漫关键词）；Emby 服务不可用时返回约定的 503 错误码（emby_not_configured / emby_unreachable）。

#### Scenario: 成功获取媒体库列表
- **WHEN** 用户打开 Emby 影视库且 Emby 服务可用
- **THEN** 接口返回媒体库列表，每项含名称、Id、CollectionType、is_anime

#### Scenario: 动漫库标记
- **WHEN** Emby 存在 tvshows 媒体库且库名含动漫关键词（动漫/动画/anime）
- **THEN** 该库的 is_anime 为 true；非 tvshows 媒体库的 is_anime 恒为 false

#### Scenario: 白名单过滤剧集库
- **WHEN** emby_series_library_ids 白名单配置非空
- **THEN** 媒体库列表仅返回白名单内的 tvshows 库，非 tvshows 库不受影响

#### Scenario: Emby 未配置
- **WHEN** EMBY_BASE_URL / EMBY_API_KEY 未配置
- **THEN** 返回 503 且 code=emby_not_configured，前端呈现「未配置空态」

#### Scenario: Emby 不可达
- **WHEN** Emby 网络故障或非 2xx 响应
- **THEN** 返回 503 且 code=emby_unreachable，前端呈现「不可达错误态」

### Requirement: 按媒体库类型查询库内条目

系统 SHALL 按所选媒体库查询库内条目：客户端以必选的媒体库 Id（library_id）作为 ParentId 调用 Emby `/Items` 接口，按所选类型映射 IncludeItemTypes（movie→Movie，series→Series，缺省 Movie,Series）获取影片/剧集；查询结果供前端展示与「加入订阅」。

#### Scenario: 按电影库查询影片
- **WHEN** 用户选择 CollectionType=movies 的媒体库并以 movie 类型查询
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Movie 查询并返回影片条目列表

#### Scenario: 按剧集库查询剧集
- **WHEN** 用户选择 CollectionType=tvshows 的媒体库并以 series 类型查询
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Series 查询并返回剧集条目列表

#### Scenario: 缺省类型查询全部
- **WHEN** 用户查询混合库（mixed/null）且不指定类型
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Movie,Series 查询并返回全部条目

#### Scenario: 剧集状态过滤
- **WHEN** 用户筛选在更/完结剧集
- **THEN** 系统以 SeriesStatus=Continuing|Ended 过滤剧集条目

### Requirement: 影视库列表去分页

Emby 影视库条目列表 SHALL 一次性返回全部匹配条目，不进行分页；前端展示全部结果。

#### Scenario: 全量展示
- **WHEN** 用户查看某媒体库条目
- **THEN** 列表展示全部条目，无分页控件，无需翻页

### Requirement: 分类准确反映媒体库类型

前端 Emby 影视库分类 SHALL 基于媒体库真实 CollectionType 与库级识别，而非仅依赖库名关键词或固定类型猜测；动漫库识别以库级识别为准（库名关键词作为辅助）。

#### Scenario: 按 CollectionType 分类
- **WHEN** 用户浏览 Emby 影视库
- **THEN** 分类标签与真实媒体库 CollectionType 对应（电影库/剧集库），不把剧集误归电影或反之

#### Scenario: 混合库归「全部」
- **WHEN** Emby 存在 CollectionType=mixed 或 null 的混合媒体库
- **THEN** 混合库条目归入「全部」分类展示，不进入电影/剧集分类

#### Scenario: 动漫库识别
- **WHEN** Emby 存在动漫媒体库（is_anime=true）
- **THEN** 动漫分类正确识别该库，不因固定类型猜测而错分
