"""
GEO 雷达 - 优化引擎(本次升级的核心)
=====================================
把产品从"监测工具"变成"提升引擎"。三大能力:

1. diagnose_score    评分拆解:把一个模糊的能见度分,拆成几块明确短板,
                     每块标出"拖累了多少分""可提升空间多大"。
2. build_action_plan 任务生成:根据短板,生成排好优先级的 GEO 待办清单,
                     每条写清"做什么、为什么、预计提升多少"。
3. compare_reports   复测对比:对比前后两次监测,算出真实提升,生成证据。

第一性原理依据(AI 凭什么推荐一个品牌):
  A. 训练数据中的存在感(高权重站点的真实提及)  -> 站外信源任务
  B. 实时检索的可抓取性(结构化、直答问题的内容)  -> 官网内容任务
  C. 语义关联强度(品牌与场景词的绑定)            -> 语义绑定任务

诚实底线:所有"预计提升"都是基于优化逻辑的估算区间,绝不承诺确定排名。
"""

import json
import logging

logger = logging.getLogger("geo.optimizer")


# ----------------------------------------------------------------------------
# 1. 评分拆解:把能见度分拆成可干预的短板
# ----------------------------------------------------------------------------

def diagnose_score(report: dict) -> dict:
    """
    输入一份监测报告(monitor.py 产出的 dict),
    输出评分拆解:当前分 + 各短板 + 每块的可提升空间。

    返回 {
      current_score, max_potential,
      factors: [{name, status, lost_points, why}],
    }
    """
    mention_rate = report.get("mention_rate", 0)
    platform_breakdown = report.get("platform_breakdown", {})
    competitor_share = report.get("competitor_share", {})
    source_count = report.get("source_count", 0)
    gaps = report.get("gaps", [])

    factors = []

    # 短板1:内容覆盖(有多少高价值问题完全没提到品牌)—— 对应可抓取性 B
    content_gaps = [g for g in gaps if g.get("type") == "content_gap"]
    if content_gaps:
        lost = min(30, len(content_gaps) * 8)
        factors.append({
            "name": "内容覆盖不足",
            "status": "weak",
            "lost_points": lost,
            "why": f"有 {len(content_gaps)} 个高价值问题,AI 回答时完全没提到你。"
                   f"这些问题缺少你的结构化内容供 AI 抓取引用。",
            "lever": "website",
        })

    # 短板2:站外信源(被引用来源太少)—— 对应训练数据存在感 A
    if source_count < 10:
        lost = min(25, (10 - source_count) * 3)
        factors.append({
            "name": "站外信源薄弱",
            "status": "weak" if source_count < 5 else "medium",
            "lost_points": lost,
            "why": f"AI 只引用了 {source_count} 个提到你的外部站点。"
                   f"Reddit、Quora、行业媒体等高权重站点缺少你的真实声量,"
                   f"导致模型'记不住'你。",
            "lever": "offsite",
        })

    # 短板3:平台不均衡(某些 AI 平台明显落后)—— 对应可抓取性 B
    if platform_breakdown:
        weak_platforms = [name for name, rate in platform_breakdown.items() if rate < 35]
        if weak_platforms:
            lost = min(20, len(weak_platforms) * 6)
            factors.append({
                "name": "部分平台落后",
                "status": "medium",
                "lost_points": lost,
                "why": f"在 {', '.join(weak_platforms)} 上的提及率明显偏低,"
                       f"说明这些平台依赖的信源里你的存在感不足。",
                "lever": "platform",
            })

    # 短板4:竞品压制(竞品提及率高于你)—— 对应语义关联 C
    if competitor_share:
        top_comp = max(competitor_share.items(), key=lambda x: x[1])
        if top_comp[1] > mention_rate:
            lost = min(15, int(top_comp[1] - mention_rate))
            factors.append({
                "name": f"被竞品 {top_comp[0]} 压制",
                "status": "medium",
                "lost_points": lost,
                "why": f"竞品 {top_comp[0]} 的提及率({top_comp[1]}%)高于你"
                       f"({mention_rate}%),说明在用户关心的场景里,"
                       f"AI 更容易联想到它而不是你。",
                "lever": "semantic",
            })

    total_lost = sum(f["lost_points"] for f in factors)
    max_potential = min(95, round(mention_rate + total_lost))

    return {
        "current_score": mention_rate,
        "max_potential": max_potential,
        "total_improvable": total_lost,
        "factors": sorted(factors, key=lambda f: -f["lost_points"]),
    }


# ----------------------------------------------------------------------------
# 2. 任务生成:把短板变成可执行的待办清单
# ----------------------------------------------------------------------------

