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

import re

logger = logging.getLogger("geo.generator")

# ----------------------------- 广告法违禁词扫描 -----------------------------
# 基于《广告法》绝对化用语 + 常见违禁词（真实检测，安抚商家风控焦虑）
BANNED_WORDS = [
    # 绝对化用语
    "国家级", "世界级", "最高级", "最佳", "最好", "第一", "唯一", "顶级", "极致",
    "最强", "最优", "最先进", "最大", "最低", "最高", "首个", "独一无二", "绝无仅有",
    "史上最", "全网最", "全国第一", "世界第一", "100%", "百分百", "绝对", "永久",
    # 医疗保健违禁（医美/保健行业高危）
    "根治", "治愈", "痊愈", "疗效", "药到病除", "包治", "彻底解决", "无副作用",
    "纯天然无害", "包好", "无效退款"[:0],  # 占位
    # 虚假承诺
    "稳赚", "零风险", "包过", "保证赚", "一夜暴富", "躺赚",
]
BANNED_WORDS = [w for w in BANNED_WORDS if w]

def compliance_scan(text: str) -> dict:
    """
    扫描内容里的广告法违禁词。
    返回是否通过 + 命中的词 + 建议。
    """
    if not text:
        return {"passed": True, "hits": [], "count": 0}
    hits = []
    for w in BANNED_WORDS:
        if w in text:
            hits.append(w)
    hits = list(dict.fromkeys(hits))  # 去重
    return {
        "passed": len(hits) == 0,
        "hits": hits,
        "count": len(hits),
        "scanned_at": "2026最新广告法违禁词库",
    }


def _gen_config():
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if deepseek_key:
        return {
            "key": deepseek_key,
            "url": "https://api.deepseek.com/v1/chat/completions",
            "model": os.getenv("GEN_MODEL", "deepseek-chat"),
            "supports_json_mode": False,  # DeepSeek 不支持 response_format
        }
    if openai_key:
        return {
            "key": openai_key,
            "url": "https://api.openai.com/v1/chat/completions",
            "model": os.getenv("GEN_MODEL", "gpt-4o"),
            "supports_json_mode": True,
        }
    return None


def _safe_parse_json(raw: str) -> dict:
    """
    健壮的 JSON 解析：处理 AI 返回内容带 markdown 代码块、
    多余文字、或格式不规范的情况。
    """
    if not raw:
        return {}
    # 去掉 markdown 代码块标记
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 直接尝试解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取第一个 { } 块
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    logger.error("JSON 解析失败，原始内容: %s", raw[:300])
    return {}


async def _chat(prompt: str, system: str = "", json_mode: bool = False) -> str:
    cfg = _gen_config()
    if not cfg:
        raise RuntimeError("生成功能需要 DEEPSEEK_API_KEY 或 OPENAI_API_KEY，请在环境变量中配置。")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if json_mode:
        prompt = prompt + "\n\n重要：只返回纯 JSON，不要任何额外文字、不要 markdown 代码块。"
    messages.append({"role": "user", "content": prompt})
    body = {"model": cfg["model"], "messages": messages, "temperature": 0.7}
    if json_mode and cfg.get("supports_json_mode"):
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                cfg["url"],
                headers={"Authorization": f"Bearer {cfg['key']}"},
                json=body,
                timeout=120,  # 加长超时，问题生成需要更多时间
            )
            # 详细的错误提示
            if r.status_code == 401:
                raise RuntimeError("AI 密钥无效或已过期，请在 Render Environment 中检查 DEEPSEEK_API_KEY 或 OPENAI_API_KEY")
            if r.status_code == 402:
                raise RuntimeError("AI 账户余额不足，请充值后重试")
            if r.status_code == 429:
                raise RuntimeError("AI 请求频率过高，请稍等 1 分钟后重试")
            if r.status_code >= 500:
                raise RuntimeError(f"AI 服务暂时不可用（{r.status_code}），请稍后重试")
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            raise RuntimeError("AI 响应超时（超过120秒），请稍后重试。问题较多时正常，可缩短问题数量后重试")
        except httpx.ConnectError:
            raise RuntimeError("无法连接到 AI 服务，请检查网络或稍后重试")


