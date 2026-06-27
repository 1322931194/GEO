"""
GEO 雷达 - 关键词商机分析引擎
================================
帮商家回答一个核心问题：我这个行业，哪些关键词值得在 AI 时代抢占？

核心逻辑：
1. 基于行业/品牌，AI 生成该行业最有商业价值的关键词
2. 对每个关键词评估：AI热度、竞争度、商机评分、内容建议
3. 按商机评分排序，告诉商家优先抢哪些词

诚实边界：
- 没有付费搜索量API，热度是AI基于训练知识的相对评估（1-100分）
- 给的是"哪个词更值得做"的相对排序，不是精确搜索量
- 这对商家决策足够有用：他们要的是优先级，不是绝对数字
"""

import json
import logging
from services.generator import _chat, _safe_parse_json

logger = logging.getLogger("geo.keyword")


async def analyze_keyword_opportunities(
    industry: str,
    brand_name: str = "",
    product: str = "",
    mode: str = "outbound",
    count: int = 12,
) -> dict:
    """
    生成行业关键词商机表。
    返回每个关键词的：词本身、AI热度、竞争度、商机评分、推荐内容方向、为什么。
    """
    lang = "英文" if mode == "outbound" else "中文"
    market = "海外市场（用户用ChatGPT/Perplexity等英文AI搜索）" if mode == "outbound" else "国内市场（用户用DeepSeek/豆包等中文AI搜索）"

    system = "你是资深的GEO（生成式引擎优化）和关键词商业分析专家，擅长评估关键词在AI搜索时代的商业价值。你的评估基于真实的行业认知，诚实、专业、可执行。"

    prompt = f"""
为以下品牌/行业，分析在 AI 搜索时代最值得抢占的 {lang} 关键词商机。

行业：{industry}
品牌：{brand_name or '（通用行业分析）'}
主营产品：{product or '（未指定）'}
目标市场：{market}

请生成 {count} 个该行业用户最可能去问 AI 的高价值关键词/问题短语。
对每个关键词，给出专业评估：

1. keyword: 关键词或问题短语（{lang}）
2. ai_heat: AI热度评分(1-100)，代表有多少用户会就这个词问AI。考虑：是否高频需求、是否决策类问题、用户基数
3. competition: 竞争度评分(1-100)，代表这个词的AI回答里已经有多少品牌在抢。越高越red ocean
4. opportunity_score: 商机评分(1-100)，综合 = 高热度+低竞争才高分。这是核心排序依据
5. intent: 用户意图类型，从["购买决策","对比研究","信息了解","问题解决","品牌寻找"]里选一个
6. content_direction: 针对这个词，建议商家创作什么内容方向（一句话，具体可执行）
7. reason: 为什么这个词值得/不值得做（一句话，点明商机或风险）

排序：按 opportunity_score 从高到低排列，让商家一眼看到最该抢的词。

只返回JSON：
{{"keywords":[{{"keyword":"","ai_heat":85,"competition":40,"opportunity_score":78,"intent":"购买决策","content_direction":"","reason":""}}],"summary":"一句话总结这个行业的关键词商机格局","top_advice":"给商家的一句话核心建议：优先抢哪类词"}}
"""

    raw = await _chat(prompt, system, json_mode=True, scene="opportunity")
    data = _safe_parse_json(raw)
    if not data or "keywords" not in data:
        return {"error": True, "message": "分析失败，请重试"}

    # 数据清洗 + 分级标签
    for kw in data.get("keywords", []):
        score = kw.get("opportunity_score", 0)
        heat = kw.get("ai_heat", 0)
        comp = kw.get("competition", 0)
        # 商机等级
        if score >= 75:
            kw["grade"] = "黄金词"
            kw["grade_color"] = "gold"
        elif score >= 55:
            kw["grade"] = "潜力词"
            kw["grade_color"] = "green"
        elif score >= 35:
            kw["grade"] = "一般词"
            kw["grade_color"] = "gray"
        else:
            kw["grade"] = "红海词"
            kw["grade_color"] = "red"
        # 蓝海/红海标签
        if heat >= 60 and comp <= 40:
            kw["tag"] = "🔵 蓝海机会（高需求·低竞争）"
        elif heat >= 60 and comp > 60:
            kw["tag"] = "🔴 红海激战（高需求·高竞争）"
        elif heat < 40:
            kw["tag"] = "⚪ 小众长尾"
        else:
            kw["tag"] = "🟢 稳健选择"

    # 按商机评分排序兜底
    data["keywords"] = sorted(
        data.get("keywords", []),
        key=lambda k: k.get("opportunity_score", 0),
        reverse=True
    )

    # 统计
    kws = data["keywords"]
    data["stats"] = {
        "total": len(kws),
        "gold_count": sum(1 for k in kws if k.get("grade") == "黄金词"),
        "blue_ocean_count": sum(1 for k in kws if "蓝海" in k.get("tag", "")),
        "avg_opportunity": round(sum(k.get("opportunity_score", 0) for k in kws) / len(kws)) if kws else 0,
    }
    data["error"] = False
    return data
