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
) -> list:
    """
    生成品牌专属问题集。
    mode=outbound: 出海模式，英文问题+中文翻译，监测 ChatGPT/Gemini 等
    mode=domestic: 国内模式，纯中文问题，监测 DeepSeek/通义千问/豆包/Kimi
    """
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
1. 用英文写，口语化，是海外用户真实会打出来的话
2. 与品牌的具体特征和场景高度相关
3. 不包含品牌名
4. 覆盖以下类目，每类均匀分布：
{chr(10).join('- ' + c for c in categories)}

每个问题返回：
- category：中文类目名（品类推荐/对比评测/使用场景等）
- question：英文问题（用于 AI 监测）
- question_cn：对应中文翻译（让中国商家看懂）

只返回 JSON：
{{"questions":[{{"category":"类目名","question":"英文问题","question_cn":"中文翻译"}}]}}
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
def get_media_matrix(industry: str = "") -> dict:
    """高权重媒体直发矩阵：各AI优先引用的媒体源清单。"""
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
        {"platform": "CSDN", "weight": "★★★☆☆", "ai": "DeepSeek技术类问题偏好",
         "why": "技术向GEO内容的高权重源", "action": "适合技术/B2B行业", "difficulty": "中"},
        {"platform": "抖音", "weight": "★★★★☆", "ai": "豆包生态，视频内容",
         "why": "视频竞争者远少于图文，蓝海", "action": "发'实测问AI'系列短视频", "difficulty": "中"},
    ]
    return {"matrix": matrix, "note": "策略：想被某个AI推荐，就重点铺它引用的媒体。搜狐号+知乎+百度百科是三大必做。"}


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
