#!/usr/bin/env python3
"""生成民乐北巷停车长效治理 KT 板（120×90cm 横版，含 3mm 出血）。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "print" / "kt-board"

COLS = [
    {
        "no": "01",
        "title": "现在还有什么问题？",
        "color": "#B5541F",
        "tint": "#F0DCCB",
        "items": [
            "哪一段、哪个时段最堵？",
            "是没地方停，还是有地方不停？",
            "卸货、顾客停车、居民停车，谁最难？",
            "最近一周，哪里又反弹了？",
        ],
    },
    {
        "no": "02",
        "title": "责任怎么落到户？",
        "color": "#14524A",
        "tint": "#D8E7E1",
        "items": [
            "商户：门前三包管到哪一步？谁来提醒、记录？",
            "居民：发现乱停找谁？多久有回应？",
            "志愿者：巡查怎么排班？缺什么保障？",
            "菜市：门口通道和卸货区，怎么错开？",
        ],
    },
    {
        "no": "03",
        "title": "长期谁来管？",
        "color": "#2F5D7C",
        "tint": "#DCE6ED",
        "items": [
            "设施：隔离桩、停车线、标识，谁巡查、谁修？",
            "机制：每周巡查、每月议事，怎么不打折？",
            "反弹：哪些点位要“回头看”？",
            "保障：需要社区给什么支持？",
        ],
    },
]

RULES = [
    "车进线、头朝外，通道不停车。",
    "门前三包到户：卫生、秩序、绿化，自家门前自家管。",
    "卸货即卸即走，不长时间占道。",
    "电瓶车、三轮车停进临时停放区，不堵巷口、不压盲道。",
    "见到乱停先劝一句；劝不动，找网格员或发群里。",
    "每周巡查、每月议事，反弹点位当月上会。",
]

CSS = """
@page { size: 1206mm 906mm; margin: 0; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  color: #1F2A26; background: #DCD6C8;
}
.board {
  width: 1206mm; height: 906mm; position: relative; overflow: hidden;
  background: #FBF7EC; margin: 0 auto;
}
.band {
  position: absolute; left: 0; top: 0; width: 100%; height: 145mm;
  background: #14524A; color: #fff;
}
.band .brand {
  position: absolute; left: 45mm; top: 34mm;
  font-size: 22pt; letter-spacing: .12em; opacity: .92;
}
.band h1 {
  position: absolute; left: 45mm; top: 46mm; margin: 0;
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 82pt; line-height: 1.08; font-weight: 700; letter-spacing: .04em;
}
.band h1 span { color: #F3C9A6; }
.band .sub {
  position: absolute; left: 45mm; top: 117mm;
  font-size: 28pt; letter-spacing: .08em; opacity: .95;
}
.band .meta {
  position: absolute; right: 45mm; bottom: 30mm; text-align: right;
  font-size: 20pt; line-height: 1.6; opacity: .9;
}
.band .meta b { font-size: 30pt; font-weight: 700; letter-spacing: .04em; }

.intro {
  position: absolute; left: 45mm; right: 45mm; top: 170mm; height: 55mm;
  background: #fff; border-left: 9mm solid #B5541F;
  display: flex; align-items: center; padding: 0 24mm;
  font-size: 27pt; letter-spacing: .02em;
}
.intro b { color: #14524A; margin-left: 6mm; }

.cols {
  position: absolute; left: 45mm; right: 45mm; top: 250mm; height: 220mm;
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 24mm;
}
.col {
  position: relative; background: #fff; padding: 18mm 20mm 14mm;
  border-top: 6mm solid var(--c);
}
.col .no {
  position: absolute; right: 14mm; top: 4mm;
  font-family: "Noto Serif CJK SC", serif; font-size: 96pt; font-weight: 700;
  color: var(--t); line-height: 1;
}
.col h2 {
  position: relative; margin: 2mm 0 0; font-size: 34pt; line-height: 1.25;
  font-weight: 700; letter-spacing: .02em; color: #1F2A26;
}
.col ul { margin: 14mm 0 0; padding: 0; list-style: none; }
.col li {
  position: relative; padding-left: 13mm; margin-top: 9mm;
  font-size: 27pt; line-height: 1.55; color: #2B2A25;
}
.col li::before {
  content: ""; position: absolute; left: 0; top: 4.5mm;
  width: 5mm; height: 5mm; border-radius: 50%; background: var(--c);
}
.col .record { margin-top: 12mm; }
.col .record b { display: block; font-size: 17pt; color: #8A7F6E; margin-bottom: 2mm; }
.col .record i { display: block; height: 9mm; border-bottom: .5mm solid #C9C0B0; }

.rules {
  position: absolute; left: 45mm; right: 45mm; top: 490mm; height: 160mm;
  background: #fff; border-top: 6mm solid #14524A; padding: 16mm 24mm 14mm;
}
.rules h2 {
  margin: 0 0 7mm; font-family: "Noto Serif CJK SC", serif;
  font-size: 32pt; font-weight: 700; letter-spacing: .04em;
}
.rules h2 em { font-style: normal; font-family: "Noto Sans CJK SC", sans-serif;
  font-size: 15pt; color: #8A7F6E; font-weight: 400; margin-left: 4mm; }
.rule-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10mm 26mm; }
.rule { display: flex; align-items: flex-start; gap: 6mm; }
.rule .n {
  flex: none; width: 11mm; height: 11mm; border-radius: 50%;
  background: #14524A; color: #fff; font-size: 14pt; font-weight: 700;
  display: flex; align-items: center; justify-content: center; margin-top: .5mm;
}
.rule p { margin: 0; font-size: 30pt; line-height: 1.45; }
.rules .tip { margin-top: 9mm; font-size: 17pt; color: #6B6257; }

.notes {
  position: absolute; left: 45mm; right: 45mm; top: 680mm; height: 140mm;
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18mm;
}
.note {
  border: 1.6mm dashed #A79B87; background: #fff; padding: 12mm 14mm;
}
.note b { display: block; font-size: 23pt; color: #14524A; margin-bottom: 4mm; }
.note span { font-size: 16pt; color: #6B6257; }

.foot {
  position: absolute; left: 45mm; right: 45mm; bottom: 30mm;
  display: flex; justify-content: space-between; align-items: flex-end;
  font-size: 14pt; color: #6B6257; line-height: 1.5;
}
.foot .r { text-align: right; }
"""


def build() -> str:
    cols = []
    for c in COLS:
        items = "".join(f"<li>{x}</li>" for x in c["items"])
        cols.append(
            f'<section class="col" style="--c:{c["color"]};--t:{c["tint"]}">'
            f'<div class="no">{c["no"]}</div><h2>{c["title"]}</h2>'
            f'<ul>{items}</ul>'
            f'<div class="record"><b>现场记录</b><i></i><i></i><i></i><i></i><i></i></div></section>'
        )
    rules = "".join(
        f'<div class="rule"><div class="n">{i}</div><p>{r}</p></div>'
        for i, r in enumerate(RULES, 1)
    )
    notes = "".join(
        f'<div class="note"><b>{b}</b><span>把便利贴贴在这里</span></div>'
        for b in ["最该先解决的一件事", "愿意搭把手的事", "最担心反弹的点位"]
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>民乐北巷停车长效治理 KT 板</title><style>{CSS}</style></head>
<body><div class="board">
  <div class="band">
    <div class="brand">惠民社区 · 民乐北巷</div>
    <h1>停车与通行<br><span>长效治理</span></h1>
    <div class="sub">爱邻议事会 · 现场讨论</div>
    <div class="meta"><b>2026.11.11</b><br>智慧菜市门口</div>
  </div>
  <div class="intro">今天议三件事：<b>问题说具体，责任落到人，办法定下来。</b></div>
  <div class="cols">{''.join(cols)}</div>
  <div class="rules"><h2>六条约定<em>讨论稿，以议事会通过文本为准</em></h2>
    <div class="rule-grid">{rules}</div>
    <div class="tip">今天逐条过；有不同意见，当场说。</div>
  </div>
  <div class="notes">{notes}</div>
  <div class="foot">
    <div>主办：成都市温江区公平街道惠民社区居民委员会<br>承办：成都市温江区益嘉仁社会工作服务中心</div>
    <div class="r">民乐北巷 · 停车与通行长效治理</div>
  </div>
</div></body></html>"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    html_path = OUT / "kt-board.html"
    html_path.write_text(build(), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=1)
        page.goto(html_path.as_uri())
        page.wait_for_timeout(400)
        page.pdf(
            path=str(OUT / "kt-board.pdf"),
            width="1206mm",
            height="906mm",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            print_background=True,
            prefer_css_page_size=True,
        )
        el = page.locator(".board")
        el.screenshot(path=str(OUT / "_preview_full.png"))
        browser.close()

    img = Image.open(OUT / "_preview_full.png")
    w = 1800
    h = round(img.height * w / img.width)
    img.resize((w, h), Image.LANCZOS).save(OUT / "preview.png")
    (OUT / "_preview_full.png").unlink()
    print("pdf:", OUT / "kt-board.pdf")


if __name__ == "__main__":
    main()
