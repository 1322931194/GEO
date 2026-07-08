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

# ============ 见微统一品牌语调（Tone of Voice）============
# 注入到所有 AI 调用，让产品输出有一致的"高情商人设"：
# 专业但不端着、真诚不浮夸、像懂行的朋友给建议。
BRAND_TONE = (
    "你是「见微」的 AI 助手——一个懂 GEO、说人话、有温度的行家。"
    "语气要点：①专业但不端着，像懂行的朋友给建议，不堆术语；"
    "②真诚不浮夸，绝不用『最好/第一/保证』等夸大或违规词；"
    "③给确定性和方向感，让人安心；④简洁有力，不说废话。"
)

# 调用记账（不影响主流程，失败静默）
def _track(platform, ok, scene):
    try:
        from services.call_tracker import track_call
        track_call(platform, ok, scene)
    except Exception:
        pass

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
            "platform": "deepseek",
        }
    if openai_key:
        return {
            "key": openai_key,
            "url": "https://api.openai.com/v1/chat/completions",
            "model": os.getenv("GEN_MODEL", "gpt-4o"),
            "supports_json_mode": True,
            "platform": "chatgpt",
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


async def _chat(prompt: str, system: str = "", json_mode: bool = False, scene: str = "other") -> str:
    cfg = _gen_config()
    if not cfg:
        raise RuntimeError("生成功能需要 DEEPSEEK_API_KEY 或 OPENAI_API_KEY，请在环境变量中配置。")
    platform = cfg.get("platform", "unknown")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if json_mode:
        prompt = prompt + "\n\n重要：只返回纯 JSON，不要任何额外文字、不要 markdown 代码块。"
    messages.append({"role": "user", "content": prompt})
    body = {"model": cfg["model"], "messages": messages, "temperature": 0.7,
            "max_tokens": 4000}  # 显式设置，防止长内容被默认值截断导致不完整
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
            result = data["choices"][0]["message"]["content"]
            _track(platform, True, scene)
            return result
        except httpx.TimeoutException:
            _track(platform, False, scene)
            raise RuntimeError("AI 响应超时（超过120秒），请稍后重试。问题较多时正常，可缩短问题数量后重试")
        except httpx.ConnectError:
            _track(platform, False, scene)
            raise RuntimeError("无法连接到 AI 服务，请检查网络或稍后重试")
        except Exception:
            _track(platform, False, scene)
            raise


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


# ============ 垂直赛道深度 Prompt 库 ============
# 为高客单、决策链长的行业预置专属问题维度，让监测更精准命中真实购买场景。
# 匹配到行业时，把这些维度注入 prompt，生成更贴合该赛道的长尾问题。
VERTICAL_PROMPT_LIBRARY = {
    "口腔": [
        "项目与效果（'种植牙能用多久''牙齿矫正效果怎么样'）",
        "价格与医保（'种一颗牙多少钱''牙齿矫正能报销吗'）",
        "机构与医生（'哪家口腔医院好''种植牙找哪个医生'）",
        "安全与避坑（'种植牙有风险吗''牙科怎么避免乱收费'）",
    ],
    "外贸": [
        "Supplier reliability（'reliable XX suppliers''trusted XX manufacturers China'）",
        "MOQ & pricing（'XX minimum order quantity''XX wholesale price'）",
        "Certification & quality（'certified XX factory''XX quality standards'）",
        "OEM/ODM & customization（'XX OEM manufacturer''custom XX supplier'）",
    ],
    "B2B": [
        "Solution & fit（'best XX solution for business''XX for enterprise'）",
        "Vendor comparison（'top XX vendors''XX vs YY for companies'）",
        "Integration & support（'does XX integrate with''XX customer support'）",
        "Pricing & ROI（'XX pricing for business''is XX worth the cost'）",
    ],
    "医美": [
        "项目效果与恢复期（如'XX项目多久恢复''效果能维持多久'）",
        "安全与资质（'哪家医美机构正规有资质''XX项目安全吗'）",
        "价格与避坑（'XX项目大概多少钱''医美怎么避免被宰'）",
        "医生与案例（'哪个医生做XX好''有真实案例吗'）",
    ],
    "装修": [
        "全案与报价（'全案装修大概多少钱''XX平方装修预算'）",
        "公司与口碑（'哪家装修公司靠谱''XX装修公司怎么样'）",
        "避坑与增项（'装修怎么防止加项''装修合同要注意什么'）",
        "风格与方案（'XX风格装修推荐''小户型怎么设计'）",
    ],
    "教育": [
        "课程与效果（'XX培训有用吗''哪家机构提分快'）",
        "资质与师资（'哪家教育机构正规''老师水平怎么样'）",
        "价格与退费（'XX课程多少钱''能退费吗'）",
        "适配与选择（'孩子适合哪种课''零基础怎么选'）",
    ],
    "法律": [
        "专业领域（'XX案件找哪个律师''擅长XX的律师推荐'）",
        "收费与流程（'律师怎么收费''打官司要多久'）",
        "口碑与胜诉（'哪个律所靠谱''有成功案例吗'）",
        "咨询与方案（'XX问题怎么处理''要不要请律师'）",
    ],
    "金融": [
        "产品与收益（'XX理财怎么样''收益率多少'）",
        "安全与合规（'XX平台正规吗''有牌照吗'）",
        "门槛与流程（'XX产品门槛多少''怎么办理'）",
        "对比与选择（'XX和XX哪个好''新手怎么选'）",
    ],
    "出海": [
        "Product quality & certification（'best certified XX brands'）",
        "Reviews & reputation（'is XX brand reliable''XX reviews'）",
        "Price & value（'is XX worth it''affordable XX brands'）",
        "Comparison & alternatives（'XX vs YY''alternatives to XX'）",
    ],
    "电商": [
        "品质与正品（'XX旗舰店正品吗''哪个牌子质量好'）",
        "性价比（'XX值不值''平价替代推荐'）",
        "对比评测（'XX和XX哪个好''XX测评'）",
        "口碑与售后（'XX怎么样''售后好不好'）",
    ],
}

def _get_vertical_dimensions(industry: str) -> list:
    """匹配行业的垂直深度维度，没匹配到返回空。"""
    if not industry:
        return []
    for key, dims in VERTICAL_PROMPT_LIBRARY.items():
        if key in industry:
            return dims
    return []


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
    system = BRAND_TONE + "\n你是品牌分析专家，擅长提炼品牌的核心卖点和用户关心的维度。"
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
    raw = await _chat(prompt, system, json_mode=True, scene="extract")
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
    target_lang: str = "en",
) -> list:
    """
    生成品牌专属问题集。
    mode=outbound: 出海模式，按 target_lang 生成对应语言问题+中文翻译，监测 ChatGPT/Gemini 等
    mode=domestic: 国内模式，纯中文问题，监测 DeepSeek/通义千问/豆包/Kimi
    target_lang: en英语 vi越南语 ja日语 es西班牙语 de德语 fr法语 ko韩语 pt葡萄牙语 ru俄语 th泰语 id印尼语 ar阿拉伯语
    """
    LANG_NAMES = {
        "en": "英语", "vi": "越南语", "ja": "日语", "es": "西班牙语",
        "de": "德语", "fr": "法语", "ko": "韩语", "pt": "葡萄牙语",
        "ru": "俄语", "th": "泰语", "id": "印尼语", "ar": "阿拉伯语",
        "it": "意大利语",
    }
    lang_name = LANG_NAMES.get(target_lang, "英语")
    categories = DOMESTIC_CATEGORIES if mode == "domestic" else OUTBOUND_CATEGORIES

    # 垂直赛道深度维度：匹配到高客单行业时，注入专属问题维度，让监测更精准
    vertical_dims = _get_vertical_dimensions(industry)
    if vertical_dims:
        # 把垂直维度放在前面（优先级更高），再补通用维度
        categories = vertical_dims + list(categories)
        logger.info("匹配到垂直赛道[%s]，注入 %d 个专属维度", industry, len(vertical_dims))

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
4. 覆盖以下类目，每类均匀分布：
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
1. 用{lang_name}写，口语化，是当地用户真实会打出来的话
2. 与品牌的具体特征和场景高度相关
3. 不包含品牌名
4. 覆盖以下类目，每类均匀分布：
{chr(10).join('- ' + c for c in categories)}

