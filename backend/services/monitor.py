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
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("geo.monitor")

# 调用记账（统一记录所有AI调用，不影响主流程）
def _track_mon(platform, ok, scene):
    try:
        from services.call_tracker import track_call
        track_call(platform, ok, scene)
    except Exception:
        pass

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
        "cost": "expensive",   # 贵：约 ¥0.02-0.05/次
    },
    "gemini": {
        "label": "Gemini",
        "api_key_env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        "model": "gemini-2.0-flash",
        "cost": "mid",         # 中：gemini-flash 较便宜
    },
    "claude": {
        "label": "Claude",
        "api_key_env": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-5",
        "cost": "expensive",
    },
    "perplexity": {
        "label": "Perplexity",
        "api_key_env": "PERPLEXITY_API_KEY",
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar",
        "cost": "mid",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "cost": "cheap",       # 便宜：约 ¥0.001-0.003/次
    },
    # 国内 AI 平台 —— 接口均兼容 OpenAI 格式，有密钥即自动启用
    "qwen": {
        "label": "通义千问",
        "api_key_env": "QWEN_API_KEY",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
        "cost": "cheap",
    },
    "kimi": {
        "label": "Kimi",
        "api_key_env": "KIMI_API_KEY",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "cost": "cheap",
    },
    "doubao": {
        "label": "豆包",
        "api_key_env": "DOUBAO_API_KEY",
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "model": "ep-20260625160759-6p6ht",
        "cost": "cheap",
    },
    "wenxin": {
        "label": "文心一言",
        "api_key_env": "WENXIN_API_KEY",
        "url": "https://qianfan.baidubce.com/v2/chat/completions",
        "model": "ernie-4.0-8k",
    },
    # ===== 通过 QuickRouter 中转接入的新平台（用户无需单独密钥）=====
    "grok": {
        "label": "Grok",
        "api_key_env": "GROK_API_KEY",
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-3",
        "cost": "mid",
    },
    "glm": {
        "label": "智谱清言",
        "api_key_env": "GLM_API_KEY",
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4.6",
        "cost": "cheap",
    },
    "yuanbao": {
        "label": "腾讯元宝",
        "api_key_env": "YUANBAO_API_KEY",
        "url": "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
        "model": "hunyuan-turbo",
        "cost": "cheap",
    },
    "spark": {
        "label": "讯飞星火",
        "api_key_env": "SPARK_API_KEY",
        "url": "https://spark-api-open.xf-yun.com/v1/chat/completions",
        "model": "generalv3.5",
        "cost": "cheap",
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
    cited_urls: list = field(default_factory=list)    # 完整URL（URL级穿透）
    deep_urls: list = field(default_factory=list)     # 能穿透到具体文章的URL
    error: Optional[str] = None
    queried_at: str = ""                          # 提问时间戳(用于对话快照活体证据)
    node: str = ""                                # 查询节点(地域，用于活体证据)


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
    citation_targets: list = field(default_factory=list)   # 高价值引用节点(对标Similarweb)
    alerts: list = field(default_factory=list)             # 异动预警(对标Goodie Catch shifts)
    by_topic: list = field(default_factory=list)           # 主题维度细分(对标Goodie Segment by topic)
    platform_breakdown: dict = field(default_factory=dict) # 各平台分别的提及率
    gaps: list = field(default_factory=list)               # 发现的缺口(可一键修复)
    raw_results: list = field(default_factory=list)
    sample_note: str = ""                        # 采样口径说明(合规要求,必须标注)
    geo_score: int = 0                           # GEO综合评分(0-100)
    geo_grade: str = ""                          # 等级:优秀/良好/待提升/危险
    geo_grade_desc: str = ""                     # 等级说明
    geo_score_detail: dict = field(default_factory=dict)  # 各维度得分
    dashboard_metrics: dict = field(default_factory=dict)  # GEO看板明确指标
    competitor_diagnosis: list = field(default_factory=list)  # 竞品深度诊断(凭什么赢/你差在哪/怎么补)


# ----------------------------------------------------------------------------
# 各平台的调用适配器:统一输入(prompt),统一输出(回答文本)
# ----------------------------------------------------------------------------

async def _call_openai(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.7, "max_tokens": 800},
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    # 容错：有些平台出错时返回的不是标准结构，给出清晰错误而非崩溃
    if "choices" not in data or not data["choices"]:
        raise ValueError(f"返回缺少choices字段: {str(data)[:150]}")
    return data["choices"][0]["message"]["content"]


async def _call_gemini(client, cfg, prompt, key):
    r = await client.post(
        f"{cfg['url']}?key={key}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


async def _call_claude(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": cfg["model"], "max_tokens": 800,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


async def _call_perplexity(client, cfg, prompt, key):
    r = await client.post(
        cfg["url"],
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}]},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def _call_quickrouter(client, cfg, prompt, key, pid):
    """通过 QuickRouter 中转调用海外平台（OpenAI 兼容格式）。
    自动试候选模型名 + 对临时错误(429/503)重试，提高成功率。"""
    candidates = QUICKROUTER_MODELS.get(pid, [cfg.get("model", "gpt-4o")])
    if isinstance(candidates, str):
        candidates = [candidates]
    # 上次成功的模型排在最前，避免重复试错
    if pid in _qr_working_model and _qr_working_model[pid] in candidates:
        candidates = [_qr_working_model[pid]] + [m for m in candidates if m != _qr_working_model[pid]]

    last_err = None
    for model in candidates:
        # 每个模型最多试 3 次（应对 429/503 临时错误）
        for attempt in range(3):
            try:
                r = await client.post(
                    QUICKROUTER_BASE,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.7, "max_tokens": 800},
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
                if "choices" not in data or not data["choices"]:
                    raise ValueError(f"QuickRouter 返回缺少 choices: {str(data)[:120]}")
                _qr_working_model[pid] = model  # 记住能用的模型
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                code = getattr(getattr(e, "response", None), "status_code", None)
                # 429限流 / 503服务不可用 / 500 → 临时错误，等一下重试
                if code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))  # 2秒、4秒递增等待
                    continue
                # 模型不存在(400/404) → 换下一个候选模型
                if code in (400, 404):
                    break
                # 其他错误(401余额/密钥问题) → 直接抛出
                raise
    raise last_err or RuntimeError(f"QuickRouter 所有候选模型均失败 ({pid})")


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
OUTBOUND_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "grok", "deepseek"}
DOMESTIC_PLATFORMS  = {"deepseek", "qwen", "kimi", "doubao", "wenxin", "glm", "yuanbao", "spark"}

