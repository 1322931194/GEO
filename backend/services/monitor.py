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
    # 国内 AI 平台 —— 接口均兼容 OpenAI 格式，有密钥即自动启用
    "qwen": {
        "label": "通义千问",
        "api_key_env": "QWEN_API_KEY",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
    },
    "kimi": {
        "label": "Kimi",
        "api_key_env": "KIMI_API_KEY",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
    },
    "doubao": {
        "label": "豆包",
        "api_key_env": "DOUBAO_API_KEY",
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "model": "ep-20260625160759-6p6ht",
    },
    "wenxin": {
        "label": "文心一言",
        "api_key_env": "WENXIN_API_KEY",
        "url": "https://qianfan.baidubce.com/v2/chat/completions",
        "model": "ernie-4.0-8k",
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
    "deepseek": _call_openai,
    "qwen": _call_openai,
    "kimi": _call_openai,
    "doubao": _call_openai,
    "wenxin": _call_openai,   # 文心一言兼容 OpenAI 格式
}

# 平台分组
OUTBOUND_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "deepseek"}
DOMESTIC_PLATFORMS  = {"deepseek", "qwen", "kimi", "doubao", "wenxin"}

def _normalize(text: str) -> str:
    """标准化文本：转小写、去掉空格和常见标点，方便匹配"""
    import unicodedata
    text = text.lower().strip()
    # 去掉中间的空格和常见标点
    text = re.sub(r'[\s\-_·•·]', '', text)
    return text


def _analyze_answer(answer: str, brand: str, competitors: list) -> dict:
    """
    对单条回答做实体解析。
    品牌匹配策略：
    1. 精确小写匹配（最严格）
    2. 去空格匹配（处理 "G X G" 这种情况）
    3. 部分匹配（品牌名超过3字时）
    """
    low = answer.lower()
    # 去掉回答里的空格做标准化匹配
    low_norm = _normalize(answer)
    brand_low = brand.lower().strip()
    brand_norm = _normalize(brand)

    # 多种匹配方式，任一命中即为提及
    mentioned = (
        brand_low in low or           # 精确匹配（已有）
        brand_norm in low_norm or     # 标准化匹配（处理空格/标点问题）
        (len(brand_low) >= 2 and      # 品牌名>=2字时，检查各种变体
         any(v in low for v in [
             brand_low.replace(' ', ''),   # 去空格
             brand_low.replace('-', ''),   # 去横线
         ]))
    )
    position = low.find(brand_low) if brand_low in low else (
        low_norm.find(brand_norm) if brand_norm in low_norm else None
    )

    # 竞品同样用增强匹配
    comps_found = []
    for c in competitors:
        c_low = c.lower().strip()
        c_norm = _normalize(c)
        if c_low in low or c_norm in low_norm:
            comps_found.append(c)

    # 抽取回答里出现的 URL / 来源域名
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

# 平台分组：出海模式 vs 国内模式
OUTBOUND_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "deepseek", "qwen"}
DOMESTIC_PLATFORMS = {"deepseek", "qwen", "kimi", "doubao", "wenxin"}

async def run_monitoring(
    brand: str,
    questions: list,
    competitors: list,
    samples_per_question: int = 1,
    mode: str = "outbound",
) -> VisibilityReport:
    """
    对一个品牌的问题集，在对应模式的平台上跑监测。
    mode: outbound=出海模式(ChatGPT/Gemini等), domestic=国内模式(DeepSeek/通义千问等)
    """
    # 根据模式筛选平台
    allowed = DOMESTIC_PLATFORMS if mode == "domestic" else OUTBOUND_PLATFORMS
    available = {
        pid: cfg for pid, cfg in PLATFORMS.items()
        if os.getenv(cfg["api_key_env"]) and pid in allowed
    }

    if not available:
        # 没有对应模式的密钥时，退回到所有有密钥的平台
        available = {pid: cfg for pid, cfg in PLATFORMS.items()
                     if os.getenv(cfg["api_key_env"])}

    if not available:
        raise RuntimeError(
            "没有任何 AI 平台密钥可用。请在环境变量中配置至少一个密钥。"
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