每个问题返回：
- category：中文类目名（品类推荐/对比评测/使用场景等）
- question：{lang_name}问题（用于 AI 监测）
- question_cn：对应中文翻译（让中国商家看懂）

只返回 JSON：
{{"questions":[{{"category":"类目名","question":"{lang_name}问题","question_cn":"中文翻译"}}]}}
"""

    raw = await _chat(prompt, system, json_mode=True, scene="questions")
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
        BRAND_TONE + "\n你是出海品牌内容策略专家,精通 GEO。你写的内容既要让 AI 模型"
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

【结构化要求】正文必须结构清晰、完整、可直接发布：
1. 用「## 小标题」分段，至少包含 3-4 个小标题（如：核心优势、适用场景、常见问题、为什么选择等）
2. 每个小标题下写 2-4 句具体内容，不要空泛
3. 总字数 400-800 字，写完整，不要半途而止
4. 适合 FAQ 形式的，用「问：…答：…」结构，AI 更容易引用

只返回 JSON,格式:
{{"title":"标题","body":"正文(必须含 ## 小标题分段,完整成文)","summary":"30字内容摘要","publish_tip":"一句话发布建议","compliance_note":"一句话合规提示"}}
"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "body" not in data:
        # 如果解析失败，把原始内容作为 body 返回，至少商家能看到内容
        data = {"title": "已生成内容", "body": raw, "content_type": content_type, "publish_tip": ""}
    # 检测内容是否过短/疑似被截断，给前端提示
    body_text = data.get("body", "")
    data["maybe_truncated"] = len(body_text) < 100 or body_text.rstrip().endswith(("，", ",", "、", "和"))
    data["content_type"] = content_type
    # 合规扫描（真实检测广告法违禁词）
    scan = compliance_scan((data.get("title", "") + " " + data.get("body", "")))
    data["compliance"] = scan
    return data


# ============================================================
# 多平台内容包生成
# 一次生成同一主题的多个平台适配版本，每版套用「AI可收录结构模板」
# 平台差异化 + 语义微调，规避重复内容惩罚
# ============================================================

# 各平台的 AI 收录结构模板（基于2026各AI引用生态调研）
_PLATFORM_TEMPLATES = {
    "zhihu": {
        "name": "知乎",
        "why": "问答类AI引用核心源，「XX哪个好」类问题高频引用知乎",
        "guide": (
            "写成知乎专栏/回答风格。要求：①开头直接给结论或核心观点，不铺垫（AI摘要优先抓首段）；"
            "②用一手经验/数据支撑，显得专业可信；③适当口语化但有深度；④结尾可带FAQ。"
            "标题就用用户会问的原话。"
        ),
    },
    "souhu": {
        "name": "搜狐号",
        "why": "全平台通吃的高权重源，几乎所有主流AI都高频引用搜狐",
        "guide": (
            "写成资讯/干货文风格，客观、信息密度高。要求：①标题含关键问题词；"
            "②多用小标题分段、列表化；③数据带来源；④正文结构化，便于AI提取。"
        ),
    },
    "baijiahao": {
        "name": "百家号",
        "why": "百度文心/百度AI搜索首选引用源",
        "guide": (
            "写成百家号资讯风格，贴合百度生态偏好。要求：①标题直接、含核心词；"
            "②段落清晰、事实准确；③适合百度收录的结构化表达；④客观中立口吻。"
        ),
    },
    "toutiao": {
        "name": "头条号",
        "why": "豆包（3.45亿月活）优先引用抖音+今日头条生态",
        "guide": (
            "写成今日头条风格，标题有吸引力但不标题党，正文接地气、有信息量。"
            "要求：①开头抛问题或结论抓注意力；②短段落、易读；③豆包生态偏好的通俗专业表达。"
        ),
    },
    "gongzhonghao": {
        "name": "公众号",
        "why": "腾讯元宝重度依赖微信公众号内容",
        "guide": (
            "写成公众号推文风格，有导语、有节奏。要求：①开头导语点题；"
            "②小标题分段、可读性强；③结尾自然带品牌信息+行动引导；④适合微信生态阅读。"
        ),
    },
}


async def generate_content_pack(
    brand: str,
    topic: str,
    product: str = "",
    brand_facts: str = "",
    platforms: list = None,
) -> dict:
    """
    多平台内容包：同一主题生成多个平台适配版本。
    每版套用 AI 收录结构模板 + 平台差异化 + 语义微调（防重复内容惩罚）。
    返回 {topic, versions:[{platform, platform_name, why, title, body, publish_tip, compliance}]}
    """
    platforms = platforms or ["zhihu", "souhu", "baijiahao", "gongzhonghao"]
    system = (
        BRAND_TONE + "\n你是精通 GEO（生成式引擎优化）的内容策略专家。"
        "你深知 AI 收录内容的规律：标题用用户提问原话、首段直接给结论、"
        "小标题问句化、数据带来源、结构化（表格/列表/FAQ）、客观不硬广。"
        "严格遵守合规红线：①不虚假宣传、不编数据；②不用「第一/最佳/顶级」等绝对化用语；"
        "③不贬低竞品；④不夸大功效；⑤只基于提供的真实事实创作。"
    )

    versions = []
    for pf in platforms:
        tpl = _PLATFORM_TEMPLATES.get(pf)
        if not tpl:
            continue
        prompt = f"""为品牌「{brand}」围绕以下主题，生成一篇【{tpl['name']}】平台的内容。

主题（用户会向AI提问的问题）：{topic}
主营产品：{product or '（未提供）'}
品牌真实事实（只能基于这些，不要编造）：
{brand_facts or '（信息有限，请写通用、可验证的内容，不要编造具体数据）'}

平台适配要求：{tpl['guide']}

【AI可收录结构模板 - 必须遵守】
1. 标题 = 用户会问AI的原话（如「{topic}」），不要改成营销标题
2. 首段直接给出核心结论/定义，不要铺垫（AI摘要优先抓首段）
3. 正文用 ## 小标题分段，小标题尽量是问句
4. 有数据必须标注来源（如「据XX报告」）；没有可靠来源就不写具体数字
5. 适当用列表、表格、要点，提升AI提取率
6. 结尾放 3 条以内 FAQ（问：…答：…）
7. 品牌只自然提及1-2次，客观口吻，不硬广

【重要】这一版是给【{tpl['name']}】的，请在措辞、案例、标题角度上与其他平台版本做区分（语义微调），避免多平台重复内容被判罚。