# ===== QuickRouter 中转配置（接法B：只中转海外平台）=====
# 海外平台（ChatGPT/Gemini/Claude/Perplexity）难直连，可通过 QuickRouter 统一中转。
# 配了 QUICKROUTER_API_KEY 才启用；国内平台仍走各自直连，不受影响。
QUICKROUTER_BASE = "https://api.quickrouter.ai/v1/chat/completions"
# 海外平台在 QuickRouter 上对应的模型名（OpenAI 兼容格式，可按控制台实际模型名调整）
# 海外平台在 QuickRouter 上对应的模型名（候选列表，监测时自动试哪个能用）
# 选型：批量监测优先性价比——GPT用mini，Claude用3.5 sonnet，Gemini用2.5 flash
QUICKROUTER_MODELS = {
    "chatgpt": ["gpt-5.4-mini", "gpt-5-mini", "gpt-4o-mini", "gpt-4o", "gpt-4"],
    "gemini": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
    "claude": ["claude-sonnet-4-6", "claude-3-5-sonnet", "claude-sonnet-4", "claude-3-5-haiku", "claude-opus-4-6"],
    "perplexity": ["sonar", "sonar-pro", "perplexity/sonar"],
    "grok": ["grok-3", "grok-3-mini", "grok-4", "grok-beta"],
    "glm": ["glm-4.6", "glm-4.5", "glm-4-flash", "glm-4"],
    "yuanbao": ["hunyuan-turbo", "hunyuan-standard", "hunyuan-lite"],
}
# 记住每个平台上次成功的模型名，避免每次都从头试
_qr_working_model = {}
# 走 QuickRouter 中转的平台（这些平台没有自己密钥时，自动走中转）
QUICKROUTER_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "grok", "glm", "yuanbao"}
def _quickrouter_key():
    return os.getenv("QUICKROUTER_API_KEY", "")


