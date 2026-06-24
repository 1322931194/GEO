"""
GEO 雷达 - AI 能见度监测引擎
================================
这是整个产品的核心。它把品牌的"问题集"逐条丢给各大 AI 模型,
采集回答,解析其中的品牌提及、引用来源、竞品对比,算出能见度分数。

设计原则(基于市场调研的真实结论):
1. 只用各家官方 API,不爬网页 / 不模拟登录 —— 合规、稳定、可上线。
2. 监测结果标注采样口径(采样次数、置信度),绝不包装成"绝对精确"。
3. 不承诺排名,只给"被提及概率 / 竞品份额 / 引用来源"三个真实指标。
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("geo.monitor")

# ----------------------------------------------------------------------------
# 配置:各大 AI 平台的接入点。商家部署时在 .env 填入自己的密钥即可。
# 缺密钥的平台会被自动跳过,不会让整个监测失败。
# ----------------------------------------------------------------------------

PLATFORMS = {
    "chatgpt": {
        "label": "ChatGPT",
        "api_key_env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
    },
    "gemini": {
        "label": "Gemini",
        "api_key_env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        "model": "gemini-2.0-flash",
    },
    "claude": {
        "label": "Claude",
        "api_key_env": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-5",
    },
    "perplexity": {
        "label": "Perplexity",
        "api_key_env": "PERPLEXITY_API_KEY",
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
    },
}


@dataclass
class AnswerResult:
    """单次"某个问题问某个平台"的监测结果。"""
    platform: str
    question: str
    answer_text: str
    brand_mentioned: bool = False
    brand_position: Optional[int] = None        # 品牌在回答中第一次出现的字符位置(越靠前越好)
    competitors_mentioned: list = field(default_factory=list)
    cited_sources: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class VisibilityReport:
    """一次完整监测的汇总报告 —— 这就是商家在首屏看到的那张报告。"""
    brand: str
    generated_at: str
    total_queries: int
    answered_queries: int
    mention_rate: float                          # 被提及概率(%)
    avg_position_score: float                    # 平均位置分(0-100,越靠前分越高)
    competitor_share: dict = field(default_factory=dict)   # 各竞品抢走的份额
    source_count: int = 0
    platform_breakdown: dict = field(default_factory=dict) # 各平台分别的提及率
    gaps: list = field(default_factory=list)               # 发现的缺口(可一键修复)
    raw_results: list = field(default_factory=list)
    sample_note: str = ""                        # 采样口径说明(合规要求,必须标注)


# ----------------------------------------------------------------------------
# 各平台的调用适配器:统一输入(prompt),统一输出(回答文本)
# ----------------------------------------------------------------------------

async def _call_openai(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.7, "max_tokens": 800},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def _call_gemini(client, cfg, prompt, key):
    r = await client.post(
        f"{cfg['url']}?key={key}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


async def _call_claude(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": cfg["model"], "max_tokens": 800,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


async def _call_perplexity(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


_DISPATCH = {
    "chatgpt": _call_openai,
    "gemini": _call_gemini,
    "claude": _call_claude,
    "perplexity": _call_perplexity,
    "deepseek": _call_openai,  # DeepSeek 接口与 OpenAI 完全兼容,复用同一调用方式
}


# ----------------------------------------------------------------------------
# 解析层:从 AI 的回答里抽取品牌提及、竞品、引用来源
# ----------------------------------------------------------------------------

def _analyze_answer(answer: str, brand: str, competitors: list) -> dict:
    """对单条回答做实体解析。返回是否提及品牌、位置、提到了哪些竞品、引用了哪些来源。"""
    low = answer.lower()
    brand_low = brand.lower()

    mentioned = brand_low in low
    position = low.find(brand_low) if mentioned else None

    comps_found = [c for c in competitors if c.lower() in low]

    # 抽取回答里出现的 URL / 来源域名(GEO 的"被引用来源")
    urls = re.findall(r"https?://([\w\.-]+)", answer)
    sources = sorted(set(d.lower().lstrip("www.") for d in urls))

    return {
        "brand_mentioned": mentioned,
        "brand_position": position,
        "competitors_mentioned": comps_found,
        "cited_sources": sources,
    }


# ----------------------------------------------------------------------------
# 主流程:跑一遍完整监测
# ----------------------------------------------------------------------------

async def run_monitoring(
    brand: str,
    questions: list,
    competitors: list,
    samples_per_question: int = 1,
) -> VisibilityReport:
    """
    对一个品牌的问题集,在所有已配置密钥的平台上跑监测。

    samples_per_question: 每个问题重复采样几次(AI 回答有随机性,
                          多采样能提高数据可信度。默认 1,专业版可调高)。
    """
    available = {pid: cfg for pid, cfg in PLATFORMS.items()
                 if os.getenv(cfg["api_key_env"])}

    if not available:
        raise RuntimeError(
            "没有任何 AI 平台密钥可用。请在 .env 中至少配置一个:"
            "OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY / PERPLEXITY_API_KEY"
        )

    results: list[AnswerResult] = []

    async with httpx.AsyncClient() as client:
        tasks = []
        for q in questions:
            for pid, cfg in available.items():
                for _ in range(samples_per_question):
                    tasks.append(_one_query(client, pid, cfg, q, brand, competitors))
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for g in gathered:
            if isinstance(g, AnswerResult):
                results.append(g)
            else:
                logger.warning("监测任务异常: %s", g)

    return _aggregate(brand, questions, competitors, available, results,
                      samples_per_question)


async def _one_query(client, pid, cfg, question, brand, competitors) -> AnswerResult:
    key = os.getenv(cfg["api_key_env"])
    res = AnswerResult(platform=pid, question=question, answer_text="")
    try:
        answer = await _DISPATCH[pid](client, cfg, question, key)
        res.answer_text = answer
        parsed = _analyze_answer(answer, brand, competitors)
        res.brand_mentioned = parsed["brand_mentioned"]
        res.brand_position = parsed["brand_position"]
        res.competitors_mentioned = parsed["competitors_mentioned"]
        res.cited_sources = parsed["cited_sources"]
    except Exception as e:
        res.error = str(e)
        logger.warning("平台 %s 查询失败: %s", pid, e)
    return res


def _aggregate(brand, questions, competitors, available, results,
               samples) -> VisibilityReport:
    """把所有单次结果汇总成商家看到的报告。"""
    ok = [r for r in results if not r.error]
    answered = len(ok)
    mentioned = [r for r in ok if r.brand_mentioned]

    mention_rate = round(100 * len(mentioned) / answered, 1) if answered else 0.0

    # 位置分:品牌出现得越靠前,分越高(0-100)
    pos_scores = []
    for r in mentioned:
        if r.brand_position is not None:
            # 出现在前 200 字 => 满分,越往后越低
            pos_scores.append(max(0, 100 - (r.brand_position / 5)))
    avg_position = round(sum(pos_scores) / len(pos_scores), 1) if pos_scores else 0.0

    # 竞品份额:每个竞品在多少比例的回答里被提到
    comp_share = {}
    for c in competitors:
        hits = sum(1 for r in ok if c in r.competitors_mentioned)
        comp_share[c] = round(100 * hits / answered, 1) if answered else 0.0

    # 引用来源去重
    all_sources = set()
    for r in ok:
        all_sources.update(r.cited_sources)

    # 各平台分别的提及率
    platform_breakdown = {}
    for pid, cfg in available.items():
        p_results = [r for r in ok if r.platform == pid]
        p_mentioned = [r for r in p_results if r.brand_mentioned]
        rate = round(100 * len(p_mentioned) / len(p_results), 1) if p_results else 0.0
        platform_breakdown[cfg["label"]] = rate

    # 缺口诊断:哪些问题完全没提到品牌 => 内容缺口(可一键生成内容修复)
    gaps = []
    miss_by_q = {}
    for r in ok:
        miss_by_q.setdefault(r.question, []).append(r.brand_mentioned)
    for q, flags in miss_by_q.items():
        if not any(flags):
            gaps.append({
                "type": "content_gap",
                "priority": "high",
                "question": q,
                "title": f'"{q}" — AI 回答里没有提到你',
                "action": "generate_content",
            })
    # 竞品压制缺口
    for c, share in comp_share.items():
        if share > mention_rate and share > 20:
            gaps.append({
                "type": "competitor_gap",
                "priority": "medium",
                "competitor": c,
                "title": f"竞品 {c} 的提及率({share}%)高于你({mention_rate}%)",
                "action": "competitor_analysis",
            })

    sample_note = (
        f"本报告基于 {len(questions)} 个问题 × {len(available)} 个平台"
        f" × {samples} 次采样,共 {len(results)} 次真实 AI 查询统计得出。"
        f"AI 回答存在随机性,数据为采样估计,非绝对精确值。"
    )

    return VisibilityReport(
        brand=brand,
        generated_at=datetime.utcnow().isoformat(),
        total_queries=len(results),
        answered_queries=answered,
        mention_rate=mention_rate,
        avg_position_score=avg_position,
        competitor_share=comp_share,
        source_count=len(all_sources),
        platform_breakdown=platform_breakdown,
        gaps=sorted(gaps, key=lambda g: 0 if g["priority"] == "high" else 1),
        raw_results=[r.__dict__ for r in results],
        sample_note=sample_note,
    )
