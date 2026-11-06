# M2 分类规则层 — 实施规格

仓库：`/tmp/sw-probe`（分支 main）。**不要 push，不要碰 `/srv/social-work`。**

## 背景

`scripts/scan-inbox.py` 归档分类时唯一调用 LLM 的地方是 `classify()`（约 :229-276）：读 `ROOT/.env` 的 LLM_ENDPOINT/LLM_API_KEY/LLM_MODEL，POST `/chat/completions`，解析 JSON 得到 `{project, category, confidence}`。

目标：在 `classify()` 之前加一层**纯代码关键词规则**，判得了就省掉一次 LLM 调用；判不了返回 None 交 LLM。

**最高原则：宁可交 LLM，不可判错。规则层判错率必须为 0。**

## 新增文件

### 1. `config/project_aliases.yaml`

```yaml
# 项目识别关键词（M2 规则层）
#
# 归档分类时先跑规则：文件名 + 正文预览里命中哪个项目的关键词，就判给哪个项目。
# 判不了（没命中 / 多个项目同分）→ 返回 None，交 LLM。
#
# key 用 feishu-flow.yaml 里 projects 的 key（huadu / huimin / shared）。
# 「共享」不靠关键词，走下面 fallback。

huadu:
  - 花都
  - 世纪光华
  - 光华小区
huimin:
  - 惠民
  - 民乐北巷
  - 公平街道

# 兜底：没有任何项目命中，但类别命中这里列的类别 → 用该项目
fallback:
  shared:
    - 政策文件
```

### 2. `config/category_keywords.yaml`

```yaml
# 类别识别关键词（M2 规则层）
#
# key 必须是 feishu-flow.yaml 里 categories 的六个之一。
# 「其他」没有关键词，规则永远不会判它 —— 落 LLM。

发票票据:
  - 发票
  - 收据
  - 报销
  - 采购
  - 费用
  - 票据
项目计划:
  - 方案
  - 申报
  - 立项
  - 策划
  - 预算
  - 申请书
项目执行:
  - 推文
  - 活动
  - 会议
  - 纪要
  - 进展
  - 总结
  - 培训
日常工作佐证:
  - 台账
  - 签到
  - 走访
  - 名单
  - 入户
  - 宣传栏
  - 微网实格
  - 关爱
  - 特殊人群
政策文件:
  - 政策
  - 法规
  - 通知
  - 办法
  - 标准
  - 规定
其他: []
```

### 3. `config/rule_weights.yaml`

```yaml
# 规则层权重：文件名命中 vs 正文预览命中
#
# 文件名是人工起的标签，一个命中就应压过正文里的偶然提及；
# 正文预览是原始内容，提及多、噪声高，只能用来补项目名。
filename: 10
preview: 1
```

### 4. `scripts/classify_rules.py`

```python
"""M2 分类规则层：在 LLM 分类之前先用关键词规则判一次。

判得了 → 省一次 LLM 调用；判不了（没命中 / 最高分并列）→ 返回 None，交 LLM。

设计原则：宁可交 LLM，不可判错。
- 文件名是人工起的标签，权重高（默认 10）
- 正文预览是原始内容，提及多、噪声高，权重低（默认 1）
- 必须唯一最高分才算命中；并列最高 → 判不了
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULTS = {"filename": 10, "preview": 1}


def load_rules(config_dir):
    """读 config/ 下的 project_aliases.yaml + category_keywords.yaml + rule_weights.yaml。"""
    config_dir = Path(config_dir)
    with open(config_dir / "project_aliases.yaml", encoding="utf-8") as f:
        proj_raw = yaml.safe_load(f) or {}
    with open(config_dir / "category_keywords.yaml", encoding="utf-8") as f:
        categories = yaml.safe_load(f) or {}
    with open(config_dir / "rule_weights.yaml", encoding="utf-8") as f:
        weights = yaml.safe_load(f) or {}

    fallback = proj_raw.pop("fallback", None) or {}
    projects = {k: v for k, v in proj_raw.items() if isinstance(v, list)}
    w = dict(DEFAULTS)
    w.update(weights)
    return {"projects": projects, "categories": categories,
            "fallback": fallback, "weights": w}


def _hits(text, keywords):
    if not text:
        return 0
    return sum(text.count(k) for k in keywords if k)


def _best(scores):
    """唯一最高分且 >0 才返回 key；否则 None（没命中或并列）。"""
    ranked = sorted(((v, k) for k, v in scores.items() if v > 0), reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def classify_rule(name, preview, rules, projects=None, categories=None):
    """规则分类。判不了返回 None。

    name       文件名（含扩展名）
    preview    正文预览，可为空串
    rules      load_rules() 的返回值
    projects   合法项目 key 列表（feishu-flow.yaml 的 projects），用于过滤
    categories 合法类别列表，用于过滤
    """
    if not isinstance(name, str):
        name = ""
    if not isinstance(preview, str):
        preview = ""

    proj_kw = rules.get("projects") or {}
    cat_kw = rules.get("categories") or {}
    if projects:
        proj_kw = {k: v for k, v in proj_kw.items() if k in projects}
    if categories:
        cat_kw = {k: v for k, v in cat_kw.items() if k in categories}

    wf = (rules.get("weights") or {}).get("filename", DEFAULTS["filename"])
    wp = (rules.get("weights") or {}).get("preview", DEFAULTS["preview"])

    p_scores = {k: _hits(name, kw) * wf + _hits(preview, kw) * wp
                for k, kw in proj_kw.items()}
    project = _best(p_scores)

    c_scores = {k: _hits(name, kw) * wf + _hits(preview, kw) * wp
                for k, kw in cat_kw.items()}
    category = _best(c_scores)

    # 兜底：没判出项目，但类别命中 fallback 规则 → 用 fallback 指定的项目
    if project is None and category is not None:
        for proj, cats in (rules.get("fallback") or {}).items():
            if category in (cats or []):
                project = proj
                break

    if project is None or category is None:
        return None
    return {"project": project, "category": category, "confidence": 1.0,
            "reason": "rule", "scores": {"project": p_scores, "category": c_scores}}
```