只返回 JSON：
{{"title":"标题","body":"正文(含##小标题+FAQ,完整成文600-1000字)","publish_tip":"针对{tpl['name']}的一句话发布建议"}}"""

        raw = await _chat(prompt, system, json_mode=True, scene="content")
        data = _safe_parse_json(raw)
        if not data or "body" not in data:
            data = {"title": topic, "body": raw, "publish_tip": ""}
        # 合规扫描
        scan = compliance_scan((data.get("title", "") + " " + data.get("body", "")))
        versions.append({
            "platform": pf,
            "platform_name": tpl["name"],
            "why": tpl["why"],
            "title": data.get("title", topic),
            "body": data.get("body", ""),
            "publish_tip": data.get("publish_tip", ""),
            "compliance": scan,
        })

    return {"topic": topic, "brand": brand, "versions": versions}


# ============================================================
# AI 品牌认知报告（AI Brand Perception Report）
# 把监测拿到的各平台AI回答，聚合成"AI眼中的你"品牌画像
# 回答4个问题：AI如何描述你 / AI是否理解你的产品 / 跨平台是否一致 / 优势有没有被正确表达
# ============================================================
async def analyze_brand_perception(
    brand: str,
    product: str,
    brand_facts: str,
    platform_answers: list,   # [{"platform":"豆包","answer":"...","mentioned":True}, ...]
) -> dict:
    """
    基于各平台AI的真实回答，分析AI对品牌的认知。
    只用一次AI调用聚合，控制成本。
    """
    # 只取提到品牌的回答（这些才有"AI怎么描述你"的信息），最多12条控制token
    mentioned = [a for a in platform_answers if a.get("mentioned")][:12]
    if not mentioned:
        return {
            "has_data": False,
            "msg": "AI 目前几乎没有提到你的品牌，还谈不上「认知」——请先通过内容优化，让 AI 认识你，再来看认知报告。",
        }

    # 把各平台回答拼给AI分析
    answers_text = ""
    for a in mentioned:
        ans = (a.get("answer") or "")[:400]  # 每条截断控制长度
        answers_text += f"\n【{a.get('platform','AI')}】的回答片段：{ans}\n"

    system = (
        "你是品牌认知分析专家。基于多个AI平台对某品牌的真实回答，"
        "客观分析AI对这个品牌的认知状况。只基于给定的回答内容分析，不编造。"
        "如果某方面信息不足，如实说明。"
    )
    prompt = f"""分析各大AI平台对品牌「{brand}」的认知情况。

品牌真实信息（作为对照基准，判断AI说得对不对）：
产品/服务：{product or '（未提供）'}
品牌事实：{brand_facts or '（信息有限）'}

以下是各AI平台提到该品牌时的真实回答片段：
{answers_text}

