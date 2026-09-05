"""
LumenCloud 阶段 1 Q2 容量实证脚本（10G 空间转存 >10G 测试源，验证软超额/硬上限 → 模型 A/B）

用法：
  # 只读探测（推荐先跑）：登录 → 搜索候选 >10G 分享源 → 读当前容量 → 不转存
  python scripts/probe_capacity.py --dry-run

  # 实测（D14：用户发令 + 确认后才可执行）：转存 → 读容量 → 立即删除 → 报告
  python scripts/probe_capacity.py --run --share-code <code> [--file-name <名>] [--folder-id <夸克目标目录>]

护栏：
  - 凭据全部走环境变量（backend/.env），脚本不落任何凭据
  - 实测必须 --run 显式传入 --share-code，杜绝误触
  - 转存成功 → 立即读取容量 → 立即删除释放（alist /api/fs/remove）
  - 删除失败 → 打印人工清理指引并返回非 0
  - 全程写 task 到 stdout，敏感字段脱敏（stoken 仅示前 8 位）
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# 允许从任意 cwd 运行：backend 目录加入 sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.services import alist, cloudsaver  # noqa: E402

_GB = 1024 ** 3
TARGET_MIN_GB = 15.0  # 目标测试源最小大小（>10G 配额即可，贴近设计文档「15G 电影」）


def mask(s: str, n: int = 8) -> str:
    return s[:n] + "..." if s else "(空)"


async def read_usage_gb() -> tuple[float, str]:
    """容量读取（Q1 选型：alist /quark 目录统计）。返回 (used_gb, source)。"""
    try:
        files = await alist.list_dir("/quark")
        used = sum(int(f.get("size") or 0) for f in files if not f.get("is_dir")) / _GB
        return used, "alist"
    except Exception as exc:  # noqa: BLE001
        print(f"[容量] alist 统计失败: {exc}（fail-closed：不转存）")
        raise


async def find_candidates() -> list[dict]:
    """搜索并收集 >15G 的候选分享源（只读）。返回 [{share_code, name, size_gb, is_folder, extra}]。"""
    candidates: list[dict] = []
    for keyword in ["亮剑", "仙逆", "凡人修仙传", "一人之下"]:
        print(f"\n[搜索] 关键词: {keyword}")
        try:
            results = await cloudsaver.search(keyword)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 搜索失败: {exc}")
            continue
        quark_codes = []
        for item in results:
            for cl in item.get("cloud_links", []):
                if (cl.get("cloud_type") or "").lower() == "quark" and cl.get("link"):
                    code = str(cl["link"]).rstrip("/").split("/")[-1]
                    if code and code not in quark_codes:
                        quark_codes.append(code)
        print(f"  展开 quark 分享码: {len(quark_codes)} 个")

        for code in quark_codes[:10]:
            try:
                info = await cloudsaver.share_info(code)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⏭️ share-info {code} 失败: {exc}")
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)

            # share-info 自带文件列表优先；否则 share-list
            file_list = info.get("list") or []
            if not file_list:
                try:
                    data = await cloudsaver.share_list(
                        code,
                        pwd_id=info.get("pwdId") or "",
                        stoken=info.get("stoken") or "",
                    )
                    file_list = data.get("list") or []
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⏭️ share-list {code} 失败: {exc}")
                    continue

            # 顶层 fileSize（整个分享总大小）≥15G → 整个分享作为候选（文件夹结构常见）
            top_size_gb = int(info.get("fileSize") or 0) / _GB
            if top_size_gb >= TARGET_MIN_GB:
                cand = {
                    "share_code": code,
                    "name": f"[整个分享] fileSize={top_size_gb:.2f}G",
                    "size_gb": round(top_size_gb, 2),
                    "is_folder": True,
                    "fids": info.get("fids"),
                    "fid_tokens": info.get("fidTokens"),
                    "stoken": info.get("stoken"),
                    "pwd_id": info.get("pwdId"),
                }
                candidates.append(cand)
                print(f"  ✅ 候选(整分享): [{code}] fileSize={top_size_gb:.2f}G")

            for f in file_list:
                name = f.get("fileName") or f.get("name") or ""
                size = int(f.get("size") or f.get("fileSize") or 0)
                is_folder = bool(f.get("isFolder") or f.get("is_folder"))
                size_gb = size / _GB
                if size_gb >= TARGET_MIN_GB:
                    cand = {
                        "share_code": code,
                        "name": name,
                        "size_gb": round(size_gb, 2),
                        "is_folder": is_folder,
                        "fids": info.get("fids"),
                        "fid_tokens": info.get("fidTokens"),
                        "stoken": info.get("stoken"),
                        "pwd_id": info.get("pwdId"),
                    }
                    candidates.append(cand)
                    print(f"  ✅ 候选: [{code}] {name[:60]} {size_gb:.2f}G {'(文件夹)' if is_folder else ''}")
    return candidates


async def do_transfer(share_code: str, file_name: str, folder_id: str) -> None:
    """实测：转存 → 读容量 → 删除 → 报告（D14 确认后才调用）。"""
    print("=" * 70)
    print("[Q2 实测开始]")
    print("=" * 70)

    # 0. 转存前容量基线
    used0, src0 = await read_usage_gb()
    print(f"[容量] 转存前 used={used0:.2f}G source={src0}")

    # 1. share-info 拿转存凭据
    info = await cloudsaver.share_info(share_code)
    stoken = info.get("stoken") or ""
    fids = info.get("fids")
    fid_tokens = info.get("fidTokens")
    if not fids and isinstance(info.get("list"), list) and info.get("list"):
        # share-info.list[0] 为首层文件/文件夹条目：取其 fileId/fileIdToken 作为转存对象
        first = info["list"][0]
        cand = {"file_id": first.get("fileId"), "file_id_token": first.get("fileIdToken") or ""}
        if cand["file_id"]:
            fids = [cand["file_id"]]
            fid_tokens = [cand["file_id_token"]]
            print(f"[转存] 使用 share-info 首条目: {str(first.get('fileName'))[:50]} isFolder={bool(first.get('isFolder'))}")
    if not fids:
        print("[转存] share-info 未返回 fids/list，改用 share-list 首文件")
        data = await cloudsaver.share_list(share_code, pwd_id=info.get("pwdId") or "", stoken=stoken)
        file_list = data.get("list") or []
        target = next((f for f in file_list if f.get("fileName") == file_name), file_list[0] if file_list else None)
        if not target:
            raise SystemExit("[转存] 未找到目标文件")
        fids = [target.get("fileId")]
        fid_tokens = [target.get("fileIdToken") or ""]
    size_hint = int(info.get("fileSize") or 0) / _GB
    print(f"[转存] share_code={share_code} fids={len(fids)} stoken={mask(stoken)} folder={folder_id} 分享总大小≈{size_hint:.2f}G")

    # 2. 转存（cloudSaver /api/quark/save，receiveCode=stoken 契约）
    params = {
        "fids": fids,
        "fidTokens": fid_tokens,
        "folderId": folder_id,
        "shareCode": share_code,
        "receiveCode": stoken,
    }
    try:
        resp = await cloudsaver.save(params)
        print(f"[转存] 成功: {resp}")
    except Exception as exc:  # noqa: BLE001
        print(f"[转存] 失败: {exc}")
        # 转存失败也可能留下残留 → 仍做残留检查
        await cleanup_quark("转存失败后残留清理")
        raise SystemExit(2)

    # 3. 读容量验证（等 alist 刷新，转存是夸克秒级内转）
    await asyncio.sleep(3)
    used1, src1 = await read_usage_gb()
    print(f"[容量] 转存后 used={used1:.2f}G source={src1}")
    print(f"[判定] 增量 ≈ {used1 - used0:.2f}G | 配额 {settings.QUARK_QUOTA_GB:.0f}G | 软超额可用空间 ≈ {settings.QUARK_QUOTA_GB - used0:.2f}G")

    # 4. 立即删除释放（D14：测完即删）
    print("[清理] 立即删除测试源...")
    await cleanup_quark("测试后立即删除")

    # 5. 终态
    used2, _ = await read_usage_gb()
    print(f"[容量] 删除后 used={used2:.2f}G（应回落到 ~{used0:.2f}G）")
    print("[Q2 实测结束] 请将以上数据记入 docs/阶段1验证报告.md")


async def cleanup_quark(reason: str) -> None:
    """删除 /quark 全部文件（测试源 + 残留）。删除失败 → 人工指引 + 非 0。"""
    try:
        files = await alist.list_dir("/quark")
        names = [f.get("name") for f in files if not f.get("is_dir")]
        if not names:
            print(f"[清理] {reason}: /quark 已空，无需删除")
            return
        print(f"[清理] {reason}: 删除 {len(names)} 个文件")
        resp = await alist.remove(names, "/quark")
        print(f"[清理] 删除响应: {resp}")
        # 验证
        files2 = await alist.list_dir("/quark")
        remain = [f.get("name") for f in files2 if not f.get("is_dir")]
        if remain:
            raise RuntimeError(f"删除后仍有残留: {remain}")
        print("[清理] 验证通过：/quark 已归零")
    except Exception as exc:  # noqa: BLE001
        print("=" * 60)
        print("[⚠️ 删除失败 — 需要人工清理]")
        print(f"  原因: {exc}")
        print("  人工清理指引（或等 12h 兜底清理）:")
        print("    alist: POST /api/fs/list {path:/quark} 查看残留文件")
        print("    alist: POST /api/fs/remove {names:[...], dir:/quark} 删除")
        print("=" * 60)
        raise SystemExit(3)


async def check_nastools_login() -> None:
    """Q4 顺带：NasTools 登录接口确认（只读，不重启）。"""
    try:
        from app.services import nastools
        # nastools 签名占位，此处直接验证底层登录契约
        import httpx
        base = (settings.NASTOOLS_BASE_URL or "").rstrip("/")
        if not base:
            print("[Q4] NASTOOLS_BASE_URL 未配置，跳过")
            return
        form = {
            "next": "",
            "username": settings.NASTOOLS_USERNAME or "",
            "password": settings.NASTOOLS_PASSWORD or "",
            "remember": "on",
        }
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            resp = await client.post(base + "/", data=form)
        cookie = resp.headers.get("set-cookie") or ""
        has_session = "session=" in cookie
        print(f"[Q4] NasTools 登录: HTTP {resp.status_code}, session cookie={'有' if has_session else '无'} → {'✅' if has_session else '❌'}")
    except Exception as exc:  # noqa: BLE001
        print(f"[Q4] NasTools 登录探测失败（不阻塞 Q2）: {exc}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Q2 容量实证")
    parser.add_argument("--dry-run", action="store_true", help="只读探测候选，不转存")
    parser.add_argument("--run", action="store_true", help="实测（需确认）")
    parser.add_argument("--share-code", default="", help="实测转存的分享码")
    parser.add_argument("--file-name", default="", help="实测转存的目标文件名（可选）")
    parser.add_argument("--folder-id", default="", help="夸克目标目录 folderId（默认取设置/空）")
    args = parser.parse_args()

    if not args.dry_run and not args.run:
        parser.print_help()
        return

    if args.dry_run:
        print("[dry-run] 只读探测开始（不转存）")
        used, src = await read_usage_gb()
        print(f"[容量] 当前 /quark used={used:.2f}G source={src}（配额 {settings.QUARK_QUOTA_GB:.0f}G）")
        cands = await find_candidates()
        print(f"\n[dry-run] 共找到 {len(cands)} 个 ≥{TARGET_MIN_GB:.0f}G 候选")
        print("请人工选择候选 share_code 后，确认后运行:")
        print("  python scripts/probe_capacity.py --run --share-code <code> [--file-name <名>]")
        await check_nastools_login()
        return

    if args.run:
        if not args.share_code:
            raise SystemExit("--run 必须提供 --share-code（D14 护栏）")
        print("[!] 警告：即将对生产夸克账号执行转存写操作（D14 已确认）")
        folder_id = args.folder_id or os.environ.get("QUARK_DEFAULT_FOLDER", "")
        await do_transfer(args.share_code, args.file_name, folder_id)
        return


if __name__ == "__main__":
    asyncio.run(main())
