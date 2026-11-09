#!/usr/bin/env python3
"""生成民乐北巷停车长效治理回访卡（A5 双面，含 3mm 出血）。

输出：
  docs/print/qa-cards/qa-cards.html   可编辑版面
  docs/print/qa-cards/qa-cards.pdf    送印文件（154x216mm，含出血）
  docs/print/qa-cards/preview-*.png   逐页预览
"""
from __future__ import annotations

import html
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "print" / "qa-cards"

# ---------- 文案 ----------

COMMON = [
    {
        "type": "scale",
        "title": "给现在的停车、通行秩序打分",
        "hint": "1 很差，5 很好",
    },
    {
        "type": "opts",
        "title": "最近一个月，通道被车堵住的情况",
        "options": ["基本没有", "偶尔（每周 1-2 次）", "经常（每周 3 次以上）", "说不清"],
        "cols": 2,
    },
    {
        "type": "opts",
        "title": "看到有人把车停在通道上，您一般会",
        "options": ["上前劝一句", "告诉网格员或菜市管理方", "拍照发到群里", "不管，管不了"],
        "cols": 2,
    },
]

CARDS = [
    {
        "key": "merchant",
        "badge": "商户版",
        "title": "商户回访卡",
        "sub": "门前三包到户，自家门前自家管",
        "accent": "#B5541F",
        "tint": "#F6E3D2",
        "info": (
            '<span class="fill">商铺位置</span><span class="blank w18"></span>号'
            '<span class="sep"></span>'
            '<span class="fill">经营类型</span>'
            '<span class="tick">餐饮</span><span class="tick">零售</span>'
            '<span class="tick">菜市摊位</span><span class="tick">其他</span><span class="blank w12"></span>'
        ),
        "questions": [
            {
                "type": "opts",
                "title": "您门店前的停车秩序，写进门前三包了吗？",
                "options": ["写进了，知道怎么管", "口头说过，没写下来", "没人说过", "不归我管"],
                "cols": 2,
            },
            {
                "type": "opts",
                "title": "对您做生意影响最大的是",
                "options": ["卸货没地方停", "顾客停车不方便", "门口被堵", "影响不大"],
                "cols": 2,
            },
            {
                "type": "multi",
                "title": "长期维护，您愿意做哪几项？",
                "options": ["劝门前乱停", "保持线内整齐", "参加每月巡查", "暂时不愿意"],
                "cols": 2,
            },
        ],
        "open": "如果社区再帮一把，您最希望解决门店前的什么停车问题？",
        "lines": 4,
    },
    {
        "key": "resident",
        "badge": "居民版",
        "title": "居民回访卡",
        "sub": "路通不通，住在这儿的人最有发言权",
        "accent": "#14524A",
        "tint": "#DCEAE4",
        "info": (
            '<span class="fill">您和这条巷子</span>'
            '<span class="tick">住在本巷</span><span class="tick">住在周边</span><span class="tick">常来买菜</span>'
            '<span class="sep"></span>'
            '<span class="fill">每天经过</span><span class="tick">1-2 次</span><span class="tick">3 次以上</span>'
        ),
        "questions": [
            {
                "type": "opts",
                "title": "您知道《民乐北巷治理公约》吗？",
                "options": ["知道，看过内容", "听说过，没见过", "不知道"],
                "cols": 3,
            },
            {
                "type": "opts",
                "title": "您知道车辆临时停放区在哪吗？",
                "options": ["知道，就在附近", "知道大概位置", "不知道"],
                "cols": 3,
            },
            {
                "type": "opts",
                "title": "您愿意参加停车秩序志愿巡查吗？",
                "options": ["愿意，定期参加", "偶尔可以", "不愿意", "看时间"],
                "cols": 2,
            },
        ],
        "open": "这段路现在最让您不放心的是哪一点？",
        "lines": 4,
    },
    {
        "key": "volunteer",
        "badge": "志愿者版",
        "title": "志愿者回访卡",
        "sub": "一次行动不难，难的是有人一直管",
        "accent": "#2F5D7C",
        "tint": "#DEE8EF",
        "info": (
            '<span class="fill">志愿者身份</span>'
            '<span class="tick">居民</span><span class="tick">党员</span>'
            '<span class="tick">商户</span><span class="tick">其他</span><span class="blank w10"></span>'
            '<span class="sep"></span>'
            '<span class="fill">参加过</span><span class="tick">1 次</span><span class="tick">2-3 次</span><span class="tick">3 次以上</span>'
        ),
        "questions": [
            {
                "type": "opts",
                "title": "参加整治或巡查，您觉得最大的困难是",
                "options": ["车主不理解", "人手不够", "缺少工具、标识", "劝了没什么用"],
                "cols": 2,
            },
            {
                "type": "opts",
                "title": "现在的志愿者安排，您觉得能长期坚持吗？",
                "options": ["能", "短期可以", "很难", "说不清"],
                "cols": 2,
            },
            {
                "type": "multi",
                "title": "您愿意继续参加哪种？",
                "options": ["每周巡查", "每月集中整治", "爱邻议事会", "暂时不参加"],
                "cols": 2,
            },
        ],
        "open": "要让志愿巡查长期转下去，您觉得最该先解决什么？",
        "lines": 4,
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

# ---------- 版面 ----------

CSS = """
@page { size: 154mm 216mm; margin: 0; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  color: #1F2A26;
  background: #E7E3D8;
}
.page {
  width: 154mm; height: 216mm; position: relative; overflow: hidden;
  background: #FBF7EC; color: #1F2A26;
  margin: 0; box-shadow: none;
}
.page:not(:last-child) { break-after: page; }
.band {
  position: absolute; top: 0; left: 0; right: 0; height: 15mm;
  background: var(--accent);
}
.band .brand, .band .badge {
  position: absolute; top: 0; height: 15mm; display: flex; align-items: center;
  color: #fff; font-size: 8.2pt; letter-spacing: .06em;
}
.band .brand { left: 8mm; font-weight: 500; }
.band .badge {
  right: 8mm; font-weight: 700; letter-spacing: .12em;
  border: .3mm solid rgba(255,255,255,.75); border-radius: 1mm;
  height: 6.2mm; top: 4.4mm; padding: 0 2.2mm; font-size: 7.6pt;
}
.safe { position: absolute; left: 8mm; right: 8mm; top: 8mm; bottom: 8mm;
         display: flex; flex-direction: column; }

.head { padding-top: 12mm; }
.head h1 {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 21pt; line-height: 1.15; margin: 0; font-weight: 700; letter-spacing: .04em;
}
.head .sub { margin-top: 1.6mm; font-size: 8.6pt; color: #5C554A; letter-spacing: .02em; }

.intro {
  margin-top: 3.2mm; padding: 2.4mm 3mm; border-left: .8mm solid var(--accent);
  background: var(--tint); font-size: 8pt; line-height: 1.55; color: #3A352D;
}

.info {
  margin-top: 3mm; padding: 2.4mm 0 2.2mm; border-top: .25mm solid #D8CFBE;
  border-bottom: .25mm solid #D8CFBE;
  font-size: 8.4pt; line-height: 2.1; color: #2B2A25;
}
.info .fill { color: #6B6257; margin-right: 1mm; }
.info .sep { display: inline-block; width: 4mm; }
.info .blank { display: inline-block; border-bottom: .3mm solid #8C8272; height: 3.2mm; vertical-align: -0.4mm; margin: 0 1mm; }
.info .w18 { width: 18mm; } .info .w12 { width: 12mm; } .info .w10 { width: 10mm; }
.info .tick { display: inline-block; margin-right: 2.2mm; white-space: nowrap; }
.info .tick::before {
  content: ""; display: inline-block; width: 2.8mm; height: 2.8mm;
  border: .3mm solid #8C8272; border-radius: .5mm; background: #fff;
  vertical-align: -0.35mm; margin-right: 1mm;
}

.q { margin-top: 3.5mm; }
.qt { font-size: 10pt; line-height: 1.35; font-weight: 700; letter-spacing: .01em; }
.qt .no {
  display: inline-flex; align-items: center; justify-content: center;
  width: 4.6mm; height: 4.6mm; margin-right: 1.6mm; border-radius: .8mm;
  background: var(--accent); color: #fff; font-size: 7.6pt; font-weight: 700;
  vertical-align: .35mm;
}
.qt .hint { font-weight: 400; font-size: 7.8pt; color: #6B6257; margin-left: 1mm; }

.scale { display: flex; align-items: center; gap: 3.4mm; margin-top: 2.4mm; }
.scale .end { font-size: 7.4pt; color: #6B6257; }
.scale .dot {
  width: 8.4mm; height: 8.4mm; border: .35mm solid var(--accent); border-radius: 50%;
  background: #fff; color: var(--accent); font-size: 10pt; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}

.opts { display: grid; gap: 1.5mm 4mm; margin-top: 2.2mm; }
.opts.c2 { grid-template-columns: 1fr 1fr; }
.opts.c3 { grid-template-columns: 1fr 1fr 1fr; }
.opt { display: flex; align-items: flex-start; gap: 1.8mm; font-size: 8.8pt; line-height: 1.35; }
.opt::before {
  content: ""; flex: none; width: 3.4mm; height: 3.4mm; margin-top: .5mm;
  border: .32mm solid #6B6257; border-radius: .5mm; background: #fff;
}

.lines { margin-top: 2.6mm; }
.lines span { display: block; height: 7.6mm; border-bottom: .28mm solid #C9C0B0; }
.lines span:last-child { border-bottom-color: #C9C0B0; }

.turn {
  margin-top: auto; padding-top: 2.4mm; font-size: 7.4pt; color: #6B6257;
  display: flex; align-items: center; gap: 1.6mm;
}
.turn::after { content: ""; flex: 1; height: .25mm; background: #D8CFBE; }

/* 背面 */
.back-head { padding-top: 12mm; display: flex; align-items: baseline; gap: 3mm; }
.back-head .k { font-size: 8pt; color: #6B6257; letter-spacing: .08em; }
.back-head h2 {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 15pt; margin: 0; font-weight: 700; letter-spacing: .03em;
}
.back-head em { font-style: normal; font-size: 7.4pt; color: #8A7F6E; }

.rules { margin-top: 4mm; border-top: .35mm solid var(--accent); }
.rule { display: flex; gap: 2.6mm; padding: 2.35mm 0; border-bottom: .22mm solid #DED5C4; }
.rule .n {
  flex: none; width: 5mm; height: 5mm; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 7.6pt; font-weight: 700;
  display: flex; align-items: center; justify-content: center; margin-top: .2mm;
}
.rule p { margin: 0; font-size: 8.9pt; line-height: 1.5; }
.rule b { font-weight: 700; }

.find { margin-top: 3.6mm; background: var(--tint); padding: 3mm 3.2mm; border-radius: 1mm; }
.find .h { font-size: 8pt; font-weight: 700; color: var(--accent); letter-spacing: .04em; margin-bottom: 1.6mm; }
.find .cols { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 3mm; }
.find b { display: block; font-size: 8.4pt; margin-bottom: .6mm; }
.find span { display: block; font-size: 7.2pt; line-height: 1.45; color: #5C554A; }

.obs { margin-top: 9mm; padding-top: 2.4mm; border-top: .25mm dashed #D8CFBE; }
.obs .h { font-size: 7pt; color: #8A7F6E; letter-spacing: .04em; margin-bottom: 1.2mm; }
.obs .row { display: flex; flex-wrap: wrap; gap: 1.4mm 4.2mm; font-size: 7.4pt; color: #5C554A; }
.obs .row span::before {
  content: ""; display: inline-block; width: 2.8mm; height: 2.8mm;
  border: .28mm solid #8C8272; border-radius: .4mm; background: #fff;
  vertical-align: -.3mm; margin-right: .9mm;
}

.foot { margin-top: auto; padding-top: 2.6mm; border-top: .25mm solid #D8CFBE; }
.foot .org { font-size: 7pt; line-height: 1.55; color: #6B6257; }
.foot .meta {
  margin-top: 1.6mm; display: flex; gap: 4mm; font-size: 7.2pt; color: #6B6257;
}
.foot .meta .box { display: inline-block; width: 20mm; border-bottom: .28mm solid #8C8272; height: 3mm; }

@media screen {
  .page { margin: 10mm auto; box-shadow: 0 2mm 6mm rgba(0,0,0,.18); }
}
"""


def render_question(q: dict, no: int) -> str:
    hint = f'<span class="hint">{q["hint"]}</span>' if q.get("hint") else ""
    if q["type"] == "multi" and not q.get("hint"):
        hint = '<span class="hint">（可多选）</span>'
    if q["type"] == "scale":
        dots = "".join(f'<div class="dot">{i}</div>' for i in range(1, 6))
        body = f'<div class="scale">{dots}</div>'
    else:
        cols = "c3" if q.get("cols") == 3 else "c2"
        opts = "".join(f'<div class="opt">{html.escape(o)}</div>' for o in q["options"])
        body = f'<div class="opts {cols}">{opts}</div>'
    return (
        f'<div class="q"><div class="qt"><span class="no">{no}</span>{html.escape(q["title"])}{hint}</div>'
        f"{body}</div>"
    )


def render_front(card: dict) -> str:
    # 正面放 3 道共同题 + 3 道分众题；背面放开放题与六条约定
    qs = COMMON + card["questions"]
    qhtml = "".join(render_question(q, i + 1) for i, q in enumerate(qs))
    return f"""
<div class="page" style="--accent:{card['accent']};--tint:{card['tint']}">
  <div class="band">
    <div class="brand">惠民社区 · 民乐北巷</div>
    <div class="badge">{card['badge']}</div>
  </div>
  <div class="safe">
    <div class="head">
      <h1>{card['title']}</h1>
      <div class="sub">{card['sub']}</div>
    </div>
    <div class="intro">不写姓名和电话，约 2 分钟。请在选项前打 √，开放题写关键词就行。<br>您的话会带到下一次爱邻议事会。</div>
    <div class="info">{card['info']}<br><span class="fill">填写人</span><span class="tick">本人</span><span class="tick">志愿者代记</span></div>
    {qhtml}
    <div class="turn">背面还有一道开放题和六条约定，翻过去看看</div>
  </div>
</div>
"""


def render_back(card: dict) -> str:
    rules = "".join(
        f'<div class="rule"><div class="n">{i}</div><p>{html.escape(r)}</p></div>'
        for i, r in enumerate(RULES, 1)
    )
    q_no = len(COMMON) + len(card["questions"]) + 1
    return f"""
<div class="page" style="--accent:{card['accent']};--tint:{card['tint']}">
  <div class="band">
    <div class="brand">惠民社区 · 民乐北巷</div>
    <div class="badge">{card['badge']}</div>
  </div>
  <div class="safe">
    <div class="back-head">
      <div class="k">停车与通行</div>
      <h2>六条约定</h2>
      <em>讨论稿，以议事会通过文本为准</em>
    </div>
    <div class="rules">{rules}</div>
    <div class="find">
      <div class="h">发现问题找谁</div>
      <div class="cols">
        <div><b>找网格员</b><span>日常巡查、现场劝导</span></div>
        <div><b>发群里</b><span>拍照说明位置，商户群、居民群都行</span></div>
        <div><b>上议事会</b><span>每月一次，反弹点位集中议</span></div>
      </div>
    </div>
    <div class="q">
      <div class="qt"><span class="no">{q_no}</span>{html.escape(card['open'])}</div>
      <div class="lines">{'<span></span>' * card['lines']}</div>
    </div>
    <div class="obs">
      <div class="h">以下由回收人填写</div>
      <div class="row"><span>通道有车</span><span>隔离桩松动/破损</span><span>停放标识不清</span><span>其他</span></div>
    </div>
    <div class="foot">
      <div class="org">主办：成都市温江区公平街道惠民社区居民委员会<br>承办：成都市温江区益嘉仁社会工作服务中心</div>
      <div class="meta"><span>编号 <span class="box"></span></span><span>回收人 <span class="box" style="width:14mm"></span></span><span>日期 <span class="box" style="width:8mm"></span> 月 <span class="box" style="width:8mm"></span> 日</span></div>
    </div>
  </div>
</div>
"""


def build_html() -> str:
    pages = []
    for card in CARDS:
        pages.append(render_front(card))
        pages.append(render_back(card))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>民乐北巷停车长效治理回访卡</title>
<style>{CSS}</style></head>
<body>{''.join(pages)}</body></html>"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    html_path = OUT / "qa-cards.html"
    html_path.write_text(build_html(), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 700, "height": 1000}, device_scale_factor=2)
        page.goto(html_path.as_uri())
        page.wait_for_timeout(400)
        page.pdf(
            path=str(OUT / "qa-cards.pdf"),
            width="154mm",
            height="216mm",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            print_background=True,
            prefer_css_page_size=True,
        )
        pages = page.locator(".page")
        for i in range(pages.count()):
            pages.nth(i).screenshot(path=str(OUT / f"preview-{i + 1:02d}.png"))
        browser.close()

    print("html:", html_path)
    print("pdf :", OUT / "qa-cards.pdf")


if __name__ == "__main__":
    main()