# 出海场景类目（英文场景）
OUTBOUND_CATEGORIES = [
    "品类推荐（用户问'最好的XX是什么'）",
    "对比评测（用户问'A和B哪个好'）",
    "使用场景（用户问'适合XX场景的产品'）",
    "信任与口碑（用户问'XX品牌靠谱吗'）",
    "价格与价值（用户问'XX值不值得买'）",
    "功能与规格（用户问'XX有什么功能'）",
    "替代方案（用户问'XX的平替/替代品'）",
    "购买决策（用户问'新手买XX怎么选'）",
]

# 国内场景类目（中文场景，适合本地品牌/连锁店）
DOMESTIC_CATEGORIES = [
    "品牌推荐（用户问'哪家XX比较好'）",
    "服务对比（用户问'A和B的区别是什么'）",
    "价格咨询（用户问'XX大概要多少钱'）",
    "口碑评价（用户问'XX怎么样/靠谱吗'）",
    "选购建议（用户问'第一次选XX要注意什么'）",
    "避坑指南（用户问'买XX有什么坑要注意'）",
    "场景适配（用户问'我的情况适合选哪种XX'）",
    "售后服务（用户问'XX的售后/质保怎么样'）",
]

# 兼容旧代码
QUESTION_CATEGORIES = OUTBOUND_CATEGORIES


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
    data = _safe_parse_json(raw)
    if not data or "keywords" not in data:
        return {"keywords": {"features": [product], "scenarios": [], "users": [], "concerns": []}, "summary": f"{industry}品牌"}
    return data