请分析并只返回JSON（不要markdown，不要多余文字）：
{{
  "brand_description": "综合各AI，用2-3句话概括『AI是如何描述这个品牌的』（基于回答原文，别编）",
  "product_understanding": {{"score": 0-100的整数, "comment": "AI对产品/服务的理解是否准确，一句话"}},
  "consistency": {{"score": 0-100的整数, "comment": "不同AI对品牌的描述是否一致，一句话，如有矛盾请指出"}},
  "advantage_conveyed": {{"score": 0-100的整数, "comment": "品牌的竞争优势有没有被AI正确表达出来，一句话"}},
  "misperceptions": ["AI说错或过时的地方（数组，没有就空数组）"],
  "recommendations": ["提升AI品牌认知的2-3条具体建议（数组）"]
}}"""

    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "brand_description" not in data:
        return {"has_data": False, "msg": "分析生成失败，请稍后重试"}
    data["has_data"] = True
    data["analyzed_count"] = len(mentioned)
    return data


# ============================================================
# 增长引擎 · 4大高阶功能
# ============================================================

# 【功能1】独家高权重媒体直发矩阵（本地数据，零AI成本）
# 基于各AI引用生态调研，告诉商家：想被哪个AI引用，就往哪个媒体发

# 信源域名 → 发文平台映射（监测抓到的域名，反推该去哪发）
SOURCE_TO_PLATFORM = {
    "zhihu.com": {"platform": "知乎", "weight": "★★★★★", "how": "发专栏文章 + 回答该问题下的高热问题", "difficulty": "中"},
    "sohu.com": {"platform": "搜狐号", "weight": "★★★★★", "how": "开通搜狐号，每周1-2篇行业内容", "difficulty": "易"},
    "baijiahao.baidu.com": {"platform": "百家号", "weight": "★★★★☆", "how": "开通认证百家号，稳定更新", "difficulty": "易"},
    "baidu.com": {"platform": "百度系(百科/百家号)", "weight": "★★★★★", "how": "创建品牌百科词条 + 百家号内容", "difficulty": "难"},
    "baike.baidu.com": {"platform": "百度百科", "weight": "★★★★★", "how": "创建/完善品牌词条，AI知识类问题必引", "difficulty": "难"},
    "toutiao.com": {"platform": "今日头条号", "weight": "★★★★☆", "how": "开通头条号，配合抖音一起做", "difficulty": "易"},
    "csdn.net": {"platform": "CSDN", "weight": "★★★☆☆", "how": "技术/B2B行业发技术文", "difficulty": "中"},
    "jianshu.com": {"platform": "简书", "weight": "★★★☆☆", "how": "发深度长文", "difficulty": "易"},
    "xiaohongshu.com": {"platform": "小红书", "weight": "★★★★☆", "how": "发种草笔记 + 测评", "difficulty": "中"},
    "douyin.com": {"platform": "抖音", "weight": "★★★★☆", "how": "发'实测问AI'系列短视频", "difficulty": "中"},
    "weixin.qq.com": {"platform": "微信公众号", "weight": "★★★★☆", "how": "元宝重度依赖公众号，保持更新", "difficulty": "中"},
    "mp.weixin.qq.com": {"platform": "微信公众号", "weight": "★★★★☆", "how": "元宝重度依赖公众号，保持更新", "difficulty": "中"},
    "wikipedia.org": {"platform": "维基百科", "weight": "★★★★★", "how": "出海必做，海外AI高频引用", "difficulty": "难"},
    "reddit.com": {"platform": "Reddit", "weight": "★★★★★", "how": "出海：在相关subreddit真实互动", "difficulty": "中"},
    "quora.com": {"platform": "Quora", "weight": "★★★★☆", "how": "出海：回答行业高热问题", "difficulty": "中"},
    "medium.com": {"platform": "Medium", "weight": "★★★★☆", "how": "出海：发英文专业长文", "difficulty": "中"},
    "36kr.com": {"platform": "36氪", "weight": "★★★★☆", "how": "投稿行业观察/融资动态", "difficulty": "难"},
    "zhihu.com/zhuanlan": {"platform": "知乎专栏", "weight": "★★★★★", "how": "系统性发专业内容", "difficulty": "中"},
}

# 各行业「AI高频引用」的重点媒体源（帮不同行业商家知道该主攻哪些平台）
INDUSTRY_MEDIA = {
    "装修": ["知乎", "小红书", "搜狐号", "百家号", "抖音"],
    "口腔": ["知乎", "百度百科", "搜狐号", "小红书", "百家号"],
    "医美": ["小红书", "知乎", "百度百科", "搜狐号", "抖音"],
    "法律": ["知乎", "百家号", "搜狐号", "今日头条号", "百度百科"],
    "教育": ["知乎", "小红书", "百家号", "搜狐号", "公众号"],
    "餐饮": ["小红书", "抖音", "搜狐号", "今日头条号", "大众点评"],
    "美妆": ["小红书", "知乎", "抖音", "搜狐号", "百家号"],
    "外贸": ["Reddit", "Quora", "Medium", "维基百科", "LinkedIn"],
    "B2B": ["知乎", "36氪", "CSDN", "搜狐号", "百家号"],
    "电商": ["小红书", "知乎", "抖音", "搜狐号", "今日头条号"],
    "出海": ["Reddit", "Quora", "Medium", "维基百科", "YouTube"],
    "金融": ["知乎", "百家号", "搜狐号", "36氪", "百度百科"],
    "本地生活": ["小红书", "抖音", "大众点评", "搜狐号", "百家号"],
}

def get_media_matrix(industry: str = "") -> dict:
    """高权重媒体直发矩阵：各AI优先引用的媒体源清单。可按行业给重点推荐。"""
    matrix = [
        {"platform": "搜狐号", "weight": "★★★★★", "ai": "全平台通吃（豆包/DeepSeek/Kimi/元宝都引）",
         "why": "几乎所有主流AI都高频引用搜狐，性价比之王", "action": "必开，每周1-2篇", "difficulty": "易"},
        {"platform": "知乎", "weight": "★★★★★", "ai": "问答类AI引用核心",
         "why": "「XX哪个好」类问题AI大量引用知乎回答", "action": "发专栏+答高热问题", "difficulty": "中"},
        {"platform": "百家号", "weight": "★★★★☆", "ai": "百度文心 / 百度AI搜索首选",
         "why": "文心引用生态的核心，百度系流量入口", "action": "开通认证号，稳定更新", "difficulty": "易"},
        {"platform": "今日头条号", "weight": "★★★★☆", "ai": "豆包（3.45亿月活）优先引用",
         "why": "豆包背靠字节生态，优先抓头条+抖音", "action": "配合抖音一起做", "difficulty": "易"},
        {"platform": "公众号", "weight": "★★★★☆", "ai": "腾讯元宝重度依赖",
         "why": "元宝几乎只认公众号内容", "action": "你已有，保持更新", "difficulty": "中"},
        {"platform": "百度百科", "weight": "★★★★★", "ai": "所有AI知识类问题第一引用源",
         "why": "AI回答定义/背景类问题必引百科", "action": "创建品牌词条+完善行业词条", "difficulty": "难"},
        {"platform": "小红书", "weight": "★★★★☆", "ai": "消费类AI + 豆包引用",
         "why": "种草/测评类问题AI高频引用小红书", "action": "发真实测评+种草笔记", "difficulty": "中"},
        {"platform": "CSDN", "weight": "★★★☆☆", "ai": "DeepSeek技术类问题偏好",
         "why": "技术向GEO内容的高权重源", "action": "适合技术/B2B行业", "difficulty": "中"},
        {"platform": "抖音", "weight": "★★★★☆", "ai": "豆包生态，视频内容",
         "why": "视频竞争者远少于图文，蓝海", "action": "发'实测问AI'系列短视频", "difficulty": "中"},
    ]
    # 按行业给重点推荐
    industry_focus = None
    for key, plats in INDUSTRY_MEDIA.items():
        if key in (industry or ""):
            industry_focus = {"industry": key, "priority_platforms": plats,
                              "tip": f"你是「{key}」行业，AI 回答这类问题时最常引用：{'、'.join(plats)}。优先主攻这几个。"}
            break
    return {"matrix": matrix, "industry_focus": industry_focus,
            "note": "策略：想被某个AI推荐，就重点铺它引用的媒体。搜狐号+知乎+百度百科是三大必做。"}


def sources_to_action(cited_sources: list, industry: str = "") -> dict:
    """★核心闭环：把监测抓到的信源域名，反推成'该去哪发文'的行动建议。
    这是GEO最有价值的一环——AI从哪抓答案，你就去哪发。"""
    import re as _re
    recommendations = []
    seen_platforms = set()
    for src in (cited_sources or []):
        s = src.lower().strip()
        # 匹配信源域名
        matched = None
        for domain, info in SOURCE_TO_PLATFORM.items():
            if domain in s:
                matched = info
                break
        if matched and matched["platform"] not in seen_platforms:
            seen_platforms.add(matched["platform"])
            recommendations.append({
                "source_domain": src,
                "platform": matched["platform"],
                "weight": matched["weight"],
                "how": matched["how"],
                "difficulty": matched["difficulty"],
                "reason": f"AI 回答时引用了 {src}，说明它信任这个来源——你在这里发内容，更容易被 AI 抓到并推荐。",
            })
    # 补充行业推荐媒体（即使没抓到信源也给方向）
    industry_media = None
    for key, plats in INDUSTRY_MEDIA.items():
        if key in (industry or ""):
            industry_media = {"industry": key, "platforms": plats}
            break
    return {
        "from_sources": recommendations,
        "industry_media": industry_media,
        "note": "上半部分是从 AI 真实引用的信源反推的（最精准）；下半部分是你所在行业 AI 普遍爱引用的媒体（作为补充方向）。",
    }


# 【功能①】差异化 RAG 策略：不同 AI 引擎的引用偏好不同，针对性优化
# 每个 AI 抓取答案的"信息源生态"不一样，同一篇内容发对地方才有效
AI_ENGINE_PROFILE = {
    "chatgpt": {
        "name": "ChatGPT", "region": "海外",
        "prefers": ["维基百科", "Reddit", "官方网站", "权威媒体", "英文长文"],
        "retrieval": "实时联网检索 + 训练记忆，重视权威性和多来源共识",
        "strategy": "建品牌英文维基/官网 About 页，在 Reddit 相关话题真实互动，争取权威英文媒体报道",
        "content_tip": "英文、结构化、有数据和事实支撑，避免营销腔",
    },
    "gemini": {
        "name": "Gemini", "region": "海外",
        "prefers": ["Google 搜索结果", "YouTube", "官网", "Google 商家", "结构化数据"],
        "retrieval": "深度绑定 Google 生态，重视 SEO 和结构化数据（Schema）",
        "strategy": "做好官网 SEO + Schema 标记，建 Google 商家资料，配 YouTube 视频内容",
        "content_tip": "加 Schema 结构化标记，官网信息完整，多媒体形式",
    },
    "claude": {
        "name": "Claude", "region": "海外",
        "prefers": ["权威文档", "官方来源", "深度分析文章", "专业内容"],
        "retrieval": "重视内容质量和可信度，偏好深度、专业、有据可查的信息",
        "strategy": "产出深度专业内容（白皮书/行业分析/技术文档），强调专业性和可信度",
        "content_tip": "深度、专业、逻辑清晰、有引用来源，忌浮夸",
    },
    "deepseek": {
        "name": "DeepSeek", "region": "国内",
        "prefers": ["知乎", "CSDN", "技术社区", "搜狐号", "专业问答"],
        "retrieval": "偏技术和专业内容，知乎/CSDN 引用权重高",
        "strategy": "在知乎系统回答专业问题，技术类内容发 CSDN，搜狐号补充",
        "content_tip": "专业、有干货、逻辑严谨，适合深度问答形式",
    },
    "doubao": {
        "name": "豆包", "region": "国内",
        "prefers": ["今日头条", "抖音", "搜狐号", "字节生态内容"],
        "retrieval": "背靠字节生态，优先抓取头条+抖音+相关内容",
        "strategy": "开通头条号稳定更新，配抖音短视频（实测/测评类），搜狐号补充",
        "content_tip": "口语化、场景化、适合图文+短视频，贴近生活",
    },
    "qwen": {
        "name": "通义千问", "region": "国内",
        "prefers": ["知乎", "搜狐号", "百家号", "阿里生态"],
        "retrieval": "阿里生态，综合性引用，知乎和资讯类权重较高",
        "strategy": "知乎+搜狐号+百家号多平台铺开，保持内容一致性",
        "content_tip": "专业与通俗兼顾，多平台一致输出",
    },
    "wenxin": {
        "name": "文心一言", "region": "国内",
        "prefers": ["百家号", "百度百科", "百度知道", "百度系内容"],
        "retrieval": "百度生态核心，百家号/百科引用权重极高",
        "strategy": "必做百度百科词条 + 百家号认证号，覆盖百度知道问答",
        "content_tip": "权威、规范，符合百度收录偏好，百科要客观中立",
    },
    "kimi": {
        "name": "Kimi", "region": "国内",
        "prefers": ["知乎", "公众号", "长文内容", "专业资料"],
        "retrieval": "擅长长文本，偏好深度、结构化的专业内容",
        "strategy": "产出深度长文发知乎专栏和公众号，系统性覆盖专业话题",
        "content_tip": "长文、深度、结构化，适合系统性论述",
    },
    "yuanbao": {
        "name": "腾讯元宝", "region": "国内",
        "prefers": ["微信公众号", "微信生态", "视频号"],
        "retrieval": "腾讯生态，重度依赖公众号内容",
        "strategy": "公众号稳定更新是核心，配合视频号，做好微信生态内容",
        "content_tip": "适合公众号的深度图文，配合微信传播",
    },
}

# 【功能④】第三方背书矩阵（合规版）：帮品牌建立真实的外部引用网络
# 重要：只生成"内容框架/模板"给真实的人去发，不凭空捏造假评测批量刷网
# 造假素人/KOL评测在中国违法（反不正当竞争法/广告法），且长期害品牌
def generate_endorsement_kit(brand: str, industry: str, product: str,
                             real_strengths: str = "") -> dict:
    """第三方背书素材包（合规）：基于真实卖点，生成不同视角的内容框架，
    供真实客户/合作方参考发布，建立多平台真实引用网络。"""
    return {
        "principle": "⚠️ 合规红线：以下是内容框架，必须基于真实产品体验、由真实用户/合作方发布。凭空捏造虚假评测在中国违法（《反不正当竞争法》），且一旦被曝光会摧毁品牌信任。",
        "perspectives": [
            {
                "role": "真实老客户口碑",
                "how": "邀请真实满意的客户在知乎/小红书分享使用体验（可提供内容框架但不代写虚假内容）",
                "framework": f"分享我为什么选{industry}产品时选了它 → 实际使用中解决了什么问题 → 客观说优缺点 → 适合什么人",
                "platform": "小红书、知乎、大众点评",
                "compliant": "必须是真实客户真实体验，品牌可提供框架但内容需真实",
            },
            {
                "role": "行业专家/KOL 合作",
                "how": "与真实的行业 KOL 建立合作，提供产品让其真实体验后客观评价",
                "framework": "专业视角分析这类产品该怎么选 → 实测体验 → 专业结论",
                "platform": "知乎专栏、B站、行业公众号",
                "compliant": "需真实合作+真实体验，按《广告法》标注'合作'或'广告'",
            },
            {
                "role": "深度测评内容",
                "how": "自己或合作方产出客观的横向测评（含竞品对比，实事求是）",
                "framework": "同类产品横向对比 → 各自优劣 → 不同需求推荐不同 → 客观呈现你的优势",
                "platform": "知乎、什么值得买、B站",
                "compliant": "对比要客观真实，不可诋毁竞品或虚构数据",
            },
            {
                "role": "官方专业内容",
                "how": "以品牌官方身份产出专业干货，建立行业权威形象",
                "framework": f"{industry}行业知识科普 → 如何挑选 → 你的专业解决方案",
                "platform": "百家号、搜狐号、官网博客",
                "compliant": "官方内容，专业客观，不用绝对化用语",
            },
        ],
        "collection_tips": [
            "把真实好评（聊天截图、评价，经客户同意后）整理成可分享素材",
            "对满意客户主动邀请评价，正面口碑要主动放大",
            "多平台布局，让 AI 在不同域名都能读到你的真实评价 = 建立'多来源共识'",
        ],
        "note": "GEO 的第三方背书 = 真实口碑的系统化放大，不是造假。AI 越来越能识别虚假内容，真实、多来源、一致的正面评价才是可持续的护城河。",
    }


# 【功能③】实体权重（Entity Authority）：AI 把品牌当"实体"认知的权威度
# 诚实：没有公开精确数据源，给相对评估+建设清单，不编造"85分"这种假数字
def assess_entity_authority(brand: str, industry: str, cited_sources: list = None,
                            mention_rate: float = 0, has_wiki: bool = False) -> dict:
    """实体权威度评估：AI 是否把你当成一个'可信实体'。
    诚实定位：给相对评估+可执行的建设清单，不给伪精确分数。"""
    cited_sources = cited_sources or []
    # 实体权威度的几个真实维度（可自查）
    signals = []
    # 1. 百科类权威源
    has_baike = any("baike" in s or "wikipedia" in s or "百科" in s for s in cited_sources)
    signals.append({
        "dimension": "权威百科收录",
        "status": "已有" if (has_baike or has_wiki) else "缺失",
        "level": "high" if (has_baike or has_wiki) else "low",
        "why": "百度百科/维基是 AI 识别'实体身份'的第一来源。有词条 = AI 认可你是个'正经存在的实体'",
        "action": "创建品牌百度百科词条（国内）/ 维基百科（出海）——这是实体权威度的地基",
    })
    # 2. 多域名共识（同一品牌在多少不同来源被提到）
    unique_domains = len(set(cited_sources))
    signals.append({
        "dimension": "多来源共识",
        "status": f"{unique_domains} 个来源提及" if unique_domains else "尚无来源提及",
        "level": "high" if unique_domains >= 3 else ("mid" if unique_domains >= 1 else "low"),
        "why": "AI 只信'多个独立来源都说好'的品牌。单一来源说好 = 广告；多来源一致 = 共识",
        "action": "在知乎、搜狐、百家号、行业媒体等多个不同域名建立一致的品牌提及",
    })
    # 3. AI 提及率（间接反映实体认知度）
    signals.append({
        "dimension": "AI 实际认知度",
        "status": f"提及率 {mention_rate}%",
        "level": "high" if mention_rate >= 40 else ("mid" if mention_rate >= 15 else "low"),
        "why": "提及率反映 AI 是否'记得'并'愿意推荐'你这个实体",
        "action": "持续在高权重源产出一致内容，把实体认知从'听过'提升到'推荐'",
    })
    # 综合相对评级（诚实：给等级不给假分数）
    high_count = sum(1 for s in signals if s["level"] == "high")
    if high_count >= 2:
        overall = ("强", "#a8c48c", "AI 已把你当成可信实体，继续巩固")
    elif high_count == 1:
        overall = ("中", "#c99a52", "实体认知初步建立，还需多来源共识加固")
    else:
        overall = ("弱", "#c96a5f", "AI 还没把你当成'可信实体'——优先建百科+多来源提及")
    return {
        "overall_level": overall[0],
        "overall_color": overall[1],
        "overall_desc": overall[2],
        "signals": signals,
        "note": "实体权威度没有精确分数（那需要付费数据源且各家算法不同），这里给的是基于真实信号的相对评估。核心逻辑：AI 只推'在多个可信来源被反复正面提及'的品牌。",
    }


# 【功能②】多轮对话意图：模拟真实用户的追问链，测品牌在对话深入后还在不在
# 真实用户不是问一次就走：哪家好→那A和B比→预算X推荐哪个
async def generate_multi_turn_questions(brand: str, industry: str, product: str,
                                        competitors: str = "", target_lang: str = "zh") -> dict:
    """生成多轮对话意图链：模拟用户从'泛问'到'具体决策'的追问过程。"""
    comp_hint = f"已知竞品：{competitors}。" if competitors else ""
    prompt = f"""你是{industry}行业的真实消费者。针对"{product}"这类产品，