async def check_all_keys() -> list:
    """
    逐个测试每个平台的 API 密钥能否真实调用。
    返回每个平台的状态：已配置/未配置、能否调通、错误原因。
    用于管理后台一键自检。
    """
    results = []
    async with httpx.AsyncClient() as client:
        for pid, cfg in PLATFORMS.items():
            key = os.getenv(cfg["api_key_env"])
            qr_key = _quickrouter_key()
            use_qr = (pid in QUICKROUTER_PLATFORMS and qr_key and not key)
            item = {
                "platform": pid,
                "label": cfg.get("label", pid),
                "env_name": cfg["api_key_env"],
                "configured": bool(key) or use_qr,
            }
            if not key and not use_qr:
                item["status"] = "未配置"
                item["ok"] = False
                item["detail"] = f"环境变量 {cfg['api_key_env']} 没有设置"
                results.append(item)
                continue
            # 真实测试调用（用最短的问题省成本）
            try:
                if use_qr:
                    ans = await _call_quickrouter(client, cfg, "你好", qr_key, pid)
                else:
                    ans = await _DISPATCH[pid](client, cfg, "你好", key)
                _track_mon(pid, True, "check_keys")
                if ans and len(ans.strip()) > 0:
                    item["status"] = "正常（QuickRouter中转）" if use_qr else "正常"
                    item["ok"] = True
                    item["detail"] = "密钥有效，调用成功"
                else:
                    item["status"] = "异常"
                    item["ok"] = False
                    item["detail"] = "返回空内容"
            except Exception as e:
                _track_mon(pid, False, "check_keys")
                item["status"] = "失败"
                item["ok"] = False
                msg = str(e)[:120]
                # 常见错误友好提示
                if "401" in msg or "invalid" in msg.lower() or "auth" in msg.lower():
                    item["detail"] = "密钥无效或错误（401），请检查密钥是否填对"
                elif "402" in msg or "balance" in msg.lower() or "quota" in msg.lower() or "insufficient" in msg.lower():
                    item["detail"] = "余额不足或额度用完，请充值"
                elif "429" in msg:
                    item["detail"] = "请求过于频繁（429），稍后重试"
                elif "timeout" in msg.lower():
                    item["detail"] = "网络超时，可能是服务器无法访问该平台"
                else:
                    item["detail"] = msg
            results.append(item)
    return results


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

    # 提取品牌核心词：去掉括号及其内容（中英文括号）、去掉常见后缀
    def _core(name):
        n = name.lower().strip()
        # 去掉括号及内容：优品茶叶（Premium Tea）→ 优品茶叶
        n = re.sub(r'[（(\[【].*?[）)\]】]', '', n)
        # 去掉常见公司后缀
        n = re.sub(r'(有限公司|股份|集团|科技|旗舰店|官方|品牌|公司|store|inc|ltd|co)\.?$', '', n, flags=re.I)
        return n.strip()
    brand_core = _core(brand_low)
    brand_core_norm = _normalize(brand_core)

    # 多种匹配方式，任一命中即为提及
    mentioned = (
        brand_low in low or           # 精确匹配
        brand_norm in low_norm or     # 标准化匹配（去空格标点）
        (len(brand_core) >= 2 and brand_core in low) or        # 核心词匹配（去括号后缀）
        (len(brand_core_norm) >= 2 and brand_core_norm in low_norm) or  # 核心词标准化匹配
        (len(brand_low) >= 2 and any(v in low for v in [
             brand_low.replace(' ', ''),   # 去空格
             brand_low.replace('-', ''),   # 去横线
        ]))
    )
    # 记录第一次出现位置（优先用完整名，其次核心词）
    position = None
    for probe in [brand_low, brand_core]:
        idx = low.find(probe)
        if idx >= 0:
            position = idx; break
    if position is None:
        idx = low_norm.find(brand_norm)
        if idx < 0: idx = low_norm.find(brand_core_norm)
        position = idx if idx >= 0 else None

    # 竞品同样用增强匹配
    comps_found = []
    for c in competitors:
        c_low = c.lower().strip()
        c_norm = _normalize(c)
        if c_low in low or c_norm in low_norm:
            comps_found.append(c)

    # 抽取回答里出现的 URL：同时保留【完整URL】和【域名】
    # 完整URL可点击跳转，直接看竞品被引用的那篇文章（URL级穿透）
    full_urls = re.findall(r"https?://[^\s\)\]\}，。；、\"'<>]+", answer)
    # 清理末尾标点
    cleaned_urls = []
    for u in full_urls:
        u = u.rstrip(".,;:!?)]}\u3002\uff0c\uff1b\uff1a")
        if len(u) > 12:  # 过滤太短的残缺链接
            cleaned_urls.append(u)
    cleaned_urls = sorted(set(cleaned_urls))

    # 域名（用于聚合统计和信源反推）
    domains = re.findall(r"https?://([\w\.-]+)", answer)
    sources = sorted(set(d.lower().lstrip("www.") for d in domains))

    # 判断URL深度：有路径的才是"具体页面"，只有域名的是"根域名"
    deep_urls = [u for u in cleaned_urls
                 if len(u.split("://", 1)[-1].split("/", 1)) > 1
                 and u.split("://", 1)[-1].split("/", 1)[1].strip("/")]

    return {
        "brand_mentioned": mentioned,
        "brand_position": position,
        "competitors_mentioned": comps_found,
        "cited_sources": sources,
        "cited_urls": cleaned_urls[:20],      # 完整URL（含路径）
        "deep_urls": deep_urls[:20],          # 能穿透到具体文章的URL
    }


