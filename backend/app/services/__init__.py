"""
LumenCloud 服务层
==================
外部服务接入的统一层（httpx async，不阻塞事件循环）：

- tmdb        ：影视元数据搜索
- emby        ：防重基线 / 遗漏集 / 已有集查询
- cloudsaver  ：网盘搜源 / 分享信息 / 转存（token 仅驻留内存）
- alist       ：挂载目录 / 直链 / 释放
- aria2       ：下载器（后续阶段实现）
- nastools    ：目录同步（后续阶段实现）
- pushplus    ：PushPlus 推送（后续阶段实现）
- notifier    ：通知抽象（站内 + PushPlus 可选 + 链式）
- capacity    ：夸克中转空间容量判断（占位 fail-closed）

设计依据：docs/新系统设计.md §7（通知抽象）、§10（外部服务接入表）、§6.2（容量 fail-closed）。
"""