模拟你从初次了解到最终决策，会连续问 AI 的一串问题（多轮对话）。
{comp_hint}
要求生成 3 组对话链，每组 3 轮，体现真实决策过程：
- 第1轮：泛问（如"XX哪个牌子好"）
- 第2轮：对比追问（如"那A和B哪个更适合XX场景"）
- 第3轮：决策追问（如"预算XX的话你推荐哪个"）
不要出现品牌名"{brand}"。只返回JSON：
{{"chains":[{{"intent":"决策场景描述","turns":["第1轮问题","第2轮追问","第3轮决策问"]}}]}}"""
    try:
        raw = await _chat(prompt, "你擅长模拟真实用户的完整决策对话过程。", json_mode=True, scene="questions")
        data = _safe_parse_json(raw)
        chains = data.get("chains", [])[:3]
    except Exception:
        chains = []
    return {
        "chains": chains,
        "note": "多轮对话监测：真实用户会连续追问。测品牌在对话逐步深入、AI 给出具体推荐时，还在不在名单里——这比单次提问更接近真实成交场景。",
    }


def get_rag_strategy(target_ais: list = None, industry: str = "") -> dict:
    """差异化 RAG 策略：针对不同 AI 引擎的引用偏好，给差异化的内容+发布建议。
    同一篇内容，发对地方才能被对应 AI 抓到。"""
    if not target_ais:
        target_ais = ["deepseek", "doubao", "wenxin", "chatgpt", "gemini"]
    strategies = []
    for ai in target_ais:
        prof = AI_ENGINE_PROFILE.get(ai)
        if not prof:
            continue
        strategies.append({
            "ai": ai,
            "name": prof["name"],
            "region": prof["region"],
            "prefers": prof["prefers"],
            "retrieval": prof["retrieval"],
            "strategy": prof["strategy"],
            "content_tip": prof["content_tip"],
        })
    # 找出交集平台（多个AI都爱的，优先做）
    all_prefers = {}
    for s in strategies:
        for p in s["prefers"]:
            all_prefers[p] = all_prefers.get(p, 0) + 1
    priority = sorted(all_prefers.items(), key=lambda x: -x[1])
    high_value = [p for p, c in priority if c >= 2]
    return {
        "strategies": strategies,
        "high_value_platforms": high_value,
        "key_insight": f"不同 AI 从不同地方抓答案。{('多个 AI 都爱引用：' + '、'.join(high_value[:5]) + '，优先主攻这些性价比最高。') if high_value else '按目标 AI 分别布局。'}",
        "note": "GEO 不是'发得多'，而是'发对地方'。想被哪个 AI 推荐，就重点铺它引用的信息源。",
    }


# 【功能2】Schema结构化数据一键注入（强化版，本地生成，零AI成本）
def generate_schema_inject(brand: str, product: str, industry: str,
                           address: str = "", phone: str = "", url: str = "",
                           faqs: list = None) -> dict:
    """一键生成可直接注入官网的完整Schema代码（LocalBusiness + FAQPage + Organization）。"""
    import json as _j
    faqs = faqs or []
    org = {
        "@context": "https://schema.org", "@type": "Organization",
        "name": brand, "description": f"{brand}是{industry}领域的专业品牌，主营{product or industry}。",
    }
    if url: org["url"] = url
    local = {
        "@context": "https://schema.org", "@type": "LocalBusiness",
        "name": brand, "description": f"{brand} - {industry}",
    }
    if address: local["address"] = {"@type": "PostalAddress", "streetAddress": address}
    if phone: local["telephone"] = phone
    if url: local["url"] = url
    blocks = [org, local]
    if faqs:
        faq_schema = {
            "@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": f.get("q", ""),
                            "acceptedAnswer": {"@type": "Answer", "text": f.get("a", "")}} for f in faqs[:8]]
        }
        blocks.append(faq_schema)
    code = "\n".join(f'<script type="application/ld+json">\n{_j.dumps(b, ensure_ascii=False, indent=2)}\n</script>' for b in blocks)
    return {"schema_code": code, "block_count": len(blocks),
            "guide": "把以上代码整段复制，粘贴到你官网每个页面的 </head> 之前。AI 和搜索引擎会据此精准理解你的品牌。"}


# 【功能3】逆向RAG专家语料生成引擎
async def generate_rag_corpus(brand: str, product: str, brand_facts: str, topic: str) -> dict:
    """逆向RAG：反推AI检索逻辑，生成'AI最爱抓取'的结构化专家语料。
    RAG(检索增强生成)是AI回答的底层机制——AI先检索片段再组织答案。
    本引擎生成'易被切片检索、易被AI采信'的高密度语料。"""
    system = (
        BRAND_TONE + "\n你是精通 RAG（检索增强生成）机制的语料工程专家。"
        "你深知 AI 检索时偏好：①信息密度高、②事实性强带数据、③结构化易切片、"
        "④问答配对清晰、⑤权威客观。你生成的语料要让 AI 在检索时优先命中、优先采信。"
    )
    prompt = f"""为品牌「{brand}」围绕主题「{topic}」，生成一份'逆向RAG专家语料'。