async def generate_questions(
    brand: str,
    industry: str,
    product: str,
    target_market: str = "海外",
    count: int = 50,
    brand_facts: str = "",
    mode: str = "outbound",
) -> list:
    """
    生成品牌专属问题集。
    mode=outbound: 出海模式，英文问题+中文翻译，监测 ChatGPT/Gemini 等
    mode=domestic: 国内模式，纯中文问题，监测 DeepSeek/通义千问/豆包/Kimi
    """
    categories = DOMESTIC_CATEGORIES if mode == "domestic" else OUTBOUND_CATEGORIES

    # 关键词提取失败时用默认值继续，不中断整个问题生成
    try:
        kw_data = await extract_brand_keywords(brand, industry, product, brand_facts)
    except Exception as e:
        logger.warning("品牌关键词提取失败（将用默认值继续）: %s", e)
        kw_data = {}
    keywords = kw_data.get("keywords", {})
    summary = kw_data.get("summary", f"{industry}品牌")

    features = "、".join(keywords.get("features", [product])[:4])
    scenarios = "、".join(keywords.get("scenarios", [])[:3])
    concerns = "、".join(keywords.get("concerns", [])[:4])
    users = "、".join(keywords.get("users", [])[:2])

    if mode == "domestic":
        # 国内模式：纯中文问题，贴近国内用户在 DeepSeek/通义千问里的真实提问
        system = (
            "你是资深的国内品牌 AI 能见度分析师。"
            "你的任务是模拟国内真实用户在 DeepSeek、通义千问、豆包、Kimi 里会问的问题。"
            "问题必须口语化、贴近中国用户的真实表达习惯，与品牌的具体特征高度相关。"
            "不要出现品牌名本身。"
        )
        prompt = f"""
品牌定位：{summary}
核心产品特征：{features}
主要使用场景：{scenarios if scenarios else '日常使用'}
目标用户：{users if users else '普通消费者'}
用户核心关注点：{concerns if concerns else '质量、性价比'}

基于以上品牌特征，生成 {count} 个国内用户在 DeepSeek/通义千问等 AI 里会真实提问的问题。
问题必须：
1. 用中文写，口语化，像真实用户打出来的话
2. 与品牌的具体特征和场景高度相关
3. 不包含品牌名
4. 覆盖以下 8 大类目，每类均匀分布：
{chr(10).join('- ' + c for c in categories)}

每个问题返回：
- category：中文类目名
- question：中文问题（用于 AI 监测，国内用户真实会问的话）
- question_cn：同上（国内模式 question 和 question_cn 一致）

只返回 JSON：
{{"questions":[{{"category":"类目名","question":"中文问题","question_cn":"中文问题"}}]}}
"""
    else:
        # 出海模式：英文问题+中文翻译
        system = (
            "你是资深的出海品牌 GEO（生成式引擎优化）分析师。"
            "你的任务是基于品牌的真实特征，模拟海外真实用户在 ChatGPT 等 AI 里会问的问题。"
            "问题必须与品牌的具体特征、使用场景、用户关注点高度相关。"
            "不要出现品牌名本身。同时提供中文翻译供中国商家查看。"
        )
        prompt = f"""
品牌定位：{summary}
核心产品特征：{features}
主要使用场景：{scenarios if scenarios else '日常使用'}
目标用户：{users if users else '普通消费者'}
用户核心关注点：{concerns if concerns else '质量、性价比'}
目标市场：{target_market}

基于以上品牌特征，生成 {count} 个海外真实用户在 ChatGPT/Gemini 等 AI 里会问的问题。
问题必须：
1. 用英文写，口语化，是海外用户真实会打出来的话
2. 与品牌的具体特征和场景高度相关
3. 不包含品牌名
4. 覆盖以下 8 大类目，每类均匀分布：
{chr(10).join('- ' + c for c in categories)}

每个问题返回：
- category：中文类目名（品类推荐/对比评测/使用场景等）
- question：英文问题（用于 AI 监测）
- question_cn：对应中文翻译（让中国商家看懂）

只返回 JSON：
{{"questions":[{{"category":"类目名","question":"英文问题","question_cn":"中文翻译"}}]}}
"""

    raw = await _chat(prompt, system, json_mode=True)
    data = _safe_parse_json(raw)
    qs = data.get("questions", [])[:count]
    for q in qs:
        if not q.get("question_cn"):
            q["question_cn"] = q.get("question", "")
    if not qs:
        logger.error("问题集为空，原始返回: %s", raw[:300])
    return qs


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
        "你是出海品牌内容策略专家,精通 GEO。你写的内容既要让 AI 模型"
        "愿意引用,又要真实、合规。严格遵守以下合规红线:"
        "①不虚假宣传、不编造数据或荣誉;"
        "②不使用'第一''最佳''国家级''顶级'等违反广告法的绝对化用语;"
        "③不冒用他人商标、不贬低竞品;"
        "④不夸大功效(尤其食品、保健、医疗类不得宣称疗效);"
        "⑤只基于品牌提供的真实事实创作,事实不足时写通用、可验证的内容。"
    )
    prompt = f"""
品牌:{brand}
主营产品:{product}
要补上的缺口问题(AI 目前回答这个问题时没提到该品牌):
"{gap_question}"

品牌已知事实(用于保证内容真实,请只基于这些事实,不要编造):
{brand_facts or "(品牌方未提供额外事实,请只写通用、可验证的内容,不要编造具体数据)"}

内容类型要求:{guide}

【合规要求】内容必须真实、客观,不含绝对化用语(第一/最/顶级等),不夸大功效,不虚假对比。这是给品牌方人工审核后使用的草稿。

请生成内容,用目标市场语言(海外默认英文,国内中文)。
只返回 JSON,格式:
{{"title":"标题","body":"正文(可含小标题)","publish_tip":"一句话发布建议","compliance_note":"一句话合规提示(如:本内容为草稿,发布前请核实数据真实性)"}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    data = _safe_parse_json(raw)
    if not data or "body" not in data:
        # 如果解析失败，把原始内容作为 body 返回，至少商家能看到内容
        data = {"title": "已生成内容", "body": raw, "content_type": content_type, "publish_tip": ""}
    data["content_type"] = content_type
    # 合规扫描（真实检测广告法违禁词）
    scan = compliance_scan((data.get("title", "") + " " + data.get("body", "")))
    data["compliance"] = scan
    return data
