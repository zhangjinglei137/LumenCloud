## Purpose

为影视详情页提供清晰的信息层级与高效的集数浏览：移除转存队列区块，大小与巡检设置并排置于 detail-header 最右侧，集数状态获得最大显示空间，集数按每 100 集分组导航并可查看 TMDB 集名称。

## ADDED Requirements

### Requirement: 移除转存队列区块

影视详情页 SHALL 不再展示转存队列区块；转存相关信息由巡检/下载任务队列页面承载。

#### Scenario: 详情页无转存队列
- **WHEN** 用户打开影视详情
- **THEN** 页面不再显示转存队列区块

### Requirement: 大小与巡检设置并排于 header 最右侧

影视详情页的「大小」与「巡检设置」控件 SHALL 并排展示在 lc-panel detail-header 的最右侧。

#### Scenario: header 布局
- **WHEN** 用户打开影视详情
- **THEN** 大小与巡检设置并排位于 detail-header 最右侧，不与其他控件混杂

### Requirement: 集数状态最大显示

影视详情页集数状态区域 SHALL 获得最大显示空间（横向/纵向优先），以完整展示单集状态列表。

#### Scenario: 集数状态占主导
- **WHEN** 用户查看影视详情
- **THEN** 集数状态区占用主要显示区域，信息完整可见

### Requirement: 集数按 100 集分组导航

剧集集数 SHALL 按每 100 集一组生成分组 tag（如 350 集 → 1-100 / 101-200 / 201-300 / 301-350）；用户选择某分组 tag 后，集数状态区 SHALL 只展示该范围内的集数。电影不分组。

#### Scenario: 生成分组 tag
- **WHEN** 某剧集共 350 集
- **THEN** 详情页显示 1-100 / 101-200 / 201-300 / 301-350 四个分组 tag

#### Scenario: 按分组过滤展示
- **WHEN** 用户选择 101-200 分组 tag
- **THEN** 集数状态区仅展示 101-200 集的单集行

#### Scenario: 电影不分组
- **WHEN** 影视类型为电影
- **THEN** 不显示分组 tag，集数状态区直接展示

### Requirement: 展示 TMDB 集名称

集数状态行 SHALL 展示对应集的 TMDB 名称（来自集信息缓存）；无名称时回退为集号（SxxExx），不展示空字段。

#### Scenario: 展示集名称
- **WHEN** 某集存在 TMDB 集信息且含名称
- **THEN** 该集状态行展示名称与集号

#### Scenario: 无名称回退
- **WHEN** 某集无 TMDB 名称或集信息缓存为空
- **THEN** 该集状态行仅展示集号，不报错