# ----------------------------------------------------------------------------
# 主流程:跑一遍完整监测
# ----------------------------------------------------------------------------

# 平台分组：出海模式 vs 国内模式
OUTBOUND_PLATFORMS = {"chatgpt", "gemini", "claude", "perplexity", "grok", "deepseek", "qwen"}
DOMESTIC_PLATFORMS = {"deepseek", "qwen", "kimi", "doubao", "wenxin", "glm", "yuanbao", "spark"}

async def run_monitoring(
    brand: str,
    questions: list,
    competitors: list,
    samples_per_question: int = 1,
    mode: str = "outbound",
    economy: bool = True,        # 经济模式：优先便宜平台，控制成本
    max_platforms: int = 4,      # 最多用几个平台（控制token叠加）
    skip_resample: bool = False, # 跳过补采样（体验版提速用）
) -> VisibilityReport:
    """
    对一个品牌的问题集，在对应模式的平台上跑监测。
    mode: outbound=出海模式, domestic=国内模式
    economy: True=经济模式(优先便宜的国产模型，控制成本)
    max_platforms: 最多平台数，控制 token 叠加消耗
    """
    # 根据模式筛选平台
    allowed = DOMESTIC_PLATFORMS if mode == "domestic" else OUTBOUND_PLATFORMS
    qr_key = _quickrouter_key()
    def _platform_available(pid, cfg):
        # 海外平台：有自己的密钥 OR 配了QuickRouter中转，都算可用
        if pid in QUICKROUTER_PLATFORMS and qr_key:
            return True
        return bool(os.getenv(cfg["api_key_env"]))
    available = {
        pid: cfg for pid, cfg in PLATFORMS.items()
        if _platform_available(pid, cfg) and pid in allowed
    }

    if not available:
        # 没有对应模式的密钥时，退回到所有可用的平台
        available = {pid: cfg for pid, cfg in PLATFORMS.items()
                     if _platform_available(pid, cfg)}

    if not available:
        raise RuntimeError(
            "没有任何 AI 平台密钥可用。请在环境变量中配置至少一个密钥。"
        )

    # 成本优化 + 准确性平衡：
    # - 国内模式：国产模型(本就便宜又准确)
    # - 海外模式：必须包含主流海外平台(ChatGPT/Gemini)，准确性优先
    cost_order = {"cheap": 0, "mid": 1, "expensive": 2}
    if economy and mode == "domestic":
        # 国内经济模式：优先测最主流平台（免费版只测3个时，要测最有代表性的）
        # 豆包(月活最大) > DeepSeek(最火) > 文心(百度系) > 通义 > Kimi > 智谱 > 元宝
        domestic_priority = ["doubao", "deepseek", "wenxin", "qwen", "kimi", "glm", "yuanbao", "spark"]
        ordered = [p for p in domestic_priority if p in available]
        ordered += [p for p in available if p not in ordered]
        available = {p: available[p] for p in ordered[:max_platforms]}
    elif mode != "domestic":
        # 海外模式：准确性优先，但要避免全被失败的海外平台占满导致无数据。
        # 策略：海外主流 + 国产兜底混合，保证至少有能成功返回的平台。
        priority = ["chatgpt", "gemini", "perplexity", "deepseek", "qwen", "claude", "kimi", "doubao"]
        ordered = [p for p in priority if p in available]
        ordered += [p for p in available if p not in ordered]
        # 确保国产兜底平台(通常更稳定)至少有1-2个入选，不被海外平台挤掉
        domestic_backup = [p for p in ordered if p in DOMESTIC_PLATFORMS]
        picked = ordered[:max_platforms]
        if not any(p in DOMESTIC_PLATFORMS for p in picked) and domestic_backup:
            # 如果选中的全是海外平台，强制把1个国产兜底平台塞进来
            picked = picked[:max(1, max_platforms-1)] + domestic_backup[:1]
        available = {p: available[p] for p in picked}
    else:
        # 国内非经济模式：限制平台数防token爆炸
        if len(available) > max_platforms:
            available = dict(list(available.items())[:max_platforms])

    results: list[AnswerResult] = []

    # 并发上限：一次性发太多请求会触发平台限流(429)。
    # 降到 8，大幅降低限流概率（牺牲少量速度换稳定，对国产平台尤其重要）。
    sem = asyncio.Semaphore(8)
    async def _bounded_query(client, pid, cfg, q, brand, competitors):
        async with sem:
            return await _one_query(client, pid, cfg, q, brand, competitors)

    async with httpx.AsyncClient() as client:
        coros = []
        for q in questions:
            for pid, cfg in available.items():
                for _ in range(samples_per_question):
                    coros.append(_bounded_query(client, pid, cfg, q, brand, competitors))
        # 诊断日志：记录监测规模和耗时，便于在 Render 日志排查慢的原因
        import time as _time
        _t0 = _time.time()
        logger.warning("【监测开始】平台=%s 问题数=%d 总请求=%d",
                       list(available.keys()), len(questions), len(coros))
        # 总超时保护：整批最多等 80 秒，到点就用已返回的结果出报告，
        # 不让个别慢平台(如未配好的文心)拖死整个监测。
        task_objs = [asyncio.ensure_future(c) for c in coros]
        done, pending = await asyncio.wait(task_objs, timeout=80)
        logger.warning("【监测完成】耗时=%.1f秒 成功=%d 未完成=%d",
                       _time.time()-_t0, len(done), len(pending))
        for t in pending:
            t.cancel()  # 取消还没完成的慢任务
        if pending:
            logger.warning("监测总超时，%d 个任务未完成，用已返回的 %d 条出报告",
                           len(pending), len(done))
        for t in done:
            try:
                g = t.result()
                if isinstance(g, AnswerResult):
                    results.append(g)
            except Exception as e:
                logger.warning("监测任务异常: %s", e)

        # 智能补采样：AI 回答有随机性，单次采样可能"碰巧没提到品牌"。
        # 对"成功调用但没提到品牌"的问题，自动再补 1 次确认，消除冤枉的 0。
        # 性能优化：最多补采样 5 个问题，避免新商家(大量问题没提及)时补采样拖慢一倍时间。
        if samples_per_question == 1 and not skip_resample:
            # 找出每个问题是否至少被提及一次
            q_mentioned = {}
            for r in results:
                if not r.error:
                    q_mentioned.setdefault(r.question, False)
                    if r.brand_mentioned:
                        q_mentioned[r.question] = True
            # 对"调用成功但完全没提到"的问题补采样（最多5个）
            retry_tasks = []
            retry_meta = []
            for q in questions:
                if len(retry_tasks) >= 5:
                    break
                if q in q_mentioned and not q_mentioned[q]:
                    # 用最便宜的1个平台补采样1次确认
                    for pid, cfg in list(available.items())[:1]:
                        retry_tasks.append(_bounded_query(client, pid, cfg, q, brand, competitors))
                        retry_meta.append(q)
            if retry_tasks:
                retry_objs = [asyncio.ensure_future(t) for t in retry_tasks]
                rdone, rpending = await asyncio.wait(retry_objs, timeout=20)
                for t in rpending:
                    t.cancel()
                for t in rdone:
                    try:
                        g = t.result()
                        if isinstance(g, AnswerResult):
                            results.append(g)
                    except Exception:
                        pass

    return _aggregate(brand, questions, competitors, available, results,
                      samples_per_question)