品牌真实信息：{brand_facts or product or '信息有限'}

【逆向RAG语料要求】——目标是让AI检索时优先抓取、优先采信：
1. 拆成 5-8 个独立的"知识切片"，每片是一个自包含的事实陈述（AI按片检索）
2. 每片格式：一句话核心结论 + 支撑细节/数据（有则标来源）
3. 语言客观、事实性强，像专家/百科，不像广告
4. 覆盖：定义、优势、对比、场景、常见疑问
5. 每片可独立成立，不依赖上下文（这是RAG检索的关键）

只返回JSON：
{{"corpus":[{{"slice_title":"切片主题","content":"自包含的事实陈述(80-150字)"}}],"usage":"这份语料怎么用的一句话建议"}}"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "corpus" not in data:
        return {"corpus": [], "usage": "生成失败，请重试"}
    return data


# 【功能4】高转化意图拦截与标题工程
async def generate_intent_titles(brand: str, product: str, industry: str, brand_facts: str) -> dict:
    """高转化意图拦截：挖掘高购买意图的搜索问题，并做标题工程（标题=AI命中钩子）。"""
    system = (
        BRAND_TONE + "\n你是精通用户搜索意图和标题工程的GEO专家。"
        "你能识别'高购买意图'的搜索问题（马上要下单的人会问的），"
        "并写出既能被AI命中、又能吸引点击的标题。"
    )
    prompt = f"""为品牌「{brand}」（{industry}，主营{product or industry}）做'高转化意图拦截+标题工程'。

品牌信息：{brand_facts or '信息有限'}

分两部分：
第一部分【高意图问题拦截】：列出6个'高购买意图'的搜索问题——就是那些'马上准备花钱的客户'会问AI的问题（如"XX多少钱""XX哪家靠谱""XX和YY哪个好"），这些问题的流量转化率最高，要优先拦截。每个标注意图强度（高/极高）。

第二部分【标题工程】：针对这些高意图问题，写出6个'AI命中+高点击'的标题。标题工程原则：①包含问题原话（AI命中）②有具体数字/利益点（吸引点击）③不标题党。

只返回JSON：
{{"intent_questions":[{{"question":"问题","intent":"高/极高","why":"为什么高意图"}}],"engineered_titles":[{{"title":"标题","target":"针对哪个问题","technique":"用了什么技巧"}}]}}"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "intent_questions" not in data:
        return {"intent_questions": [], "engineered_titles": [], "msg": "生成失败，请重试"}
    return data


# ============================================================
# 关键词联想词（免费：优先真实下拉词接口，失败用AI兜底）
# ============================================================
async def get_keyword_suggestions(keyword: str, industry: str = "") -> dict:
    """获取关键词的联想词/长尾词。
    诚实定位：这是"相关热门搜索词"，不是精确搜索量。
    数据源：优先百度/搜狗免费下拉词接口，失败时用AI生成相关长尾词兜底。"""
    kw = (keyword or "").strip()
    if not kw:
        return {"keyword": kw, "suggestions": [], "source": "none"}

    suggestions = []
    source = "ai"
    # 尝试1：百度免费下拉词接口（真实搜索联想）
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://www.baidu.com/sugrec",
                params={"prod": "pc", "wd": kw},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            if r.status_code == 200:
                data = r.json()
                for g in data.get("g", []):
                    q = g.get("q", "").strip()
                    if q and q not in suggestions:
                        suggestions.append(q)
                if suggestions:
                    source = "baidu"
    except Exception:
        pass

    # 尝试2：如果下拉词没拿到，用AI生成相关长尾词兜底
    if not suggestions:
        try:
            prompt = f"""围绕关键词「{kw}」{f'（行业：{industry}）' if industry else ''}，
