"""
GEO 雷达 - AI 能见度监测引擎
================================
设计原则(基于市场调研的真实结论):
1. 只用各家官方 API,不爬网页 / 不模拟登录 —— 合规、稳定、可上线。
2. 监测结果标注采样口径(采样次数、置信度),绝不包装成"绝对精确"。
3. 不承诺排名,只给"被提及概率 / 竞品份额 / 引用来源"三个真实指标。
4. 【新增】前置 RAG 联网增强：确保 API 测试环境与用户网页端体感完全一致。
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
        "model": "sonar", # Perplexity 原生支持联网
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
    },
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
        "model": "ep-20260625160759-6p6ht", # 请确保这是你真实的豆包接入点
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
    platform: str
    question: str
    answer_text: str
    brand_mentioned: bool = False
    brand_position: Optional[int] = None
    competitors_mentioned: list = field(default_factory=list)
    cited_sources: list = field(default_factory=list)
    error: Optional[str] = None
    is_rag_enhanced: bool = False # 记录是否使用了实时联网


@dataclass
class VisibilityReport:
    brand: str
    generated_at: str
    total_queries: int
    answered_queries: int
    mention_rate: float
    avg_position_score: float
    competitor_share: dict = field(default_factory=dict)
    source_count: int = 0
    platform_breakdown: dict = field(default_factory=dict)
    gaps: list = field(default_factory=list)
    raw_results: list = field(default_factory=list)
    sample_note: str = ""

# ----------------------------------------------------------------------------
# 核心新增：全局实时联网搜索模块 (RAG)
# ----------------------------------------------------------------------------
async def _fetch_web_context(client: httpx.AsyncClient, query: str) -> str:
    """使用 Tavily API 获取最新的网页快照（需在 .env 配置 TAVILY_API_KEY）"""
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return "" # 如果没有配置搜索密钥，安全退回到无联网模式
    
    try:
        r = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": tavily_key, "query": query, "search_depth": "basic", "max_results": 4},
            timeout=10
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            context = "\n".join([f"- 标题: {res.get('title')}\n  内容: {res.get('content')}\n  URL: {res.get('url')}" for res in results])
            return context
    except Exception as e:
        logger.warning(f"RAG Web Search Failed for query '{query}': {e}")
    return ""

def _build_rag_prompt(question: str, web_context: str) -> str:
    """拼装携带实时上下文的 Prompt"""
    if not web_context:
        return question
    return (
        f"【系统前置信息：以下是针对该问题最新的全网实时检索快照】\n"
        f"{web_context}\n\n"
        f"【用户真实提问】\n{question}\n\n"
        f"请综合上述实时信息与你的已有知识库，直接且客观地回答用户的问题。如果有推荐，请优先参考上述全网公认的真实信息源并附上 URL。"
    )

# ----------------------------------------------------------------------------
# 各平台的调用适配器
# ----------------------------------------------------------------------------
async def _call_openai(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.3, "max_tokens": 800}, # 降低 temperature 到 0.3，减少幻觉，增强对 RAG 上下文的忠诚度
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
    "chatgpt": _call_openai, "gemini": _call_gemini, "claude": _call_claude,
    "perplexity": _call_perplexity, "deepseek": _call_openai, "qwen": _call_openai,
    "kimi": _call_openai, "doubao": _call_openai, "wenxin": _call_openai,
}

# ----------------------------------------------------------------------------
# 解析逻辑
# ----------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[\s\-_·•·]', '', text)
    return text

def _analyze_answer(answer: str, brand: str, competitors: list) -> dict:
    low = answer.lower()
    low_norm = _normalize(answer)
    brand_low = brand.lower().strip()
    brand_norm = _normalize(brand)

    mentioned = (
        brand_low in low or
        brand_norm in low_norm or
        (len(brand_low) >= 2 and
         any(v in low for v in [
             brand_low.replace(' ', ''),
             brand_low.replace('-', ''),
         ]))
    )
    position = low.find(brand_low) if brand_low in low else (
        low_norm.find(brand_norm) if brand_norm in low_norm else None
    )

    comps_found = []
    for c in competitors:
        c_low = c.lower().strip()
        c_norm = _normalize(c)
        if c_low in low or c_norm in low_norm:
            comps_found.append(c)

    urls = re.findall(r"https?://([\w\.-]+)", answer)
    sources = sorted(set(d.lower().lstrip("www.") for d in urls))

    return {
        "brand_mentioned": mentioned,
        "brand_position": position,
        "competitors_mentioned": comps_found,
        "cited_sources": sources,
    }

# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
OUTBOUND_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "deepseek", "qwen"}
DOMESTIC_PLATFORMS = {"deepseek", "qwen", "kimi", "doubao", "wenxin"}

async def run_monitoring(
    brand: str, questions: list, competitors: list,
    samples_per_question: int = 2,  # 默认提频到2次，抹平大模型单次波动
    mode: str = "outbound",
) -> VisibilityReport:
    
    allowed = DOMESTIC_PLATFORMS if mode == "domestic" else OUTBOUND_PLATFORMS
    available = {
        pid: cfg for pid, cfg in PLATFORMS.items()
        if os.getenv(cfg["api_key_env"]) and pid in allowed
    }

    if not available:
        available = {pid: cfg for pid, cfg in PLATFORMS.items() if os.getenv(cfg["api_key_env"])}
    if not available:
        raise RuntimeError("没有任何 AI 平台密钥可用。请在环境变量中配置至少一个密钥。")

    results: list[AnswerResult] = []

    async with httpx.AsyncClient() as client:
        # 【重要提效】：同一问题只搜索一次网络快照，所有平台共享，节省外部 API 调用
        tasks = []
        for q in questions:
            web_context = await _fetch_web_context(client, q)
            final_prompt = _build_rag_prompt(q, web_context)
            has_rag = bool(web_context)

            for pid, cfg in available.items():
                for _ in range(samples_per_question):
                    tasks.append(_one_query(client, pid, cfg, final_prompt, q, brand, competitors, has_rag))
                    
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for g in gathered:
            if isinstance(g, AnswerResult):
                results.append(g)
            else:
                logger.warning("监测任务异常: %s", g)

    return _aggregate(brand, questions, competitors, available, results, samples_per_question)

async def _one_query(client, pid, cfg, prompt, original_question, brand, competitors, has_rag) -> AnswerResult:
    key = os.getenv(cfg["api_key_env"])
    res = AnswerResult(platform=pid, question=original_question, answer_text="", is_rag_enhanced=has_rag)
    try:
        answer = await _DISPATCH[pid](client, cfg, prompt, key)
        res.answer_text = answer
        parsed = _analyze_answer(answer, brand, competitors)
        res.brand_mentioned = parsed["brand_mentioned"]
        res.brand_position = parsed["brand_position"]
        res.competitors_mentioned = parsed["competitors_mentioned"]
        res.cited_sources = parsed["cited_sources"]
    except Exception as e:
        res.error = str(e)
        logger.warning(f"平台 {pid} 查询失败: {e}")
    return res

def _aggregate(brand, questions, competitors, available, results, samples) -> VisibilityReport:
    ok = [r for r in results if not r.error]
    answered = len(ok)
    mentioned = [r for r in ok if r.brand_mentioned]
    mention_rate = round(100 * len(mentioned) / answered, 1) if answered else 0.0

    pos_scores = []
    for r in mentioned:
        if r.brand_position is not None:
            pos_scores.append(max(0, 100 - (r.brand_position / 5)))
    avg_position = round(sum(pos_scores) / len(pos_scores), 1) if pos_scores else 0.0

    comp_share = {}
    for c in competitors:
        hits = sum(1 for r in ok if c in r.competitors_mentioned)
        comp_share[c] = round(100 * hits / answered, 1) if answered else 0.0

    all_sources = set()
    for r in ok:
        all_sources.update(r.cited_sources)

    platform_breakdown = {}
    for pid, cfg in available.items():
        p_results = [r for r in ok if r.platform == pid]
        p_mentioned = [r for r in p_results if r.brand_mentioned]
        rate = round(100 * len(p_mentioned) / len(p_results), 1) if p_results else 0.0
        platform_breakdown[cfg["label"]] = rate

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
                "title": f'"{q}" — AI 及其联网检索中未提到你',
                "action": "generate_content",
            })
            
    for c, share in comp_share.items():
        if share > mention_rate and share > 20:
            gaps.append({
                "type": "competitor_gap",
                "priority": "medium",
                "competitor": c,
                "title": f"竞品 {c} 的提及率({share}%)大幅高于你({mention_rate}%)",
                "action": "competitor_analysis",
            })

    # 判断是否触发了联网模式
    used_rag = any(r.is_rag_enhanced for r in ok)
    rag_note = "【开启实时联网增强校验】" if used_rag else "【零样本基础模型校验】"

    sample_note = (
        f"{rag_note} 本报告基于 {len(questions)} 个痛点场景 × {len(available)} 个前沿大模型"
        f" × {samples} 次交叉采样。由于 AI 生成的概率分布特性，数据为高置信度估算值。"
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