# 每种杠杆对应的任务模板(写清做什么、为什么、预计提升)
def build_action_plan(report: dict, diagnosis: dict, brand: str) -> list:
    """
    根据评分拆解,生成排好优先级的 GEO 任务清单。
    每条任务:{priority, title, lever, why, expected_lift, action_type, target}
    action_type 决定前端给什么按钮(generate_content / manual_guide)。
    """
    tasks = []
    gaps = report.get("gaps", [])
    content_gaps = [g for g in gaps if g.get("type") == "content_gap"]

    # 任务来源1:每个内容缺口 => 一条"发布官网结构化内容"任务
    for g in content_gaps[:5]:
        q = g.get("question", "")
        tasks.append({
            "priority": "high",
            "lever": "可抓取性",
            "title": f"为「{_short(q)}」发布官网结构化内容",
            "why": "AI 回答这个问题时没提到你,因为缺少能被抓取引用的直答内容。"
                   "发布一篇标题直接对应该问题、用清晰小标题作答的内容,"
                   "能显著提高被引用概率。",
            "expected_lift": "该问题被提及概率 +5~10%",
            "action_type": "generate_content",
            "content_type": "website",
            "target": q,
        })

    # 任务来源2:站外信源薄弱 => 站外声量任务
    if any(f["lever"] == "offsite" for f in diagnosis["factors"]):
        tasks.append({
            "priority": "high",
            "lever": "训练数据存在感",
            "title": "在 Reddit / Quora 建立真实品牌声量",
            "why": "高权重社区(Reddit、Quora)是 AI 模型的重要信源。"
                   "你在这些站点的真实讨论几乎为零,导致模型'记不住'你。"
                   "发布真实、不像硬广的体验帖,长期能进入训练语料。",
            "expected_lift": "被引用来源数 +3~5,长期提升整体记忆度",
            "action_type": "generate_content",
            "content_type": "social",
            "target": f"介绍 {brand} 的真实使用体验",
        })

    # 任务来源3:平台落后 => 针对性内容任务
    pb = report.get("platform_breakdown", {})
    weak = [name for name, rate in pb.items() if rate < 35]
    if weak:
        tasks.append({
            "priority": "medium",
            "lever": "可抓取性",
            "title": f"补强 {', '.join(weak)} 依赖的信源",
            "why": f"这些平台上你的提及率偏低。它们更依赖实时检索和特定信源,"
                   f"针对性地发布权威内容、争取行业媒体引用,可拉平差距。",
            "expected_lift": f"{weak[0]} 提及率 +5~8%",
            "action_type": "manual_guide",
            "target": "platform",
        })

    # 任务来源4:竞品压制 => 语义绑定 + 对比内容任务
    cs = report.get("competitor_share", {})
    if cs:
        top_comp = max(cs.items(), key=lambda x: x[1])
        if top_comp[1] > report.get("mention_rate", 0):
            tasks.append({
                "priority": "medium",
                "lever": "语义关联",
                "title": f"强化与核心场景的绑定,追赶 {top_comp[0]}",
                "why": f"竞品 {top_comp[0]} 在用户关心的场景里被 AI 更频繁联想到。"
                       f"通过发布把你的品牌与这些场景词反复绑定的内容"
                       f"(对比文、场景化测评),能逐步改变模型的语义关联。",
                "expected_lift": f"缩小与 {top_comp[0]} 的差距",
                "action_type": "generate_content",
                "content_type": "compare",
                "target": f"{brand} 在核心使用场景下的优势对比",
            })

    # 排序:high 在前,并标号
    order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda t: order.get(t["priority"], 3))
    for i, t in enumerate(tasks, 1):
        t["step"] = i
    return tasks


# ----------------------------------------------------------------------------
# 3. 复测对比:生成"做了任务之后真的提升了"的证据
# ----------------------------------------------------------------------------

def compare_reports(before: dict, after: dict) -> dict:
    """
    对比前后两次监测报告,算出真实提升,生成可作为续费理由和营销素材的证据。
    """
    def delta(key):
        b = before.get(key, 0) or 0
        a = after.get(key, 0) or 0
        return round(a - b, 1)

    mention_delta = delta("mention_rate")
    source_delta = delta("source_count")

    # 各平台变化
    pf_before = before.get("platform_breakdown", {})
    pf_after = after.get("platform_breakdown", {})
    platform_changes = {}
    for name in set(list(pf_before.keys()) + list(pf_after.keys())):
        platform_changes[name] = round(
            (pf_after.get(name, 0) or 0) - (pf_before.get(name, 0) or 0), 1)

    # 缺口是否减少
    gaps_before = len(before.get("gaps", []))
    gaps_after = len(after.get("gaps", []))

    improved = mention_delta > 0 or source_delta > 0 or gaps_after < gaps_before

    summary = _build_summary(mention_delta, source_delta,
                             gaps_before - gaps_after, improved)

    return {
        "improved": improved,
        "mention_rate_before": before.get("mention_rate", 0),
        "mention_rate_after": after.get("mention_rate", 0),
        "mention_delta": mention_delta,
        "source_delta": source_delta,
        "gaps_closed": max(0, gaps_before - gaps_after),
        "platform_changes": platform_changes,
        "summary": summary,
    }


# ----------------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------------

def _short(text, n=24):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _build_summary(mention_delta, source_delta, gaps_closed, improved):
    if not improved:
        return ("本期数据与上期持平。GEO 优化通常需要 2~4 周才会在 AI 回答中"
                "体现,建议继续完成任务清单后再复测。")
    parts = []
    if mention_delta > 0:
        parts.append(f"AI 提及率提升了 {mention_delta} 个百分点")
    if source_delta > 0:
        parts.append(f"被引用来源增加了 {source_delta} 个")
    if gaps_closed > 0:
        parts.append(f"补上了 {gaps_closed} 个内容缺口")
    return "本期优化见效:" + "、".join(parts) + "。这是你完成 GEO 任务后的真实变化。"
