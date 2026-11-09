#!/usr/bin/env python3
"""生成智慧菜市食品安全与健康饮食卡片（A6，A5 的一半，双面）。"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "print" / "food-card"

QUESTIONS = [
    {
        "q": "买猪肉，先看什么？",
        "opts": ["颜色越红越好", "检疫章和合格证", "哪块便宜买哪块"],
    },
    {
        "q": "青菜买回家，怎么洗更放心？",
        "opts": ["整棵泡一夜", "流水冲、泡 10 分钟", "用洗洁精泡"],
    },
    {
        "q": "切生肉和切熟食，能用同一块砧板吗？",
        "opts": ["能，反正要下锅", "不能，生熟要分开", "冲一下就能用"],
    },
    {
        "q": "四季豆（豆角）怎么吃才安全？",
        "opts": ["下锅翻两下就吃", "煮熟焖透", "凉拌生吃"],
    },
    {
        "q": "买熟食，哪种更放心？",
        "opts": ["露天摆着、便宜", "有防尘罩、当天做", "颜色特别鲜艳"],
    },
    {
        "q": "一顿饭，菜、肉、饭怎么分？",
        "opts": ["菜一半，肉饭各四分之一", "肉多菜少", "咸菜下饭最省事"],
        "wide": True,
    },
]

ANSWERS = [
    ("1  B", "正规摊位有“两证两章”，买肉看检疫印章和合格证，不买来源不明的肉。"),
    ("2  B", "先冲掉泥土，再浸泡 10 分钟；叶菜掰开洗，别用洗洁精。"),
    ("3  B", "生肉带细菌，刀、砧板、盘子都要生熟分开，避免交叉污染。"),
    ("4  B", "没煮熟的四季豆含皂素，容易中毒；要煮到颜色变暗、没有豆腥味。"),
    ("5  B", "看防尘防蝇设施，问是不是当天做的；有异味、颜色异常鲜艳的不买。"),
    ("6  A", "平衡膳食餐盘：菜一半，肉饭各四分之一；少油少盐少糖。"),
]

CSS = """
@page { size: 111mm 154mm; margin: 0; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  color: #1F2A26; background: #DCD6C8;
}
.page {
  width: 111mm; height: 154mm; position: relative; overflow: hidden;
  background: #FBF7EC; margin: 0; box-shadow: none;
}
.page:not(:last-child) { break-after: page; }
.band {
  position: absolute; left: 0; top: 0; width: 100%; height: 14mm;
  background: #14524A; color: #fff;
}
.band .brand { position: absolute; left: 8mm; top: 0; height: 14mm; display: flex; align-items: center;
  font-size: 7.4pt; letter-spacing: .06em; }
.band .badge { position: absolute; right: 8mm; top: 4mm; height: 6mm; display: flex; align-items: center;
  border: .3mm solid rgba(255,255,255,.75); border-radius: 1mm; padding: 0 2mm;
  font-size: 7pt; font-weight: 700; letter-spacing: .08em; }
.safe { position: absolute; left: 8mm; right: 8mm; top: 8mm; bottom: 8mm;
  display: flex; flex-direction: column; }