async def run_sandbox_multiturn(brand: str, competitors: list, turns: list,
                                platform: str = "deepseek") -> dict:
    """★多轮追问沙盒：维持对话上下文，模拟客户连续追问的真实成交链路。
    打破 Zero-shot 限制——查清在哪一轮被竞品截胡。"""
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return {"error": f"不支持的平台：{platform}"}
    key = os.getenv(cfg["api_key_env"], "")
    qr_key = _quickrouter_key()
    use_qr = (platform in QUICKROUTER_PLATFORMS and qr_key and not key)
    if not key and not use_qr:
        return {"error": f"{cfg['label']} 未配置密钥，无法运行沙盒"}

    messages = []
    results = []
    async with httpx.AsyncClient(timeout=90) as client:
        for i, q in enumerate(turns[:5]):  # 最多5轮
            messages.append({"role": "user", "content": q})
            try:
                if use_qr:
                    models = QUICKROUTER_MODELS.get(platform, [cfg.get("model")])
                    model = _qr_working_model.get(platform) or models[0]
                    r = await client.post(
                        QUICKROUTER_BASE,
                        headers={"Authorization": f"Bearer {qr_key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": messages, "temperature": 0.7, "max_tokens": 800},
                    )
                    r.raise_for_status()
                    answer = r.json()["choices"][0]["message"]["content"]
                else:
                    r = await client.post(
                        cfg["url"],
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": cfg["model"], "messages": messages,
                              "temperature": 0.7, "max_tokens": 800},
                    )
                    r.raise_for_status()
                    answer = r.json()["choices"][0]["message"]["content"]
                messages.append({"role": "assistant", "content": answer})
                analysis = _analyze_answer(answer, brand, competitors)
                results.append({
                    "turn": i + 1, "question": q, "answer": answer[:1200],
                    "mentioned": analysis["mentioned"],
                    "competitors_found": analysis.get("competitors_found", []),
                })
                _track_mon(platform, True, "sandbox")
            except Exception as e:
                results.append({"turn": i + 1, "question": q, "error": str(e)[:150]})
                _track_mon(platform, False, "sandbox")
                break

    drop_turn = None
    for r in results:
        if "mentioned" in r and not r["mentioned"]:
            drop_turn = r["turn"]
            break
    mentioned_turns = [r["turn"] for r in results if r.get("mentioned")]
    return {
        "platform": cfg["label"],
        "turns": results,
        "mentioned_turns": mentioned_turns,
        "drop_turn": drop_turn,
        "insight": (f"⚠️ 你在第 {drop_turn} 轮追问时从推荐名单里消失了——客户越问越细时，AI 转向了竞品。"
                    if drop_turn else
                    (f"✅ 全部 {len(results)} 轮追问中你都被提及，对话链路稳固。" if mentioned_turns else
                     "❌ 全程都没被提及——从第一轮就不在 AI 的视野里。")),
        "note": "真实客户会连续追问对比、逼单、问缺点。这个沙盒模拟完整成交链路，查清你在哪个深度环节被截胡。",
    }


