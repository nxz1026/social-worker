#!/usr/bin/env python3
"""飞书「未分类」→ 分类 → 转 md → 归档 → Cognee 索引。

流程
  1. lark-cli drive +pull 未分类/ → inbox/
  2. 逐文件：转 md（临时目录）→ 读内容 → LLM 分类（项目 + 类别）
  3. 原件 → sources/projects/<项目>/<类别>/
     md   → vault-<项目>/md/<类别>/<文档名>/<文档名>.md（顶部插原件链接）
     图片 → vault-<项目>/md/<类别>/<文档名>/media/<图名> + 同目录 md（内嵌 + OCR）
           例外：日常工作佐证 类图片只归档不进 md/Cognee，
           命名 <拍摄日期>-<关键词1>-<关键词2>.<ext>（见 auto_image_name）
  4. 飞书 +move → 已分类/<项目>/<类别>/
  5. 写 state/manifest.yaml（幂等 + 变更检测）
  6. cognee add + cognify

用法
  python3 scan-inbox.py                 # 扫描并处理
  python3 scan-inbox.py --dry-run       # 只报计划
  python3 scan-inbox.py --no-cognify    # 不重建图谱
  python3 scan-inbox.py --verbose       # 打印分类理由
  python3 scan-inbox.py --config <path> # 指定配置文件（默认 config/config.yaml）

路径与容器名来自 config/config.yaml（见 scripts/config.py）。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from config import load_config

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_convert_md():
    """convert-md.py 带连字符，不能直接 import，用 importlib 加载。"""
    spec = importlib.util.spec_from_file_location("convert_md", SCRIPT_DIR / "convert-md.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_classify_rules():
    """加载同目录的 classify_rules.py（M2 规则层）。"""
    spec = importlib.util.spec_from_file_location("classify_rules", SCRIPT_DIR / "classify_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_image_policy():
    """加载同目录的 image_policy.py（M1 图片闸门）。"""
    spec = importlib.util.spec_from_file_location("image_policy", SCRIPT_DIR / "image_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_audit_log():
    """加载同目录的 audit_log.py（审计日志写入口）。"""
    spec = importlib.util.spec_from_file_location("audit_log", SCRIPT_DIR / "audit_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cm = _load_convert_md()
_cr = _load_classify_rules()
_ip = _load_image_policy()
_al = _load_audit_log()
CONVERTIBLE = _cm.CONVERTIBLE
TEXTUAL = _cm.TEXTUAL
MEDIA = _cm.MEDIA
IMAGE_EXTS = _cm.IMAGE_EXTS
convert_pandoc = _cm.convert_pandoc
convert_docling = _cm.convert_docling
normalize_images = _cm.normalize_images
insert_links = _cm.insert_links

CFG = None
ROOT = None
CONFIG_PATH = None
MANIFEST_PATH = None
INBOX = None
PENDING = None
COGNEE_CONTAINER = None
COGNEE_PY = SCRIPT_DIR / "cognee.py"
TZ = timezone(timedelta(hours=8))


def _apply_config(path: str | None = None) -> None:
    """加载配置并刷新模块级路径（--config 覆盖时再次调用）。"""
    global CFG, ROOT, CONFIG_PATH, MANIFEST_PATH, INBOX, PENDING, COGNEE_CONTAINER
    CFG = load_config(path)
    ROOT = CFG.root
    CONFIG_PATH = ROOT / "feishu-flow.yaml"
    MANIFEST_PATH = ROOT / "state" / "manifest.yaml"
    INBOX = ROOT / "inbox"
    PENDING = ROOT / "sources" / "_待确认"
    COGNEE_CONTAINER = CFG.container
    # 转换器模块的 root/container 也同步（scan-inbox 直接调用它的转换函数）
    _cm.apply_config(CFG)


_apply_config()

CLASSIFY_PROMPT = """你是社工知识库的分类助手。根据文件名和内容摘要，判断这份文件属于哪个项目、哪个类别。

## 项目（必须三选一）
- 花都：花都社区的项目资料
- 惠民：惠民社区的项目资料
- 共享：跨项目通用资料、政策文件、上级通知

## 类别（必须六选一）
- 发票票据：发票、收据、报销凭证、采购清单、费用明细
- 项目计划：项目方案、申报书、年度计划、策划书、预算
- 项目执行：活动记录、会议纪要、进展表、推文、总结
- 日常工作佐证：台账、签到表、走访记录、走访照片说明
- 政策文件：上级文件、法规、通知、办法、标准
- 其他：无法归入以上类别