### 5. `tests/test_classify_rules.py`（新增）

先读 `tests/test_repo.py` 看风格，保持一致。至少覆盖：

- `load_rules` 能读到 repo 的 `config/`，三个文件都在
- 纯文件名判定：`花都社区世纪光华小区特殊人群关爱入户名单.xlsx` + 空 preview → `huadu` / `日常工作佐证`
- 正文补项目名：`2026-11-03-公众号推文-B版-温暖生活.docx` + preview 含「惠民社区」→ `huimin` / `项目执行`
- **正文不能推翻文件名的类别**：`2026-11-05-公众号推文-惠民.docx` + preview 里同时含「推文」「走访」「台账」「方案」→ 必须是 `huimin` / `项目执行`（不能判成 日常工作佐证）
- 没命中 → None（例：`IMG_20261104_1234.jpg` + 空 preview）
- 并列 → None（例：`花都惠民联合活动方案.docx`）
- fallback：`2026-11-05-关于规范社区党建经费使用的通知.docx` + 空 preview → `shared` / `政策文件`
- `其他` 永远不命中 → None

**再加一个真实回放测试**（验收数据）。硬编码这 4 条真值，逐个跑 `classify_rule`，断言「规则判出来的必须全对」（判错率 0），并打印命中率：

| 文件名 | 真值 project | 真值 category |
|---|---|---|
| `2026-11-04-宣传栏-微网实格.jpg` | huadu | 日常工作佐证 |
| `花都社区世纪光华小区特殊人群关爱入户名单.xlsx` | huadu | 日常工作佐证 |
| `2026-11-03-公众号推文-B版-温暖生活.docx` | huimin | 项目执行 |
| `2026-11-05-公众号推文-惠民.docx` | huimin | 项目执行 |

preview 这样取：docx/xlsx 用 `scan_inbox.extract()`（把 `scripts/` 加进 sys.path 后 `import scan_inbox`；import 时会跑 `_apply_config()`，有内置默认值，正常），jpg 用空串。取不到就空串，不要因为取不到 preview 让测试挂掉。

## 要改的文件

### 6. `scripts/scan-inbox.py`

先读 `main()`（约 :414-500）和 `classify()`（约 :229-276）再动手。

- 顶部 import 区加 `import classify_rules`（同目录脚本，参考文件里已有的 `_load_convert_md()` 做法）
- 在 `main()` 里加载一次规则（放在分类循环之前）：

```python
    rules = None
    try:
        import config as _config
        _cfg = _config.load_config()
        _cfg_dir = Path(_cfg.path).parent if getattr(_cfg, "path", None) else ROOT / "config"
        if _cfg_dir.is_dir():
            rules = classify_rules.load_rules(_cfg_dir)
            print(f"· 规则层已加载：{_cfg_dir}")
    except Exception as e:
        print(f"! 规则层加载失败，本次走 LLM：{e}", file=sys.stderr)
```

- 在原来调 `classify(...)` 的那一处（约 :496），改成先规则后 LLM：

```python
            rule = None
            if rules is not None:
                rule = classify_rules.classify_rule(name, preview, rules, projects, categories)
            if rule:
                project, category, confidence = rule["project"], rule["category"], 1.0
                print(f"  · 规则判定 {project} / {category}")
            else:
                project, category, confidence = classify(
                    name, preview, llm, projects, categories, verbose)
                if verbose:
                    print(f"  · LLM 判定 {project} / {category} ({confidence})")
```

保持原有的缩进和后续逻辑（`confidence < min_confidence` 时转 `_待确认/`）不变。变量名以文件里实际的为准，别硬套这里命名。

### 7. `docs/流程review.md`

把 A2 段（分类 LLM 单播）改写成「✅ 已修（M2）」，加一句：规则层判不了才走 LLM，判错率 0；命中率见 `tests/test_classify_rules.py` 的回放。

### 8. `README.md`

在仓库结构或数据流里提一句：`config/project_aliases.yaml`、`config/category_keywords.yaml`、`config/rule_weights.yaml` 是 M2 规则层配置，改关键词不用改代码。

## 交付要求

- `cd /tmp/sw-probe && python3 tests/test_repo.py` 必须全过（原来 9 项）
- 新测试要能直接跑通（没装 pytest 就用 `python3 tests/test_classify_rules.py` 的形式）
- **把回放测试的真实命中率打印出来**，在最终回复里明确说明：4 条里规则判对了几条、哪几条判不了
- `git add -A && git commit -m "M2: 分类规则层，判不了才走 LLM"`，**不要 push**

## 约束

- 不要碰 `/srv/social-work`
- 不要 push
- 不要改 `.gitignore`、`.env.example`、`config/feishu-flow.yaml.example`
- 不要引入新的第三方依赖（PyYAML 已在用）
- 代码注释用中文，风格跟现有脚本一致