.head { padding-top: 10mm; }
.head h1 {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 20pt; line-height: 1.1; margin: 0; font-weight: 700; letter-spacing: .04em;
}
.head .sub { margin-top: 1.4mm; font-size: 7.8pt; color: #5C554A; }
.intro {
  margin-top: 2.8mm; padding: 2mm 2.6mm; border-left: .8mm solid #B5541F;
  background: #F6E3D2; font-size: 7.2pt; line-height: 1.5; color: #3A352D;
}
.q { margin-top: 4.3mm; }
.qt { font-size: 8.8pt; line-height: 1.3; font-weight: 700; }
.qt .no {
  display: inline-flex; align-items: center; justify-content: center;
  width: 4.2mm; height: 4.2mm; margin-right: 1.4mm; border-radius: .7mm;
  background: #B5541F; color: #fff; font-size: 7pt; font-weight: 700; vertical-align: .3mm;
}
.opts { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.2mm 2.4mm; margin-top: 1.8mm; }
.opts.c2 { grid-template-columns: 1fr 1fr; }
.opt { display: flex; align-items: flex-start; gap: 1.4mm; font-size: 7.7pt; line-height: 1.3; }
.opt::before {
  content: ""; flex: none; width: 3mm; height: 3mm; margin-top: .4mm;
  border: .3mm solid #6B6257; border-radius: .4mm; background: #fff;
}
.foot { margin-top: auto; padding-top: 2mm; border-top: .25mm solid #D8CFBE;
  display: flex; justify-content: space-between; align-items: flex-end;
  font-size: 6.6pt; color: #6B6257; line-height: 1.45; }
.foot .score { font-size: 7.4pt; color: #1F2A26; }
.foot .score b { color: #B5541F; }

/* 背面 */
.back-head { padding-top: 10mm; }
.back-head .k { font-size: 7pt; color: #6B6257; letter-spacing: .08em; }
.back-head h2 {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 17pt; margin: 1mm 0 0; font-weight: 700; letter-spacing: .03em;
}
.back-head .note { margin-top: 1.4mm; font-size: 6.8pt; color: #8A7F6E; }
.answers { margin-top: 3.2mm; }
.ans { display: flex; gap: 2.4mm; padding: 1.9mm 0; border-bottom: .2mm solid #DED5C4; }
.ans b { flex: none; width: 9mm; font-size: 7.8pt; color: #14524A; }
.ans span { font-size: 7.2pt; line-height: 1.45; color: #2B2A25; }
.tips { margin-top: 3mm; background: #E7F0EA; padding: 2.6mm 3mm; border-radius: .8mm; }
.tips .h { font-size: 7.6pt; font-weight: 700; color: #14524A; margin-bottom: 1.2mm; }
.tips p { margin: 0 0 1mm; font-size: 7pt; line-height: 1.45; color: #2B2A25; }
.tips p:last-child { margin-bottom: 0; }
.tips b { color: #B5541F; }
.slogan { margin-top: 3mm; padding-left: 2.4mm; border-left: 1mm solid #B5541F;
  font-size: 7.6pt; line-height: 1.5; color: #14524A; font-weight: 700; }
.foot2 { margin-top: auto; padding-top: 2mm; border-top: .25mm solid #D8CFBE;
  font-size: 6.2pt; color: #6B6257; line-height: 1.45; }
"""


def front() -> str:
    qs = []
    for i, item in enumerate(QUESTIONS, 1):
        cls = "opts c2" if item.get("wide") else "opts"
        opts = "".join(f'<div class="opt">{o}</div>' for o in item["opts"])
        qs.append(
            f'<div class="q"><div class="qt"><span class="no">{i}</span>{item["q"]}</div>'
            f'<div class="{cls}">{opts}</div></div>'
        )
    return f"""
<div class="page">
  <div class="band"><div class="brand">惠民社区 · 智慧菜市</div><div class="badge">食安小问答</div></div>
  <div class="safe">
    <div class="head"><h1>买菜六问</h1><div class="sub">答对 4 题，可参加蔬菜套圈</div></div>
    <div class="intro">在选项前打 √，答案在背面。买菜顺路，2 分钟答完。</div>
    {''.join(qs)}
    <div class="foot">
      <div>套圈蔬菜只作游戏道具，不食用。</div>
      <div class="score">答对 <b>____</b> 题</div>
    </div>
  </div>
</div>
"""


def back() -> str:
    ans = "".join(f'<div class="ans"><b>{a}</b><span>{b}</span></div>' for a, b in ANSWERS)
    return f"""
<div class="page">
  <div class="band"><div class="brand">惠民社区 · 智慧菜市</div><div class="badge">答案与提醒</div></div>
  <div class="safe">
    <div class="back-head">
      <div class="k">食安小问答</div>
      <h2>答案与提醒</h2>
      <div class="note">志愿者用，答题时请勿翻面。</div>
    </div>
    <div class="answers">{ans}</div>
    <div class="tips">
      <div class="h">买菜三句话</div>
      <p><b>看证章：</b>肉看检疫印章，熟食看防尘罩。</p>
      <p><b>分开装：</b>生肉、熟食、蔬菜分袋，回家先洗手。</p>
      <p><b>热透吃：</b>剩菜 2 小时内冷藏，吃前热透；四季豆煮透，野生菌不采不买。</p>
    </div>
    <div class="slogan">健康一口诀：一斤菜、半斤果、盐少一盖、油少一勺。</div>
    <div class="foot2">
      主办：成都市温江区公平街道惠民社区居民委员会<br>
      承办：成都市温江区益嘉仁社会工作服务中心<br>
      科普资料，具体以监管部门意见为准。
    </div>
  </div>
</div>
"""


def build() -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>智慧菜市食安小问答</title><style>{CSS}</style></head>
<body>{front()}{back()}</body></html>"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    html_path = OUT / "food-card.html"
    html_path.write_text(build(), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 600, "height": 800}, device_scale_factor=2)
        page.goto(html_path.as_uri())
        page.wait_for_timeout(300)
        page.pdf(
            path=str(OUT / "food-card.pdf"),
            width="111mm",
            height="154mm",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            print_background=True,
            prefer_css_page_size=True,
        )
        pages = page.locator(".page")
        for i in range(pages.count()):
            pages.nth(i).screenshot(path=str(OUT / f"preview-{i + 1:02d}.png"))
        browser.close()
    print("pdf:", OUT / "food-card.pdf")


if __name__ == "__main__":
    main()
