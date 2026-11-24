#!/usr/bin/env python3
"""归档核对：按模板「活动目录」的 15 项清单核对一个活动目录。

清单写死成常量 CHECKLIST（1..14），第 15 项「其他」恒为不适用，
在 check() 里补进结果。真实活动包的命名差异（04-/10- 有同名文档和资料目录、
报销单旧编号 21-）都体现在 kind 与 prefixes 上：

- kind=file        前缀命中的文件
- kind=xlsx        前缀命中的 .xlsx 文件（09-/21- 都认）
- kind=docx        前缀命中的 .docx 文件（04- 的资料目录不算）
- kind=dir_or_file 前缀命中的目录或文件（签到表两种形态都有）
- kind=dir_with_media  前缀命中的目录，且目录里有图片
- kind=dir_with_file   前缀命中的目录，且目录里有 pdf/图片等任何文件
- kind=dir_nonempty    前缀命中的目录，且目录非空
- kind=signed_photo    必须签字后拍照/扫描：认 jpg/jpeg/png/pdf，
                       只有未签字 docx 时判 partial（待补），不是缺
"""
from __future__ import annotations

from . import ylock as _ylock

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .ingest import read_activity_yaml

CHECKLIST = [
    {"no": 1, "name": "活动计划", "prefix": "01-", "kind": "file"},
    {"no": 2, "name": "活动通知", "prefix": "02-", "kind": "file"},
    {"no": 3, "name": "签到表",   "prefix": "03-", "kind": "signed_photo"},
    {"no": 4, "name": "满意度测评汇总表", "prefix": "05-", "kind": "file"},
    {"no": 5, "name": "总结报告", "prefix": "06-", "kind": "file"},
    {"no": 6, "name": "简报",     "prefix": "07-", "kind": "file"},
    {"no": 7, "name": "决算表",   "prefix": "08-", "kind": "file"},
    {"no": 8, "name": "监督记录表", "prefix": "09-", "kind": "signed_photo"},
    {"no": 9, "name": "报销单",   "prefixes": ["09-", "21-"], "kind": "xlsx"},
    {"no": 10, "name": "物资照片", "prefix": "10-", "kind": "dir_with_media"},
    {"no": 11, "name": "活动照片", "prefix": "11-", "kind": "dir_with_media"},
    {"no": 12, "name": "发票照片", "prefix": "12-", "kind": "dir_with_file"},
    {"no": 13, "name": "其他佐证", "prefix": "13-", "kind": "dir_nonempty"},
    {"no": 14, "name": "满意度测评表", "prefix": "04-", "kind": "signed_photo"},
]

# 第 15 项「其他」：没有命中规则，恒为不适用
OTHER_ITEM = {"no": 15, "name": "其他"}

# 清单项 → 可能的文件名词根。带序号前缀是规范写法（01-计划书.docx），
# 但 Web 界面按模板原名生成的是「计划书.docx」，真实活动目录里两种并存，
# 且前缀后还可能带活动名（01-20261105xx活动计划书.docx）。
# 所以查找顺序是：先按 前缀+关键词 精确找，再退到「无前缀的关键词」。
# 注意：键必须与 CHECKLIST 的 name 完全一致，否则 _loose_find 查不到词根
# （2026-11-24 实测：缺「签到表」，导致误传位置的签到表被判 missing）。
_NAME_WORDS = {
    "活动计划": ("计划书", "活动计划"),
    "活动通知": ("活动通知", "通知"),
    "签到表": ("签到表", "签到"),
    "满意度测评汇总表": ("满意度测评汇总表", "满意度"),
    "总结报告": ("总结报告",),
    "简报": ("简报",),
    "决算表": ("决算表",),
    "监督记录表": ("监督记录", "监督"),
    "报销单": ("报销单",),
    # 「物资照片」这一项：用户常把「物资领取表」「物品领取表」当作物料证明传上来
    "物资照片": ("物资", "物品", "领取表"),
    "活动照片": ("照片", "图片"),
    "发票照片": ("发票",),
    "其他佐证": ("佐证", "证明", "资质"),
    "满意度测评表": ("满意度",),
    # 项目级资料用（不在 15 项内，但 loose_find 可能被复用）
    "物资领取表": ("物资领取",),
    "劳务领取表": ("劳务领取",),
}