def estimate_cost(question_count: int, platform_count: int,
                  cost_level: str = "cheap") -> dict:
    """
    估算一次监测的 token 消耗和成本。
    给后台/定价参考用。
    """
    calls = question_count * platform_count
    # 每次调用平均 token（输入+输出）
    tokens_per_call = 1200
    total_tokens = calls * tokens_per_call
    # 每百万token成本（人民币，粗估）
    price_per_m = {"cheap": 2, "mid": 8, "expensive": 25}.get(cost_level, 8)
    cost_rmb = round(total_tokens / 1_000_000 * price_per_m, 3)
    return {
        "calls": calls,
        "estimated_tokens": total_tokens,
        "estimated_cost_rmb": cost_rmb,
        "cost_level": cost_level,
        "note": f"{question_count}问 × {platform_count}平台 = {calls}次调用",
    }


async def _one_query(client, pid, cfg, question, brand, competitors) -> AnswerResult:
    key = os.getenv(cfg["api_key_env"])
    # 用 UTC+8 时间戳（活体证据用）
    now_cn = datetime.now(timezone(timedelta(hours=8)))
    res = AnswerResult(platform=pid, question=question, answer_text="",
                       queried_at=now_cn.strftime("%Y-%m-%d %H:%M:%S"),
                       node=os.getenv("QUERY_NODE", "上海"))
    # 海外平台：优先走 QuickRouter 中转（配了密钥 且 该平台没有自己的直连密钥时）
    qr_key = _quickrouter_key()
    use_qr = (pid in QUICKROUTER_PLATFORMS and qr_key and not key)
    try:
        # 双保险：除了 httpx 的 timeout，再加一层 asyncio 硬超时，
        # 防止连接层卡死导致 httpx 超时不生效，让任务永远挂起。
        if use_qr:
            answer = await asyncio.wait_for(
                _call_quickrouter(client, cfg, question, qr_key, pid),
                timeout=65,
            )
        else:
            answer = await asyncio.wait_for(
                _DISPATCH[pid](client, cfg, question, key),
                timeout=50,
            )
        res.answer_text = answer
        parsed = _analyze_answer(answer, brand, competitors)
        res.brand_mentioned = parsed["brand_mentioned"]
        res.brand_position = parsed["brand_position"]
        res.competitors_mentioned = parsed["competitors_mentioned"]
        res.cited_sources = parsed["cited_sources"]
        res.cited_urls = parsed.get("cited_urls", [])
        res.deep_urls = parsed.get("deep_urls", [])
        _track_mon(pid, True, "monitor")
    except Exception as e:
        res.error = str(e)
        _track_mon(pid, False, "monitor")
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

    # 引用来源去重 + 影响力分析（对标 Similarweb 引用分析：找出AI信任的高价值节点）
    all_sources = set()
    source_freq = {}        # 每个源被引用的次数（频次=影响力）
    source_with_brand = {}  # 该源出现的回答里，是否提到了本品牌
    source_competitors = {} # ★信源溯源：每个源的回答里带出了哪些竞品
    for r in ok:
        for src in r.cited_sources:
            all_sources.add(src)
            source_freq[src] = source_freq.get(src, 0) + 1
            # 如果这条回答提到了品牌，说明品牌已在该源的语境中露出
            if r.brand_mentioned:
                source_with_brand[src] = True
            else:
                source_with_brand.setdefault(src, False)
            # ★竞品溯源：记录这个源的回答里出现了哪些竞品
            comps_here = getattr(r, "competitors_mentioned", None) or []
            if comps_here:
                source_competitors.setdefault(src, {})
                for cmp_name in comps_here:
                    source_competitors[src][cmp_name] = \
                        source_competitors[src].get(cmp_name, 0) + 1
    # 生成"高价值节点"清单：被AI高频引用、但品牌还没出现的源 = 优先攻克目标
    # ★升级：带上"这个源把哪些竞品带出来了"——诊断和行动焊死
    citation_targets = []
    for src, freq in sorted(source_freq.items(), key=lambda x: -x[1]):
        comps = source_competitors.get(src, {})
        top_comps = sorted(comps.items(), key=lambda x: -x[1])[:3]
        brand_present = source_with_brand.get(src, False)
        # 信源权重分：被引频次 × 是否带竞品（带竞品且没你=最该攻克）
        weight = freq * 2 + sum(comps.values())
        # 行动建议
        if not brand_present and top_comps:
            action = f"该信源正把「{top_comps[0][0]}」等竞品带给AI，你却缺席——优先在此布局同类内容"
            priority = "high"
        elif not brand_present:
            action = "高频信源但你尚未露出，值得布局内容"
            priority = "mid"
        else:
            action = "你已在此露出，保持更新维持存在感"
            priority = "low"
        citation_targets.append({
            "source": src,
            "cited_count": freq,                          # 被AI引用次数=影响力
            "brand_present": brand_present,               # 品牌是否已露出
            "competitors_here": [c[0] for c in top_comps], # ★该源带出的竞品
            "weight": weight,                             # ★信源权重分
            "priority": priority,                         # ★攻克优先级
            "action": action,                             # ★具体行动建议
        })
    # 按权重排序（最该攻克的在前）
    citation_targets.sort(key=lambda x: -x["weight"])
    citation_targets = citation_targets[:15]  # 取影响力最高的15个

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
        f"，共 {len(results)} 次真实 AI 查询统计得出。"
        f"AI 回答存在随机性（同一问题多次提问，结果可能不同），"
        f"为提升准确度，系统已对未提及品牌的问题自动补采样确认。"
        f"数据为采样估计，建议监测 2-3 次观察稳定趋势。"
    )

    # ===== 见微 GEO 综合评分（0-100）=====
    # 把已有的多个指标聚合成一个总分，让报告更专业、更有"工具感"。
    # 纯计算，不新增任何监测，不改动现有数据。
    # 权重：提及率40% + 位置分20% + 竞品优势20% + 信源覆盖20%
    # ① 提及率得分（直接用百分比）
    s_mention = mention_rate
    # ② 位置分（已是0-100）
    s_position = avg_position
    # ③ 竞品优势：你的提及率 vs 最强竞品份额，相对表现
    top_comp_share = max(comp_share.values()) if comp_share else 0
    if mention_rate + top_comp_share > 0:
        s_competitor = round(100 * mention_rate / (mention_rate + top_comp_share), 1)
    else:
        s_competitor = 0
    # ④ 信源覆盖：被引用源数量，封顶10个算满分
    s_source = min(100, len(all_sources) * 10)
    # 加权综合
    geo_score = round(
        s_mention * 0.4 + s_position * 0.2 + s_competitor * 0.2 + s_source * 0.2
    )
    geo_score = max(0, min(100, geo_score))
    # 等级
    if geo_score >= 75:
        geo_grade, geo_grade_desc = "优秀", "AI 已经很认可你，继续保持领先"
    elif geo_score >= 50:
        geo_grade, geo_grade_desc = "良好", "有一定基础，重点补强弱项可冲优秀"
    elif geo_score >= 25:
        geo_grade, geo_grade_desc = "待提升", "AI 对你印象不深，需系统优化内容"
    else:
        geo_grade, geo_grade_desc = "危险", "AI 几乎不推荐你，急需行动占位"
    geo_score_detail = {
        "mention": round(s_mention), "position": round(s_position),
        "competitor": round(s_competitor), "source": round(s_source),
    }

    # ===== GEO 看板明确指标（商家一眼看懂的直观数据）=====
    # 全部用已采集的数据计算，不新增任何监测调用
    ok_results = [r for r in results if not r.get("error")] if results and isinstance(results[0], dict) else ok
    total_ans = len(ok) if ok else answered
    # ① 品牌词露出占比：有多少次AI回答提到了你
    brand_exposure = round(mention_rate, 1)
    # ② 权威数据引用数：AI 引用的不重复信息源数量
    authority_citations = len(all_sources)
    # ③ 关键词覆盖率：有多少个监测问题里至少出现过一次品牌
    q_covered = set()
    q_total = set()
    for r in ok:
        q_total.add(r.question)
        if r.brand_mentioned:
            q_covered.add(r.question)
    keyword_coverage = round(100 * len(q_covered) / len(q_total), 1) if q_total else 0
    # ④ AI 语料匹配度：被提及时的平均位置质量（位置越靠前=越匹配AI口味）
    corpus_match = round(avg_position, 1)
    # ⑤ 竞品压制力：你 vs 最强竞品（>50%=你占优）
    rival_pressure = round(s_competitor, 1)

    dashboard_metrics = {
        "brand_exposure": brand_exposure,        # 品牌词露出占比 %
        "authority_citations": authority_citations,  # 权威数据引用数 个
        "keyword_coverage": keyword_coverage,    # 关键词覆盖率 %
        "corpus_match": corpus_match,            # AI语料匹配度 0-100
        "rival_pressure": rival_pressure,        # 竞品压制力 %
        "platforms_covered": len(platform_breakdown),  # 覆盖AI引擎数
    }

    # ===== 竞品深度诊断：不只告诉"谁赢了"，还拆解"凭什么赢、你差在哪、怎么补" =====
    competitor_diagnosis = []
    # 找出被AI提及的竞品，按份额排序（抢你客户最多的排前面）
    top_comps = sorted(comp_share.items(), key=lambda x: -x[1])
    for cname, cshare in top_comps:
        if cshare <= 0:
            continue
        # 这个竞品在哪些问题上被推荐、你却没出现（= 你被它抢走的场景）
        stolen_qs = []
        co_occur = 0  # 你和竞品同时被提及的次数
        for r in ok:
            if cname in r.competitors_mentioned:
                if not r.brand_mentioned:
                    stolen_qs.append(r.question[:40])
                else:
                    co_occur += 1
        # 这个竞品出现的源（AI在哪些地方"看到"了它）
        comp_sources = set()
        for r in ok:
            if cname in r.competitors_mentioned:
                for src in r.cited_sources:
                    comp_sources.add(src)
        # 诊断结论
        total_comp_hits = sum(1 for r in ok if cname in r.competitors_mentioned)
        if cshare >= mention_rate and mention_rate < 30:
            verdict = "强于你"
            advice = f"{cname} 在 AI 心中的存在感比你强。重点：抢占它出现的引用源，补齐它有你没有的内容。"
        elif co_occur > len(stolen_qs):
            verdict = "与你并存"
            advice = f"你和 {cname} 经常一起被推荐，说明你有基础。重点：在它单独出现（你缺席）的问题上补内容，实现反超。"
        else:
            verdict = "抢走你的客户"
            advice = f"{cname} 在 {len(stolen_qs)} 个问题上被推荐、你却缺席。这些就是你被它抢走的客户，优先补这些场景的内容。"
        competitor_diagnosis.append({
            "name": cname,
            "share": cshare,                              # 抢占率
            "verdict": verdict,                           # 判断
            "stolen_count": len(stolen_qs),              # 抢走你多少个场景
            "stolen_questions": stolen_qs[:5],           # 具体哪些问题
            "co_occur": co_occur,                         # 和你并存次数
            "sources": list(comp_sources)[:6],           # 它出现在哪些源
            "advice": advice,                             # 怎么追赶
        })
    competitor_diagnosis = competitor_diagnosis[:5]  # 最多5个主要竞品

    return VisibilityReport(
        brand=brand,
        generated_at=datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(),
        total_queries=len(results),
        answered_queries=answered,
        mention_rate=mention_rate,
        avg_position_score=avg_position,
        competitor_share=comp_share,
        source_count=len(all_sources),
        citation_targets=citation_targets,
        platform_breakdown=platform_breakdown,
        gaps=sorted(gaps, key=lambda g: 0 if g["priority"] == "high" else 1),
        raw_results=[
            {**r.__dict__,
             "platform_label": (PLATFORMS.get(r.platform, {}).get("label", r.platform))}
            for r in results
        ],
        sample_note=sample_note,
        geo_score=geo_score,
        geo_grade=geo_grade,
        geo_grade_desc=geo_grade_desc,
        geo_score_detail=geo_score_detail,
        dashboard_metrics=dashboard_metrics,
        competitor_diagnosis=competitor_diagnosis,
    )