## 输入
文件名：{name}
内容摘要：
{preview}

## 输出
只输出 JSON，不要任何解释：
{{"project": "花都|惠民|共享", "category": "发票票据|项目计划|项目执行|日常工作佐证|政策文件|其他", "confidence": 0.0到1.0, "reason": "一句话理由", "title": "图片主题词：2-8 个汉字，用 - 连接（如 宣传栏-微网实格公示）；非图片文件留空字符串"}}"""

OCR_SCRIPT = r"""
import sys
from rapidocr import RapidOCR
engine = RapidOCR()
result, _ = engine(sys.argv[1])
for item in (result or []):
    print(item[1])
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path, default):
    if not path.is_file():
        return default
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or default
    except Exception as exc:
        log(f"  ! 读取 {path} 失败：{exc}")
        return default


def save_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_llm() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return {
        "endpoint": env.get("LLM_ENDPOINT", ""),
        "key": env.get("LLM_API_KEY", ""),
        "model": env.get("LLM_MODEL", "").split("/")[-1],
    }


# ---------- 飞书 ----------

def lark(args: list[str], timeout: int = 1800) -> dict:
    cmd = ["lark-cli"] + args + ["--as", "user", "--format", "json"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    out = proc.stdout or ""
    starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
    if proc.returncode != 0 or not starts:
        raise RuntimeError((proc.stderr or out).strip()[:500])
    return json.loads(out[min(starts):])


def pull_inbox(inbox_token: str) -> list[dict]:
    data = lark(["drive", "+pull", "--local-dir", "inbox", "--folder-token", inbox_token])
    return (data.get("data") or {}).get("items") or []


def move_to_archive(file_token: str, dest_folder: str) -> None:
    lark(["drive", "+move", "--file-token", file_token, "--type", "file",
          "--folder-token", dest_folder])


# ---------- 内容提取 ----------

def ocr_image(src: Path) -> str:
    tmp = f"/tmp/ocr-{os.getpid()}"
    subprocess.run(["docker", "exec", COGNEE_CONTAINER, "mkdir", "-p", tmp], capture_output=True)
    subprocess.run(["docker", "cp", str(src), f"{COGNEE_CONTAINER}:{tmp}/img"], capture_output=True)
    run = subprocess.run(
        ["docker", "exec", "-i", COGNEE_CONTAINER, "/app/.venv/bin/python", "-", f"{tmp}/img"],
        input=OCR_SCRIPT, capture_output=True, text=True, timeout=600,
    )
    subprocess.run(["docker", "exec", COGNEE_CONTAINER, "rm", "-rf", tmp], capture_output=True)
    if run.returncode != 0:
        return ""
    return (run.stdout or "").strip()


def extract(src: Path, tmpdir: Path) -> dict:
    """把文件转成可读文本，返回 {converter, text, outdir}。"""
    ext = src.suffix.lower()
    outdir = tmpdir / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    if ext in CONVERTIBLE:
        outname = src.stem + ".md"
        if CONVERTIBLE[ext] == "pandoc":
            text = convert_pandoc(src, outdir, outname)
        else:
            text = convert_docling(src, outdir, outname)
        return {"converter": CONVERTIBLE[ext], "text": text, "outdir": outdir}
    if ext in TEXTUAL:
        return {"converter": "copy", "text": src.read_text(encoding="utf-8", errors="replace"),
                "outdir": outdir}
    if ext in MEDIA:
        text = ocr_image(src) if ext in IMAGE_EXTS else ""
        return {"converter": "media", "text": text, "outdir": outdir}
    return {"converter": "unknown", "text": "", "outdir": outdir}


# ---------- 分类 ----------

def classify(name: str, preview: str, llm: dict, projects: dict, categories: list[str],
             verbose: bool = False) -> dict:
    prompt = CLASSIFY_PROMPT.format(name=name, preview=(preview or "(无文本内容)")[:3000])
    body = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        llm["endpoint"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {llm['key']}", "Content-Type": "application/json"},
    )
    fallback = {"project": "", "category": "其他", "confidence": 0.0, "reason": ""}
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        log(f"  ! 分类请求失败：{exc}")
        fallback["reason"] = f"LLM 失败：{exc}"
        return fallback

    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        fallback["reason"] = "LLM 未返回 JSON"
        return fallback
    try:
        result = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        fallback["reason"] = "JSON 解析失败"
        return fallback

    label = str(result.get("project", "")).strip()
    category = str(result.get("category", "")).strip()
    project = next((k for k, v in projects.items() if v["label"] == label), "")
    if category not in categories:
        category = "其他"
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    result.update({"project": project, "category": category, "confidence": confidence,
                   "title": str(result.get("title", "") or "").strip()})
    if verbose:
        log(f"     {label or '?'} / {category}  置信度 {confidence:.2f}  {result.get('reason', '')}")
    return result


# ---------- 归档 ----------

def entity_names(project: str) -> list[str]:
    names: set[str] = set()
    for path in (ROOT / "entities.yaml", ROOT / "sources" / "projects" / project / "entities.yaml"):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    names.update(str(x).strip() for x in v if str(x).strip())
        elif isinstance(data, list):
            names.update(str(x).strip() for x in data if str(x).strip())
    return sorted((n for n in names if len(n) >= 2), key=len, reverse=True)


def unique_dir(parent: Path, stem: str, token: str) -> Path:
    d = parent / stem
    if d.exists():
        d = parent / f"{stem}-{token[:6]}"
    return d


# ---------- 日常工作佐证图片命名 ----------

WORK_PHOTO_CATEGORY = "日常工作佐证"


def exif_date(src: Path) -> str:
    """读 EXIF 拍摄日期（YYYY-MM-DD），失败返回空串。"""
    try:
        from PIL import Image
        with Image.open(src) as im:
            exif = im.getexif()
            raw = exif.get(0x9003) or exif.get(0x0132)  # DateTimeOriginal / DateTime
        if raw:
            return str(raw).split(" ")[0].replace(":", "-")
    except Exception:
        pass
    return ""


def file_date(src: Path) -> str:
    """退路：文件 mtime（飞书落盘时间）。"""
    return datetime.fromtimestamp(src.stat().st_mtime, TZ).strftime("%Y-%m-%d")


def auto_image_name(src: Path, title: str) -> str:
    """日常工作佐证图片命名：<拍摄日期>-<关键词1>-<关键词2>.<ext>。

    日期优先取 EXIF 拍摄时间（汇报按工作日找图），无 EXIF 用文件 mtime。
    title 来自分类调用里 LLM 给的图片主题词。
    """
    date = exif_date(src) or file_date(src)
    stem = "-".join(x for x in (date, (title or "").strip()) if x) or "日常工作"
    stem = re.sub(r'[\\/:*?"<>|\s]+', "-", stem).strip("-")
    return stem + src.suffix.lower()


def unique_dest(dest: Path, digest: str) -> Path:
    """同名但内容不同时加序号，避免覆盖（只存档图片按日期命名，同一天多张会撞名）。"""
    if not dest.exists():
        return dest
    try:
        if sha256_of(dest) == digest:
            return dest                      # 同一个文件，覆盖无害
    except OSError:
        pass
    for i in range(2, 1000):
        cand = dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
        if not cand.exists():
            return cand
        try:
            if sha256_of(cand) == digest:
                return cand
        except OSError:
            pass
    return dest.with_name(f"{dest.stem}-{os.getpid()}{dest.suffix}")


def archive_one(src: Path, token: str, digest: str, decision: dict, cfg: dict,
                manifest: dict, conv: dict, dry_run: bool, llm: dict,
                action: str | None = None) -> str:
    projects = cfg["projects"]
    project = decision["project"]
    category = decision["category"]
    # 只归档：日常工作佐证图片，或 M1 判定 archive_only 的图片。
    # 两者都不进 md / Cognee，文件名自动命名。
    work_photo = src.suffix.lower() in IMAGE_EXTS and (
        action == "archive_only"
        or (bool(project) and category == WORK_PHOTO_CATEGORY)
    )
    name = auto_image_name(src, decision.get("title", "")) if work_photo else src.name
    url = cfg["feishu"]["url_template"].format(token=token)
    label = projects[project]["label"] if project else "待确认"
    header = f"> 原件：[{name}]({url})\n>\n> 分类：{label} / {category}\n\n"

    if project:
        original_dir = f"sources/projects/{project}/{category}"
        vault = ROOT / projects[project]["vault"]
        md_dir = unique_dir(vault / "md" / category, src.stem, token)
    else:
        original_dir = f"_待确认/{category}"
        md_dir = unique_dir(PENDING / "_md" / category, src.stem, token)

    entry = manifest["files"].setdefault(token, {})
    entry.update({
        "name": name, "project": project, "category": category,
        "original": f"{original_dir}/{name}", "sha256": digest, "size": src.stat().st_size,
        "status": "pending", "converter": conv["converter"],
        "seen_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })

    if dry_run:
        log(f"  [dry-run] {name} → {original_dir}/{name}")
        return "dry-run"

    md_dir.mkdir(parents=True, exist_ok=True)
    # 原件落位（只存档图片按日期命名，同一天多张会撞名 → unique_dest 加序号）
    dest_dir = ROOT / original_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(dest_dir / name, digest) if work_photo else dest_dir / name
    shutil.copy2(src, dest)
    # lark-cli 落地的文件是 600，Cognee 容器 uid=1000 读不到
    os.chmod(dest, 0o644)
    if dest.name != name:
        name = dest.name
        entry["name"] = name

    if work_photo:
        # 只归档：有项目才往 vault media 放副本，不生成 md
        if project:
            media_dir = vault / "media" / category
            media_dir.mkdir(parents=True, exist_ok=True)
            media = unique_dest(media_dir / name, digest)
            shutil.copy2(src, media)
            os.chmod(media, 0o644)
            entry["media"] = str(media.relative_to(ROOT))
            log(f"     → {entry['media']}")
    # md 落位
    elif conv["converter"] == "media":
        media_dir = md_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, media_dir / name)
        os.chmod(media_dir / name, 0o644)
        body = f"![](media/{name})\n"
        if conv["text"]:
            body += f"\n## OCR\n\n{conv['text']}\n"
        (md_dir / (src.stem + ".md")).write_text(header + body, encoding="utf-8")
    elif conv["converter"] in {"pandoc", "docling"}:
        text = normalize_images(conv["text"], conv["outdir"])
        text, hits = insert_links(text, entity_names(project))
        entry["links"] = hits
        (md_dir / (src.stem + ".md")).write_text(header + text, encoding="utf-8")
    elif conv["converter"] == "copy":
        text, hits = insert_links(conv["text"], entity_names(project))
        entry["links"] = hits
        (md_dir / (src.stem + ".md")).write_text(header + text, encoding="utf-8")
    else:
        entry["status"] = "unsupported"
        save_yaml(MANIFEST_PATH, manifest)
        log(f"  ! 不支持的格式：{name}")
        return "unsupported"

    if not work_photo:
        entry["md"] = str(md_dir.relative_to(ROOT) / (src.stem + ".md"))

    # 飞书归档
    if project:
        dest_folder = cfg["category_folders"][projects[project]["label"]][category]
        try:
            move_to_archive(token, dest_folder)
            entry["status"] = "done"
            entry["archived_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        except Exception as exc:
            entry["status"] = "pending"
            entry["error"] = f"mv 失败：{exc}"
            log(f"  ! {name} 归档失败：{exc}")
    else:
        entry["status"] = "pending_review"
        log(f"  ! {name} 置信度不足，落 _待确认/{category}/")
    return entry["status"]


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="飞书未分类 → 分类 → md → 归档")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-cognify", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--config", metavar="PATH", help="配置文件路径（默认 config/config.yaml）")
    args = ap.parse_args()

    if args.config:
        try:
            _apply_config(args.config)
        except FileNotFoundError as exc:
            log(f"  ! {exc}")
            return 1

    cfg = load_yaml(CONFIG_PATH, {})
    if not cfg:
        log(f"缺少配置 {CONFIG_PATH}")
        return 1
    projects, categories = cfg["projects"], cfg["categories"]
    min_conf = float(cfg.get("min_confidence", 0.7))
    llm = load_llm()

    # M2 规则层：判得了就省一次 LLM 调用；判不了（没命中/并列）才走 LLM
    # M1 图片策略：按目录决定图片是 OCR / 识图 / 只存档
    rules = None
    image_policy = None
    rules_dir = CFG.path.parent if getattr(CFG, "path", None) else (ROOT / "config")
    if rules_dir.is_dir():
        try:
            rules = _cr.load_rules(rules_dir)
            log(f"· 规则层已加载：{rules_dir}")
        except Exception as exc:
            log(f"  ! 规则层加载失败，本次全部走 LLM：{exc}")
        try:
            image_policy = _ip.load_policy(rules_dir)
            log(f"· 图片策略：默认 {image_policy.get('default')}")
        except Exception as exc:
            log(f"  ! 图片策略加载失败，本次不拦图片：{exc}")

    log("=== 拉取飞书未分类 ===")
    items = pull_inbox(cfg["feishu"]["inbox"])
    log(f"  {len(items)} 个文件" if items else "  (空)")

    manifest = load_yaml(MANIFEST_PATH, {"files": {}})
    manifest.setdefault("files", {})

    processed = skipped = 0
    touched: set[str] = set()

    for it in items:
        token = it.get("file_token", "")
        rel = it.get("rel_path", "")
        src = INBOX / rel
        if not src.is_file():
            log(f"  ! 拉取后找不到 {rel}")
            continue
        # lark-cli 落地的文件是 600，Cognee 容器 uid=1000 读不到，docling 会 PermissionError
        os.chmod(src, 0o644)
        digest = sha256_of(src)
        prev = manifest["files"].get(token)
        if prev and prev.get("sha256") == digest and prev.get("status") in {"done", "pending_review"}:
            log(f"  = 跳过（已处理）{rel}")
            skipped += 1
            continue

        size = src.stat().st_size
        log(f"\n--- {rel} ({size} B) ---")
        tmpdir = Path(tempfile.mkdtemp(prefix="scan-"))
        started = datetime.now(TZ)
        action = None

        def record_audit(**kw) -> None:
            """写一条审计记录。失败只告警 —— 审计挂掉不该拖垮归档。"""
            base = {
                "file": rel, "sha256": digest, "size": size,
                "action": action or "normal", "md_path": None, "data_id": None,
                "duration_ms": int((datetime.now(TZ) - started).total_seconds() * 1000),
            }
            base.update(kw)
            try:
                _al.append(ROOT, base)
            except Exception as exc:
                log(f"  ! 审计写入失败：{exc}")

        try:
            action = _ip.decide_action(rel, src.suffix.lower(), image_policy, IMAGE_EXTS)
            if action == "archive_only":
                # M1 闸门：不 OCR、不调视觉模型，直接只存档
                conv = {"converter": "media", "text": "", "outdir": tmpdir / "out"}
                log("     · 只存档（archive_only）：跳过 OCR / 视觉模型")
            else:
                try:
                    conv = extract(src, tmpdir)
                except Exception as exc:
                    log(f"  ! 转换失败：{exc}")
                    record_audit(converter=None, project=None, category=None,
                                 confidence=None, status="error",
                                 error=f"转换失败：{exc}")
                    continue
            if conv["converter"] in {"pandoc", "docling"}:
                log(f"     converter={conv['converter']}  文本 {len(conv['text'])} 字")
            # 先跑 M2 规则层；判不了才调 LLM
            decision = None
            if rules is not None:
                decision = _cr.classify_rule(rel, conv["text"], rules, projects, categories)
                if decision:
                    log(f"  · 规则判定 {decision['project']} / {decision['category']}")
            if not decision:
                decision = classify(rel, conv["text"], llm, projects, categories, args.verbose)
            if decision["confidence"] < min_conf and decision["project"]:
                log(f"  ! 置信度 {decision['confidence']:.2f} < {min_conf}，转待确认")
                decision["project"] = ""
            status = archive_one(src, token, digest, decision, cfg, manifest, conv,
                                 args.dry_run, llm, action=action)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        if status != "dry-run":
            processed += 1
            # 日常工作佐证图片不产 md，不触发 Cognee 重建
            if decision["project"] and manifest["files"][token].get("md"):
                touched.add(projects[decision["project"]]["dataset"])
            if status == "done":
                # 飞书侧已归档，清掉本地副本，下次扫描才干净
                src.unlink()
        save_yaml(MANIFEST_PATH, manifest)
        # 审计日志：每个处理过的文件一条
        record_audit(converter=conv.get("converter"),
                     project=decision.get("project", ""),
                     category=decision.get("category", ""),
                     confidence=decision.get("confidence"),
                     md_path=manifest["files"].get(token, {}).get("md"),
                     status=status)

    log(f"\n完成：处理 {processed}，跳过 {skipped}")

    if args.no_cognify or args.dry_run or not touched:
        return 0

    log("\n=== Cognee 重建 ===")
    for project, pcfg in projects.items():
        if pcfg["dataset"] not in touched:
            continue
        md_root = ROOT / pcfg["vault"]
        if not md_root.is_dir():
            continue
        r = subprocess.run([sys.executable, str(COGNEE_PY), "sync", str(md_root),
                            "--dataset", pcfg["dataset"]],
                           capture_output=True, text=True)
        out = (r.stdout or r.stderr).strip()
        log(f"  {pcfg['dataset']}: {out[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