def _item_prefixes(item: dict) -> tuple[str, ...]:
    """清单项的文件名序号前缀（可能多个，也可能是空）。"""
    pre = item.get("prefixes")
    if pre:
        return tuple(str(x) for x in pre)
    one = item.get("prefix")
    return (str(one),) if one else ()


def find_first_file(act_dir: Path, item_name: str,
                    exts: tuple[str, ...] = (".docx",)) -> Path | None:
    """活动目录里按清单项找第一个文件，兼容三种真实命名。

    1. `01-计划书-xx.docx`（规范：序号前缀 + 关键词 + 活动名）
    2. `01-20261105xx活动计划书.docx`（前缀 + 日期活动名 + 关键词）
    3. `计划书.docx`（Web 按模板原名生成，无前缀）

    找不到返回 None。多个命中取字典序第一个（与原有行为一致）。
    """
    act = Path(act_dir)
    if not act.is_dir():
        return None
    item = next((it for it in CHECKLIST if it.get("name") == item_name), None)
    if item is None:
        return None
    words = _NAME_WORDS.get(item_name, (item_name,))
    prefixes = _item_prefixes(item)

    def _pick(patterns: list[str]) -> Path | None:
        hits: list[Path] = []
        for pat in patterns:
            for ext in exts:
                hits.extend(act.glob(f"{pat}*{ext}" if pat.endswith("-") else f"{pat}*{ext}"))
        hits = sorted({p for p in hits if p.is_file()})
        return hits[0] if hits else None

    # 1) 前缀 + 关键词（01-计划书*、06-总结报告*、08-决算表*）
    for pre in prefixes:
        for w in words:
            hit = _pick([f"{pre}{w}"])
            if hit:
                return hit
    # 2) 前缀 + 任意（01-20261105xx活动计划书 —— 关键词在前缀之后）
    for pre in prefixes:
        hit = _pick([pre])
        if hit:
            return hit
    # 3) 无前缀（计划书.docx）
    for w in words:
        hit = _pick([w])
        if hit:
            return hit
    return None

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# 签字件只认拍照 / 扫描常见格式；gif/webp 不作为签字依据
SIGNED_EXT = {".jpg", ".jpeg", ".png", ".pdf"}
ANY_FILE_EXT = MEDIA_EXT | {".pdf", ".docx", ".xlsx", ".doc", ".txt", ".md", ".7z", ".zip"}

# 文件名 / 目录名里出现这个词，视为该项不适用
NA_MARK = "不适用"

STATUS_TEXT = {"ok": "齐", "partial": "待补", "missing": "缺", "na": "不适用"}

# 核对记录时间统一用东八区（与 activity.yaml 的 imported_at 一致）
CST = timezone(timedelta(hours=8))

# activity.yaml 里签字扫描件允许的后缀（与 SIGNED_EXT 对齐）
SCAN_EXTS = SIGNED_EXT | MEDIA_EXT


def _prefixes(item: dict) -> list[str]:
    """一项可能有多个编号（报销单 09-/21-）。"""
    return list(item.get("prefixes") or [item["prefix"]])