列出 12 个用户真实会搜索的相关长尾词/联想词。
要求：口语化、贴近真实搜索习惯、覆盖不同意图（了解/对比/价格/口碑/怎么选）。
只返回 JSON：{{"suggestions":["词1","词2",...]}}"""
            raw = await _chat(prompt, "你是搜索行为分析专家，熟悉用户真实搜索习惯。", json_mode=True, scene="other")
            data = _safe_parse_json(raw)
            suggestions = [s.strip() for s in data.get("suggestions", []) if s.strip()][:12]
            source = "ai"
        except Exception:
            suggestions = []

    return {
        "keyword": kw,
        "suggestions": suggestions[:15],
        "count": len(suggestions[:15]),
        "source": source,
        "note": "这是与该词相关的热门搜索词，帮你发现更多值得布局的长尾词。注意：这是相关词参考，非精确搜索量（精确数据需付费数据源）。",
    }


# ============================================================
# 关键词收录情况分析（老实方案：相对热度 + 收录追踪）
# 不编造精确搜索量，给相对热度评级 + 基于真实监测数据的收录洞察
# ============================================================
async def analyze_keyword_index(brand: str, industry: str, keywords: list) -> dict:
    """分析一组关键词的相对热度和 GEO 价值。
    诚实：不给精确搜索量（那需要付费数据源），给相对热度评级 + 收录建议。"""
    if not keywords:
        return {"keywords": []}
    system = (
        "你是 GEO 关键词分析专家。基于关键词特征判断其相对热度和商业价值。"
        "诚实：你没有精确搜索量数据，只做相对评估。"
    )
    kw_text = "、".join(keywords[:15])
    prompt = f"""为「{industry}」行业的品牌「{brand}」分析这些关键词在 AI 搜索时代的价值：
{kw_text}

对每个词评估（基于词的特征，不编造精确数字）：
- 相对热度：高/中/低（这个词有多少人会问 AI）
- 购买意图：强/中/弱（问这个词的人离下单有多近）
- 收录难度：易/中/难（让 AI 在这个词上推荐你的难度）
- GEO优先级：基于以上综合，值不值得优先做

只返回JSON：
{{"keywords":[{{"word":"关键词","heat":"高/中/低","intent":"强/中/弱","difficulty":"易/中/难","priority":"高/中/低","reason":"一句话建议"}}]}}"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "keywords" not in data:
        return {"keywords": [], "note": "分析失败，请重试"}
    data["note"] = "热度为相对评估（非精确搜索量）。精确搜索量需接入付费数据源。"
    return data


# ============================================================
# 竞品弱点分析 + 商家作战方案（把信源数据变成"照着做"的行动清单）
# ============================================================
async def generate_battle_plan(brand: str, industry: str, product: str,
                                competitors: list, citation_targets: list,
                                mention_rate: float) -> dict:
    """基于监测数据，为商家生成一份'看得懂、照着做'的作战方案。
    输入：品牌信息 + 竞品 + 信源溯源数据 + 当前推荐率
    输出：竞品弱点 + 我方切入点 + 分平台行动清单 + 预期"""
    # 整理信源数据给AI
    src_summary = []
    for t in (citation_targets or [])[:10]:
        comps = t.get("competitors_here", [])
        src_summary.append(
            f"- {t.get('source','')}：被AI引用{t.get('cited_count',0)}次，"
            f"{'你已露出' if t.get('brand_present') else '你未露出'}"
            f"{('，带出竞品:'+('、'.join(comps))) if comps else ''}"
        )
    src_text = "\n".join(src_summary) if src_summary else "（暂无信源数据）"
    comp_text = "、".join(competitors) if competitors else "（未指定竞品）"

    system = (
        "你是顶尖的GEO作战参谋，服务对象是不懂技术的中小商家。"
        "你的任务：把冷冰冰的监测数据，翻译成商家一看就懂、照着就能做的作战方案。"
        "语言要接地气、具体、可执行，不要空话套话。每条建议都要让商家知道'具体去哪、具体做什么'。"
    )
    prompt = f"""为品牌「{brand}」（行业：{industry}，产品：{product}）制定AI推荐作战方案。

【当前战况】
- AI推荐率：{mention_rate}%（{'偏低，急需提升' if mention_rate<30 else '中等，有提升空间' if mention_rate<60 else '不错，需巩固'}）
- 主要竞品：{comp_text}

【信源情报】AI在这个行业信任的信息源，以及竞品的分布：
{src_text}

请输出一份作战方案，JSON格式：
{{
  "competitor_weakness": [
    {{"competitor":"竞品名", "weakness":"这个竞品的弱点/你的机会（比如：它只在知乎有内容，小红书是空白；或它内容老旧；或它没覆盖某类问题）", "how_to_beat":"你具体怎么切入抢它的位置"}}
  ],
  "my_gaps": [
    {{"gap":"你缺什么（具体，比如：搜狐号完全没有你的内容）", "why_matters":"为什么这个重要（AI高频引用这里）", "fix":"具体怎么补"}}
  ],
  "action_checklist": [
    {{"step":1, "platform":"平台名（如知乎/搜狐号）", "action":"具体动作（发什么内容、什么主题）", "difficulty":"易/中/难", "expected":"预期效果"}}
  ],
  "priority_summary": "一句话总结：现在最该做的第一件事是什么"
}}

要求：
1. competitor_weakness 分析2-3个竞品的弱点和你的机会
2. my_gaps 找出2-4个你的关键缺口
3. action_checklist 给出4-6个按优先级排序的具体动作，商家照着做就行
4. 所有建议必须具体、可执行，不要"提升品牌影响力"这种空话
5. 只基于提供的真实数据分析，不编造"""

    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data:
        return {"error": "作战方案生成失败，请重试"}
    # 需求③：提炼"今天就做这一件事"（从行动清单里挑第一个，降低执行门槛）
    checklist = data.get("action_checklist", [])
    if checklist:
        first = checklist[0]
        data["today_focus"] = {
            "title": f"今天就做这一件：{first.get('action', '')}",
            "platform": first.get("platform", ""),
            "why": first.get("expected", "这是当前性价比最高的一步"),
            "tip": "别贪多，今天先把这一件做完。做完了，明天再做下一个。",
        }
    data["note"] = "本方案基于当前监测数据生成。执行后重新监测可看效果变化。"
    return data


