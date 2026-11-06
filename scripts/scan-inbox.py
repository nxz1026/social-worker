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


_cm = _load_convert_md()
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


def archive_one(src: Path, token: str, digest: str, decision: dict, cfg: dict,
                manifest: dict, conv: dict, dry_run: bool, llm: dict) -> str:
    projects = cfg["projects"]
    project = decision["project"]
    category = decision["category"]
    # 日常工作佐证图片：只归档，不进 md / Cognee，文件名自动命名
    work_photo = bool(project) and category == WORK_PHOTO_CATEGORY and src.suffix.lower() in IMAGE_EXTS
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
    # 原件落位
    dest_dir = ROOT / original_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / name)
    # lark-cli 落地的文件是 600，Cognee 容器 uid=1000 读不到
    os.chmod(dest_dir / name, 0o644)

    if work_photo:
        # 只归档：vault-<项目>/media/日常工作佐证/<自动命名>，不生成 md
        media_dir = vault / "media" / category
        media_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, media_dir / name)
        os.chmod(media_dir / name, 0o644)
        entry["media"] = str((media_dir / name).relative_to(ROOT))
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

        log(f"\n--- {rel} ({src.stat().st_size} B) ---")
        tmpdir = Path(tempfile.mkdtemp(prefix="scan-"))
        try:
            try:
                conv = extract(src, tmpdir)
            except Exception as exc:
                log(f"  ! 转换失败：{exc}")
                continue
            if conv["converter"] in {"pandoc", "docling"}:
                log(f"     converter={conv['converter']}  文本 {len(conv['text'])} 字")
            decision = classify(rel, conv["text"], llm, projects, categories, args.verbose)
            if decision["confidence"] < min_conf and decision["project"]:
                log(f"  ! 置信度 {decision['confidence']:.2f} < {min_conf}，转待确认")
                decision["project"] = ""
            status = archive_one(src, token, digest, decision, cfg, manifest, conv, args.dry_run, llm)
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