def _entries(dir_path: Path) -> list[Path]:
    """活动目录下的一级条目，跳过隐藏文件，按名字排序保证结果稳定。"""
    return sorted(
        (p for p in dir_path.iterdir() if not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def _files_under(directory: Path, exts: set[str]) -> list[Path]:
    """递归取目录下指定后缀的文件；后缀比较不区分大小写。"""
    return [p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() in exts]


def _all_files_under(directory: Path) -> list[Path]:
    """递归取目录下所有文件（判断「非空」用，不限后缀）。"""
    return [p for p in directory.rglob("*") if p.is_file()]


def _visible_files_under(directory: Path) -> list[Path]:
    """递归取目录下所有文件，跳过 .trash 等隐藏目录里的内容。"""
    out: list[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(directory).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        out.append(path)
    return out


def _rel(path: Path, act_dir: Path) -> str:
    """报告里用相对活动目录的路径，便于直接定位。"""
    try:
        return path.relative_to(act_dir).as_posix()
    except ValueError:
        return path.name


def _match_signed_photo(item: dict, act_dir: Path,
                        entries: list[Path]) -> tuple[Path | None, str, str]:
    """签字类材料：照片/PDF 算齐，只有未签字 docx 算待补（partial）。

    第 14 项（满意度测评表）规则不同：04- 目录外的 docx 不算；
    照片/PDF 只要文件名含「满意度」或位于 04- 目录下即可。
    其余（03- 签到表、09- 监督记录表）按前缀取目录或文件。
    """
    signed: list[Path] = []
    unsigned: list[Path] = []

    if item["no"] == 14:
        files = _visible_files_under(act_dir)
        for path in files:
            name_ok = "满意度" in path.name
            dir_ok = any(part.startswith("04-")
                         for part in path.relative_to(act_dir).parts[:-1])
            if not (name_ok or dir_ok):
                continue
            suffix = path.suffix.lower()
            if suffix in SIGNED_EXT:
                signed.append(path)
            elif suffix == ".docx":
                unsigned.append(path)
    else:
        prefixes = _prefixes(item)
        for hit in entries:
            if not any(hit.name.startswith(pre) for pre in prefixes):
                continue
            if hit.is_dir():
                for path in _visible_files_under(hit):
                    suffix = path.suffix.lower()
                    if suffix in SIGNED_EXT:
                        signed.append(path)
                    elif suffix == ".docx":
                        unsigned.append(path)
            elif hit.is_file():
                suffix = hit.suffix.lower()
                if suffix in SIGNED_EXT:
                    signed.append(hit)
                elif suffix == ".docx":
                    unsigned.append(hit)

    if signed:
        return signed[0], f"命中签字扫描件 {_rel(signed[0], act_dir)}", "ok"

    if unsigned:
        name = _rel(unsigned[0], act_dir)
        if item["no"] == 8:
            return None, f"仅有未签字 docx：{name}，缺签字扫描件", "partial"
        return None, f"有 {name}，缺签字扫描件", "partial"

    # 规范位置没找到：放宽到全目录按关键词找（用户常把扫描件误传进别的目录）
    loose_signed = _loose_find(item, act_dir, SIGNED_EXT)
    if loose_signed:
        return (loose_signed[0],
                f"命中 {_rel_label(loose_signed[0], act_dir)}"
                f"（位置不规范，建议改名 {_prefixes(item)[0]}{item['name']}）", "ok")

    if item["no"] == 14:
        return None, "未找到含「满意度」的签字照片/PDF", "missing"
    return None, f"未找到 {'/'.join(_prefixes(item))} 开头的签字照片/PDF", "missing"


def _loose_find(item: dict, act_dir: Path,
                kinds: tuple[str, ...]) -> list[Path]:
    """规范位置没找到时的兜底：全目录按关键词找，不限位置。

    用户会把材料传到别的目录里（实测 2026-11-24：签到表.pdf 和
    物品领取表.pdf 都被放进 11-活动照片资料/），按「根目录+序号前缀」
    硬判就会误报缺失。这里放宽到「只要在活动目录里、名对得上就算」，
    但结果带「位置不规范」标记，提醒归档结构该整理。
    """
    words = _NAME_WORDS.get(item["name"], ())
    if not words:
        return []
    # .md 是 Obsidian 骨架（由 docx 转出），不是原始材料，绝不能当凭证
    skip = {".md", ".markdown"}
    item_name = str(item["name"])
    out: list[Path] = []
    for path in _visible_files_under(act_dir):
        suffix = path.suffix.lower()
        if suffix not in kinds or suffix in skip:
            continue
        # 「序号-项名.docx」这种是有人把模板另存了一份，不是实拍材料。
        # 只拦这种格式，不能拦「签到表.pdf」——那是真扫描件。
        if path.stem.startswith(tuple(_item_prefixes(item))) \
                and item_name in path.stem:
            continue
        if any(w in path.name for w in words):
            out.append(path)
    # 原始材料优先：去掉 .md 后可能剩下多个，按「非 .md > .md」排序
    return out


def _rel_label(path: Path, act_dir: Path) -> str:
    """相对活动目录的路径标签，用于「位置不规范」提示。"""
    try:
        return path.relative_to(act_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def _match(item: dict, entries: list[Path],
           act_dir: Path) -> tuple[Path | None, str, str]:
    """按 kind 判定一项是否命中，返回 (命中的路径, 说明, 状态)。

    命中的路径为 None 表示没找到；状态是 ok / partial / missing，
    说明文字直接进报告的「说明」列。na 由 check() 统一覆盖。
    """
    prefixes = _prefixes(item)
    kind = item.get("kind")
    hits = [p for p in entries if any(p.name.startswith(pre) for pre in prefixes)]

    if kind == "signed_photo":
        return _match_signed_photo(item, act_dir, entries)

    if kind == "file":
        found = [p for p in hits if p.is_file()]
        if found:
            return found[0], f"命中 {found[0].name}", "ok"
        loose = _loose_find(item, act_dir, ANY_FILE_EXT)
        if loose:
            return (loose[0], f"命中 {_rel_label(loose[0], act_dir)}"
                              f"（位置不规范，建议放活动根目录）", "ok")
        return None, f"未找到 {'/'.join(prefixes)} 开头的文件", "missing"

    if kind in ("xlsx", "docx"):
        suffix = "." + kind
        found = [p for p in hits if p.is_file() and p.suffix.lower() == suffix]
        if found:
            return found[0], f"命中 {found[0].name}", "ok"
        loose = _loose_find(item, act_dir, (suffix,))
        if loose:
            return (loose[0], f"命中 {_rel_label(loose[0], act_dir)}"
                              f"（位置不规范，建议放活动根目录）", "ok")
        return None, f"未找到 {'/'.join(prefixes)} 开头的 {suffix} 文件", "missing"

    if kind == "dir_or_file":
        if hits:
            first = hits[0]
            name = first.name + ("/" if first.is_dir() else "")
            return first, f"命中 {name}", "ok"
        return None, f"未找到 {'/'.join(prefixes)} 开头的目录或文件", "missing"

    # 以下三种都要求是目录
    dirs = [p for p in hits if p.is_dir()]
    if not dirs:
        # 兜底：用户可能没建 10-/12-/13- 目录，而是把材料散放在别处
        # （2026-11-24：物品领取表.pdf 被放进 11-活动照片资料/）
        loose = _loose_find(item, act_dir, ANY_FILE_EXT)
        if loose:
            return (loose[0], f"命中 {_rel_label(loose[0], act_dir)}"
                              f"（未建 {prefixes[0]} 目录，建议按项建目录归档）", "ok")
        return None, f"未找到 {'/'.join(prefixes)} 开头的目录", "missing"

    if kind == "dir_with_media":
        for directory in dirs:
            media = _files_under(directory, MEDIA_EXT)
            if media:
                return directory, f"命中 {directory.name}/（{len(media)} 个图片）", "ok"
        return None, f"{dirs[0].name}/ 里没有图片", "missing"

    if kind == "dir_with_file":
        for directory in dirs:
            files = _files_under(directory, ANY_FILE_EXT)
            if files:
                return directory, f"命中 {directory.name}/（{len(files)} 个文件）", "ok"
        return None, f"{dirs[0].name}/ 里没有 pdf 或图片", "missing"

    if kind == "dir_nonempty":
        for directory in dirs:
            files = _all_files_under(directory)
            if files:
                return directory, f"命中 {directory.name}/（{len(files)} 个文件）", "ok"
        return None, f"{dirs[0].name}/ 是空目录", "missing"

    raise ValueError(f"未知的核对类型：{kind}")


def _not_applicable(act_dir: Path) -> set[int]:
    """读 activity.yaml 的 not_applicable，返回序号集合；读不了按空集处理。"""
    try:
        data = read_activity_yaml(act_dir)
    except ValueError:
        # yaml 坏了不该阻断核对，按「没有标记」继续
        return set()
    raw = data.get("not_applicable")
    if not isinstance(raw, (list, tuple)):
        return set()
    out: set[int] = set()
    for value in raw:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def check(dir_path: Path) -> dict:
    """核对一个活动目录，返回逐项结果 + 计数 + 是否完整。

    - 找到签字照片/PDF → ok
    - 只有未签字 docx → partial（待补，不算齐也不算完全缺）
    - 没找到 → missing
    - 序号在 activity.yaml 的 not_applicable 里，或命中的条目名含「不适用」→ na
    """
    act_dir = Path(dir_path)
    if not act_dir.is_dir():
        raise FileNotFoundError(f"活动目录不存在：{act_dir}")

    entries = _entries(act_dir)
    na_nos = _not_applicable(act_dir)
    items: list[dict] = []

    for item in CHECKLIST:
        found, detail, match_status = _match(item, entries, act_dir)
        marked = any(NA_MARK in p.name for p in entries
                     if any(p.name.startswith(pre) for pre in _prefixes(item)))
        if item["no"] in na_nos:
            status, detail = "na", "activity.yaml 标记不适用"
        elif marked:
            status, detail = "na", "文件名标注不适用"
        else:
            status = match_status
        items.append({"no": item["no"], "name": item["name"],
                      "status": status, "detail": detail})

    # 第 15 项没有命中规则，恒为不适用
    items.append({"no": OTHER_ITEM["no"], "name": OTHER_ITEM["name"],
                  "status": "na", "detail": "默认不适用"})

    ok_count = sum(1 for it in items if it["status"] == "ok")
    partial_count = sum(1 for it in items if it["status"] == "partial")
    missing_count = sum(1 for it in items if it["status"] == "missing")
    na_count = sum(1 for it in items if it["status"] == "na")
    return {
        "items": items,
        "ok_count": ok_count,
        "partial_count": partial_count,
        "missing_count": missing_count,
        "na_count": na_count,
        "complete": partial_count == 0 and missing_count == 0,
    }


def read_reviews(act_dir: Path) -> dict[str, dict]:
    """读 activity.yaml 的 reviews 字段，返回 {序号字符串: 记录}。缺失/格式错返回 {}。"""
    try:
        data = read_activity_yaml(act_dir)
    except ValueError:
        return {}
    raw = data.get("reviews")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def merge_review_status(result: dict, reviews: dict) -> dict:
    """把 reviews 合并进 check() 的返回。

    每个 item 增加：
      review: {"reviewed_by":..., "reviewed_at":..., "reviewed_note":...} | None
      signed: bool
    result 顶层增加：
      reviewed_count  —— 已核对项数
      signed_count    —— 已签字项数
      deliverable     —— ok+partial 覆盖全部「必需项」且全部必需项都已核对
    """
    items = list(result.get("items") or [])
    required: list[dict] = []
    reviewed_count = 0
    signed_count = 0
    for item in items:
        rec = reviews.get(str(item.get("no")))
        is_required = item.get("status") != "na"
        if is_required:
            required.append(item)
        if isinstance(rec, dict):
            item["review"] = {
                "reviewed_by": str(rec.get("reviewed_by") or ""),
                "reviewed_at": str(rec.get("reviewed_at") or ""),
                "reviewed_note": str(rec.get("reviewed_note") or ""),
            }
            reviewed_count += 1
            if rec.get("signed"):
                item["signed"] = True
                signed_count += 1
            else:
                item["signed"] = False
        else:
            item["review"] = None
            item["signed"] = False

    # 「签字确认即视为齐备」：用户手工签过字的 partial 项（只有未签字 docx、
    # 缺扫描件），在人工签字后不再卡着活动不完整 —— 但文件本身仍是待上传状态，
    # 单独标 pending_upload 让界面提醒补扫描件（2026-11-23 用户要求）。
    pending_upload = 0
    for it in required:
        # 文件没齐（missing）或只有未签字版（partial），只要人工签过字就归位：
        # 缺的文件本身是待上传状态，不该卡住活动完成度。
        if it.get("status") in ("partial", "missing") and it.get("signed"):
            it["pending_upload"] = True
            pending_upload += 1

    def _effective_status(it: dict) -> str:
        """签字确认过的 missing / partial 等同于 ok（用于完整性与交付判据）。"""
        if it.get("status") in ("partial", "missing") and it.get("signed"):
            return "ok"
        return str(it.get("status") or "")

    covered = sum(1 for it in required
                  if _effective_status(it) in ("ok", "partial"))
    # 「可交付」必须逐项判断：必需项既齐全、又**每一项**都有核对记录。
    # 原来用 reviewed_count >= len(required) 做计数比较，而 reviewed_count
    # 把 na 项也算进去 —— 只核对 13 个必需项 + 第 15 项「其他」（恒为 na）
    # 就能凑够 14，把「可交付」点亮（审计 R-03，已复现）。
    reviewed_required = sum(1 for it in required if it.get("review"))
    deliverable = (len(required) > 0
                   and covered == len(required)
                   and reviewed_required == len(required))

    # 顶层完整性也按「签字确认后的有效状态」重算：
    # 原本 complete 来自 check() 的 partial/missing 计数，签字后仍为 False，
    # 用户点了签字活动依然显示未完成（本次要解决的问题）。
    eff_ok = sum(1 for it in items if _effective_status(it) in ("ok", "partial"))
    eff_partial = sum(1 for it in items
                      if it.get("status") == "partial" and not it.get("signed"))
    eff_missing = sum(1 for it in items
                      if it.get("status") == "missing" and not it.get("signed"))
    result["ok_count"] = eff_ok
    result["partial_count"] = eff_partial
    result["missing_count"] = eff_missing
    result["complete"] = eff_partial == 0 and eff_missing == 0
    result["items"] = items
    result["reviewed_count"] = reviewed_count
    # 必需项的核对数：deliverable 用的就是这个口径，前端展示也用它，
    # 避免前后端两套算法打架（原来前端自己数必需项，会出现
    # 「已核对 13/14」与绿色「可交付」并存的矛盾）。
    result["reviewed_required_count"] = reviewed_required
    result["required_count"] = len(required)
    result["signed_count"] = signed_count
    result["deliverable"] = deliverable
    # 已签字确认但文件还没上传的项数（界面据此提示「待补扫描件」）
    result["pending_upload_count"] = pending_upload
    return result


def _resolve_scan(act_dir: Path, scan: str) -> Path:
    """校验 scan：活动目录内的图片文件，越界/不存在一律 ValueError。"""
    base = Path(act_dir).resolve()
    rel = str(scan or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or any(seg == ".." for seg in rel.split("/")):
        raise ValueError(f"扫描件路径非法：{scan}")
    path = (base / rel).resolve()
    if base not in path.parents:
        raise ValueError(f"扫描件必须在活动目录内：{scan}")
    if not path.is_file():
        raise ValueError(f"扫描件不存在：{scan}")
    if path.suffix.lower() not in SCAN_EXTS:
        raise ValueError(
            f"扫描件只能是图片/PDF：{path.suffix.lower() or '无后缀'}")
    return path


def save_review(act_dir: Path, item_no: int, *,
                reviewed_by: str, note: str = "",
                signed: bool = False, scan: str = "",
                clear: bool = False) -> dict:
    """写/清 activity.yaml 的 reviews[序号]，原子写并保留其余字段。

    - item_no 必须在 CHECKLIST 里（含第 15 项），否则 ValueError
    - scan 传了就必须是活动目录内已存在的图片/PDF，否则 ValueError
    - signed=True 不要求 scan（人工点击确认，扫描件后补）
    - clear=True 删掉该序号的记录
    """
    from .ingest import ACTIVITY_YAML

    act = Path(act_dir)
    valid_nos = {item["no"] for item in CHECKLIST} | {OTHER_ITEM["no"]}
    try:
        no = int(item_no)
    except (TypeError, ValueError):
        raise ValueError(f"核对序号非法：{item_no}")
    if no not in valid_nos:
        raise ValueError(f"核对序号超出范围 1-{OTHER_ITEM['no']}：{no}")

    # scan 的校验在锁外先做（只读文件系统，不碰 yaml）
    scan_rel = ""
    if not clear and str(scan or "").strip():
        scan_rel = _resolve_scan(act, scan).relative_to(act.resolve()).as_posix()

    def _mutate(data: dict) -> dict:
        raw = data.get("reviews")
        reviews = {str(k): v for k, v in raw.items()
                   if isinstance(v, dict)} if isinstance(raw, dict) else {}
        if clear:
            reviews.pop(str(no), None)
        else:
            now = datetime.now(CST).isoformat(timespec="seconds")
            record = {
                "reviewed_by": str(reviewed_by or "").strip(),
                "reviewed_at": now,
                "reviewed_note": str(note or "").strip(),
                "signed": bool(signed),
            }
            if signed:
                record["signed_at"] = now
            if scan_rel:
                record["scan"] = scan_rel
            reviews[str(no)] = record
        if reviews:
            data["reviews"] = {k: reviews[k] for k in sorted(reviews, key=int)}
        else:
            data.pop("reviews", None)
        return data

    # 整个「读 yaml → 改 reviews → 写回」在活动锁内完成。
    # 原来无锁 + 固定 .tmp 名：并发写会让后写者整段覆盖 reviews（丢记录），
    # 或撞同一临时文件报 FileNotFoundError（审计 R-02，实测 8 线程只剩 3 条）。
    written = _ylock.update_yaml(act / ACTIVITY_YAML, _mutate, lock_dir=act)
    return (written.get("reviews") or {}).get(str(no), {})


def _cell(text: str) -> str:
    """表格单元格转义：竖线会把 Markdown 表格撑破。"""
    return str(text).replace("|", "\\|")


def to_markdown(result: dict) -> str:
    """核对结果转 Markdown：明细表 + 汇总行，方便直接贴进报告。"""
    lines = [
        "| 序号 | 材料 | 状态 | 说明 |",
        "|---|---|---|---|",
    ]
    for item in result["items"]:
        lines.append("| {} | {} | {} | {} |".format(
            item["no"], _cell(item["name"]),
            STATUS_TEXT.get(item["status"], item["status"]),
            _cell(item["detail"]),
        ))
    verdict = "完整" if result["complete"] else "未完整"
    lines.append("")
    lines.append(f"**汇总**：齐 {result['ok_count']} 项、"
                 f"待补 {result.get('partial_count', 0)} 项、"
                 f"缺 {result['missing_count']} 项、"
                 f"不适用 {result['na_count']} 项；{verdict}")
    return "\n".join(lines) + "\n"