# ============================================================
# 本地商家 GEO 专项（需求⑧：地图POI/NAP一致性/本地问答）
# ============================================================
async def generate_local_geo_kit(brand: str, industry: str, region: str,
                                   address: str = "", phone: str = "",
                                   hours: str = "") -> dict:
    """为本地服务商生成本地GEO落地清单：
    地图POI认领、NAP一致性检查、本地问答页、城市+行业落地页建议。"""
    system = (
        "你是本地生活 GEO 专家。本地服务商（餐饮、口腔、装修、美容等）"
        "要被 AI 和地图推荐，靠的是 NAP 信息一致、地图 POI 完善、本地问答内容。"
        "你要给商家一份具体、可照做的本地 GEO 清单。"
    )
    nap_status = "已提供" if (address and phone) else "不完整（缺地址或电话）"
    prompt = f"""为「{region}」的本地「{industry}」商家「{brand}」生成本地 GEO 优化清单。

【商家信息】
- 地址：{address or '未填'}
- 电话：{phone or '未填'}
- 营业时间：{hours or '未填'}
- NAP完整度：{nap_status}

请输出本地 GEO 落地清单，JSON格式：
{{
  "nap_check": {{
    "status": "一致性检查结论（NAP=名称Name/地址Address/电话Phone，多平台必须完全一致）",
    "todo": ["要检查/统一的具体项，如'确保高德、百度地图、大众点评上的电话完全一致'"]
  }},
  "map_poi": [
    {{"platform":"高德地图/百度地图/微信/抖音POI", "action":"具体怎么认领和完善", "why":"为什么重要"}}
  ],
  "local_qa": [
    {{"question":"本地客户会问AI的问题（带地域，如'{region}哪家{industry}好'）", "content_tip":"这条内容怎么写、发哪"}}
  ],
  "landing_pages": [
    {{"page":"城市+行业落地页建议（如'{region}{industry}服务'）", "keywords":"该页该覆盖的词", "why":"抢本地搜索"}}
  ],
  "priority": "一句话：本地商家最该先做的第一件事"
}}

要求：具体可执行，紧扣「{region}」和「{industry}」，不要空话。"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data:
        return {"error": "本地GEO清单生成失败，请重试"}
    data["note"] = "本地商家的 GEO 核心是：信息一致 + 地图完善 + 本地问答。做好这三点，AI 和地图才会把你推给附近客户。"
    return data


# ============================================================
# 区域性关键词生成（本地商户命脉：客户问"附近哪家好"）
# ============================================================
async def generate_regional_keywords(brand: str, industry: str, product: str,
                                       region: str) -> dict:
    """为本地商户生成带地域的高意图关键词。
    这是本地生意的命脉——客户问AI'XX区哪家好''附近靠谱的XX'。"""
    system = (
        "你是本地生活 GEO 专家，深知本地客户是怎么问 AI 找店的。"
        "你要生成客户真实会问 AI 的、带地域的高意图问题，帮本地商户被 AI 推荐。"
        "语言要接地气，就是普通消费者会打出来的话。"
    )
    region_hint = region or "（商户未填地区，请生成通用地域模板，用[地区]占位）"
    prompt = f"""为「{region_hint}」的「{industry}」商户「{brand}」（产品/服务：{product}）生成区域性 GEO 关键词。

本地客户找店时，会这样问 AI（举例）：
- "[地区]哪家[行业]好/靠谱"
- "[地区]附近的[行业]推荐"
- "[地区][具体需求]去哪做"
- "[地区][行业]多少钱"

请生成4类区域词，每类3-5个，要具体、贴近真实提问：
1. 直接找店类（XX区哪家好、附近推荐）
2. 价格咨询类（XX多少钱、性价比）
3. 具体需求类（针对该行业的具体服务/产品需求+地域）
4. 决策对比类（怎么选、哪家靠谱、避坑）

只返回JSON：
{{
  "region": "{region or '通用'}",
  "keyword_groups": [
    {{"type":"直接找店","keywords":["词1","词2","词3"],"why":"这类词的客户离下单最近，是必抢的"}},
    {{"type":"价格咨询","keywords":[...],"why":"..."}},
    {{"type":"具体需求","keywords":[...],"why":"..."}},
    {{"type":"决策对比","keywords":[...],"why":"..."}}
  ],
  "top_advice": "一句话：本地商户最该先抢哪类词，为什么"
}}

要求：词要真实（就是消费者会打的字），紧扣「{industry}」和「{region or '本地'}」，不要空泛。"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "keyword_groups" not in data:
        return {"error": "区域词生成失败，请重试"}
    data["note"] = ("区域词是本地生意的命脉。建议：①先在地图POI认领并填全信息 "
                    "②围绕这些词做内容发到本地平台 ③引导老客户留真实评价。")
    return data


# ============================================================
# GEO 资料清单（告诉商户：做GEO要准备哪些资料、哪些AI爱收录）
# ============================================================
async def generate_material_checklist(brand: str, industry: str, product: str,
                                        is_local: bool = False) -> dict:
    """生成'做GEO要准备哪些资料'的清单。
    解决商户'不知道该准备什么、发什么会被AI收录'的痛点。"""
    system = (
        "你是 GEO 内容策略专家。你要告诉商户：想让 AI 收录和推荐你，"
        "需要准备哪些资料、内容，哪些是 AI 最爱引用的。"
        "要具体、可操作，让商户照着清单准备就行。"
    )
    local_hint = "这是本地商户，要包含地图POI、真实评价、门店信息等本地要素。" if is_local else ""
    prompt = f"""为「{industry}」的「{brand}」（产品/服务：{product}）生成一份 GEO 资料准备清单。{local_hint}

告诉商户：想让 AI 认识你、推荐你，需要准备哪些资料和内容。分为：
1. 基础资料（必备的品牌信息，AI 识别你的基础）
2. 内容资料（AI 爱收录引用的内容类型）
3. 信任背书（让 AI 觉得你可信的东西）
{'4. 本地要素（地图、评价、门店信息等）' if is_local else ''}

每项要说明：是什么、为什么AI爱收录、商户怎么准备。

只返回JSON：
{{
  "checklist": [
    {{
      "category": "基础资料",
      "items": [
        {{"name":"资料名","why":"为什么AI爱收录这个","how":"具体怎么准备","priority":"必备/推荐"}}
      ]
    }}
  ],
  "content_that_gets_cited": ["AI最爱收录的内容类型1（如：真实价格拆解）","类型2","类型3","类型4"],
  "top_advice": "一句话：商户最该先准备什么"
}}

要求：紧扣「{industry}」，具体可操作，不要空泛的'提升品牌形象'这种话。"""
    raw = await _chat(prompt, system, json_mode=True, scene="content")
    data = _safe_parse_json(raw)
    if not data or "checklist" not in data:
        return {"error": "资料清单生成失败，请重试"}
    data["note"] = "备齐这些资料，再用内容工厂生成内容、发到指定平台，AI 收录你的概率最高。"
    return data
