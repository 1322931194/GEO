"""
GEO 雷达 - 生成服务
====================
两个商家最常用的"省事"功能:
1. generate_questions: 输入品牌+行业,自动生成"你的客户会在 AI 里问的问题集"。
   商家不用自己想问题,只需勾选/微调。
2. generate_content: 输入一个缺口(某个 AI 没提到你的问题),
   生成一篇可直接发布的官网/媒体/社媒内容,把缺口补上。

合规边界:
- 生成的内容必须真实、不虚假宣传、不冒用他人商标。
- 社媒内容只生成"建议稿",由商家人工审核后发布,不自动铺量。
"""

import os
import json
import logging

import httpx

logger = logging.getLogger("geo.generator")

# 生成服务支持 DeepSeek 和 OpenAI。
# 优先用 DeepSeek(更便宜、国内可用);若只配了 OpenAI 则自动用 OpenAI。
# DeepSeek 的接口格式与 OpenAI 完全兼容,只是服务器地址和模型名不同。
def _gen_config():
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if deepseek_key:
        return {
            "key": deepseek_key,
            "url": "https://api.deepseek.com/v1/chat/completions",
            "model": os.getenv("GEN_MODEL", "deepseek-chat"),
        }
    if openai_key:
        return {
            "key": openai_key,
            "url": "https://api.openai.com/v1/chat/completions",
            "model": os.getenv("GEN_MODEL", "gpt-4o"),
        }
    return None


async def _chat(prompt: str, system: str = "", json_mode: bool = False) -> str:
    cfg = _gen_config()
    if not cfg:
        raise RuntimeError("生成功能需要 DEEPSEEK_API_KEY 或 OPENAI_API_KEY,请在环境变量中配置。")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": cfg["model"], "messages": messages, "temperature": 0.8}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient() as client:
        r = await client.post(cfg["url"], headers={"Authorization": f"Bearer {cfg['key']}"},
                              json=body, timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# 出海场景的 8 大问题类目(对应原图 Step 2 的"覆盖8大问题类目")
QUESTION_CATEGORIES = [
    "品类推荐(用户问'最好的XX是什么')",
    "对比评测(用户问'A 和 B 哪个好')",
    "使用场景(用户问'适合XX场景的产品')",
    "信任与口碑(用户问'XX品牌靠谱吗')",
    "价格与价值(用户问'XX值不值得买')",
    "功能与规格(用户问'XX有什么功能')",
    "替代方案(用户问'XX的平替/替代品')",
    "购买决策(用户问'新手买XX怎么选')",
]


async def extract_brand_keywords(
    brand: str,
    industry: str,
    product: str,
    brand_facts: str = "",
) -> dict:
    """
    第一步：从品牌信息里提取核心关键词和特征。
    返回给前端让商家确认，再用于生成精准问题。
    """
    system = "你是品牌分析专家，擅长提炼品牌的核心卖点和用户关心的维度。"
    facts_section = f"\n官网信息摘要:\n{brand_facts[:1000]}" if brand_facts else ""
    prompt = f"""
品牌名: {brand}
行业: {industry}
主营产品: {product}{facts_section}

请分析这个品牌，提取以下信息：
1. 核心产品特征（3-5个，用户最关心的）
2. 主要使用场景（3-4个）
3. 目标用户群体（2-3类）
4. 可能的竞品类型（不需要具体品牌名）
5. 用户购买前最常问的问题类型（3-5个方向）

只返回 JSON:
{{"keywords":{{"features":["特征1","特征2"],"scenarios":["场景1","场景2"],"users":["用户群1"],"concerns":["关注点1"]}},"summary":"一句话品牌定位"}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    try:
        return json.loads(raw)
    except Exception:
        return {"keywords": {"features": [product], "scenarios": [], "users": [], "concerns": []}, "summary": f"{industry}品牌"}


async def generate_questions(
    brand: str,
    industry: str,
    product: str,
    target_market: str = "海外",
    count: int = 50,
    brand_facts: str = "",
) -> list:
    """
    生成品牌专属问题集。
    - 先提取品牌关键词，再基于真实特征生成问题
    - 返回中英双语: [{category, question, question_cn, keywords_used}]
    - question: 英文原文，用于向 AI 发起真实监测查询
    - question_cn: 中文翻译，方便中国商家看懂
    """
    # 先提取品牌特征关键词，让问题更精准
    kw_data = await extract_brand_keywords(brand, industry, product, brand_facts)
    keywords = kw_data.get("keywords", {})
    summary = kw_data.get("summary", f"{industry}品牌")

    features = "、".join(keywords.get("features", [product])[:4])
    scenarios = "、".join(keywords.get("scenarios", [])[:3])
    concerns = "、".join(keywords.get("concerns", [])[:4])
    users = "、".join(keywords.get("users", [])[:2])

    system = (
        "你是资深的出海品牌 GEO（生成式引擎优化）分析师。"
        "你的任务是基于品牌的真实特征，模拟海外真实用户在 ChatGPT 等 AI 里会问的问题。"
        "问题必须与品牌的具体特征、使用场景、用户关注点高度相关，"
        "不能是泛泛的行业问题。同时提供中文翻译供中国商家查看。"
    )
    prompt = f"""
品牌定位: {summary}
核心产品特征: {features}
主要使用场景: {scenarios if scenarios else '日常使用'}
目标用户: {users if users else '普通消费者'}
用户核心关注点: {concerns if concerns else '质量、性价比'}
目标市场: {target_market}

基于以上品牌特征，生成 {count} 个海外真实用户在 ChatGPT/Gemini 等 AI 里会问的问题。
这些问题必须：
1. 与品牌的具体特征和场景高度相关（不要泛泛的行业问题）
2. 是用户在准备购买或了解时会真实问的（口语化、自然）
3. 不包含品牌名（测 AI 会不会主动提到该品牌）
4. 覆盖以下 8 大类目，每类均匀分布：
{chr(10).join('- ' + c for c in QUESTION_CATEGORIES)}

每个问题返回：
- category: 中文类目名（品类推荐/对比评测/使用场景/信任口碑/价值评估/功能规格/替代方案/购买决策）
- question: 英文问题（海外用户真实会问的话，用于 AI 监测）
- question_cn: 中文翻译（让中国商家看懂这个问题是什么意思）

只返回 JSON:
{{"questions":[{{"category":"类目名","question":"英文问题","question_cn":"中文翻译"}}]}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    try:
        data = json.loads(raw)
        qs = data.get("questions", [])[:count]
        # 确保每条都有中文翻译
        for q in qs:
            if not q.get("question_cn"):
                q["question_cn"] = q.get("question", "")
        return qs
    except json.JSONDecodeError:
        logger.error("问题集 JSON 解析失败: %s",
