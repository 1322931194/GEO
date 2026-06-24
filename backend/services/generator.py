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

# 生成统一用一个高质量模型即可,默认走 OpenAI,可在 .env 切换。
GEN_URL = "https://api.openai.com/v1/chat/completions"
GEN_MODEL = os.getenv("GEN_MODEL", "gpt-4o")


async def _chat(prompt: str, system: str = "", json_mode: bool = False) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("生成功能需要 OPENAI_API_KEY,请在 .env 中配置。")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": GEN_MODEL, "messages": messages, "temperature": 0.8}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient() as client:
        r = await client.post(GEN_URL, headers={"Authorization": f"Bearer {key}"},
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


async def generate_questions(
    brand: str,
    industry: str,
    product: str,
    target_market: str = "海外",
    count: int = 50,
) -> list:
    """
    生成品牌专属问题集。返回 [{category, question}] 列表。
    专为出海设计:问题以海外用户真实搜索语境生成(英文场景为主)。
    """
    system = (
        "你是资深的出海品牌 GEO(生成式引擎优化)分析师。"
        "你的任务是模拟海外真实用户在 ChatGPT 等 AI 里会问的、"
        "可能引出品牌推荐的问题。问题要自然、口语化、贴近真实购买决策,"
        "不要出现品牌名本身(我们要测的是 AI 会不会主动提到该品牌)。"
    )
    prompt = f"""
品牌:{brand}
所属行业:{industry}
主营产品/产品线:{product}
目标市场:{target_market}

请生成 {count} 个海外用户可能在 AI 助手里提问的问题,这些问题应当
有机会让 AI 推荐到本行业的品牌。覆盖以下 8 大类目,每类大致均匀分布:
{chr(10).join('- ' + c for c in QUESTION_CATEGORIES)}

要求:
- 问题用目标市场的主要语言书写(海外市场默认英文)。
- 不要在问题里出现 "{brand}" 这个品牌名。
- 每个问题是一句真实用户会打出来的话。

只返回 JSON,格式:{{"questions":[{{"category":"类目名","question":"问题"}}]}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    try:
        data = json.loads(raw)
        return data.get("questions", [])[:count]
    except json.JSONDecodeError:
        logger.error("问题集 JSON 解析失败: %s", raw[:200])
        return []


async def generate_content(
    brand: str,
    gap_question: str,
    product: str,
    content_type: str = "website",
    brand_facts: str = "",
) -> dict:
    """
    针对一个缺口生成可发布内容。
    content_type:
      website  - 官网 FAQ / 产品页内容(AI 最爱引用结构化、权威内容)
      review   - 第三方测评向文章
      social   - 海外社媒原生帖(Reddit/Quora 风格,真实不像广告)
      compare  - 对比文(回应竞品压制)
    返回 {title, body, content_type, publish_tip}
    """
    type_guides = {
        "website": (
            "写一段适合放在官网 FAQ 或产品页的权威内容。结构清晰、有小标题、"
            "事实准确、可被 AI 抓取引用。这是 GEO 最有效的内容形态。"
        ),
        "review": (
            "写一篇客观、第三方视角的测评向文章,真实陈述优缺点,"
            "可信度高,不夸大,适合发在行业媒体。"
        ),
        "social": (
            "写一篇适合发在 Reddit / Quora 的真实用户口吻内容,"
            "自然、不像硬广,像一个真实用户在分享体验。"
            "注意:这是给品牌方人工审核后发布的草稿,不做批量自动发布。"
        ),
        "compare": (
            "写一篇公正的对比文,客观比较本品牌与同类产品,"
            "突出本品牌真实优势,但不贬低、不虚假对比。"
        ),
    }
    guide = type_guides.get(content_type, type_guides["website"])

    system = (
        "你是出海品牌内容策略专家,精通 GEO。你写的内容既要让海外 AI 模型"
        "愿意引用,又要真实、合规、不虚假宣传、不冒用他人商标。"
    )
    prompt = f"""
品牌:{brand}
主营产品:{product}
要补上的缺口问题(AI 目前回答这个问题时没提到该品牌):
"{gap_question}"

品牌已知事实(用于保证内容真实,请只基于这些事实,不要编造):
{brand_facts or "(品牌方未提供额外事实,请只写通用、可验证的内容,不要编造具体数据)"}

内容类型要求:{guide}

请生成内容,用目标市场语言(海外默认英文)。
只返回 JSON,格式:
{{"title":"标题","body":"正文(可含小标题)","publish_tip":"一句话发布建议"}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    try:
        data = json.loads(raw)
        data["content_type"] = content_type
        return data
    except json.JSONDecodeError:
        return {"title": "生成失败", "body": raw, "content_type": content_type,
                "publish_tip": ""}
