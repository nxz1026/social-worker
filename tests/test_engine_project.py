

# ---------------- 第 17 项关键词放宽（2026-11-20） ----------------

def test_item17_matches_evaluation_report_variants():
    """实际文件叫「中期质效评估报告」，只认「自评报告」会漏。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from engine.project_checks import PROJECT_CHECKLIST

    kw = [it for it in PROJECT_CHECKLIST if it["no"] == 17][0]["keywords"]
    variants = [
        "花都保障资金中期质效评估报告.docx",
        "中期评估报告.docx",
        "质效评估报告2026.docx",
        "项目中期自评报告.docx",
        "2026中期检查.docx",
        "结项自评材料.docx",
    ]
    for name in variants:
        assert any(k in name for k in kw), f"{name} 应命中第 17 项"


def test_item17_does_not_match_unrelated_names():
    """放宽关键词不能误伤无关文件。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from engine.project_checks import PROJECT_CHECKLIST

    kw = [it for it in PROJECT_CHECKLIST if it["no"] == 17][0]["keywords"]
    for name in ["工作总结.docx", "会议记录.docx", "报销单.xlsx", "随便一个名字.docx"]:
        assert not any(k in name for k in kw), f"{name} 不该命中第 17 项"
