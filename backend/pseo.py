"""
见微 · 程序化建站 (pSEO) 服务端动态渲染系统
================================================
用 FastAPI + Jinja2 实现全动态、对搜索引擎/AI爬虫极度友好的 SSR 行业页。
每个 /solutions/{slug} 都是一张独立、可被秒抓取的行业 GEO 方案页。

接入方式（在 main.py 里加一行）:
    from pseo import register_pseo
    register_pseo(app)

不影响任何现有路由，纯增量。
"""
import os
import re
from datetime import datetime

from fastapi import Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_DIR = os.path.join(_HERE, "..", "frontend")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# 站点根地址（用于生成 canonical / sitemap 的绝对链接）
SITE_BASE = os.getenv("SITE_BASE_URL", "https://jianwei.uno").rstrip("/")


# ============================================================
# 模拟数据库 (Mock DB) —— 国内核心行业 GEO 数据
# 未来可平滑迁移到真实数据库：把 INDUSTRY_DB 换成 DB 查询即可。
# ============================================================
INDUSTRY_DB = {
    "shenzhen-cross-border-saas": {
        "slug": "shenzhen-cross-border-saas",
        "city": "深圳",
        "industry": "跨境电商SaaS",
        "seo_title": "深圳跨境电商SaaS行业GEO优化方案 | 见微 - AI搜索流量破局专家",
        "seo_desc": "深圳跨境电商SaaS企业正在AI搜索中集体隐形？见微提供专业GEO（生成式引擎优化）方案，解决豆包、DeepSeek语料库截流问题，让AI主动推荐你的品牌，抢回被对手夺走的商机。",
        "pain_points": [
            "客户在豆包、DeepSeek问「跨境电商用什么SaaS好」，AI张口就报竞品，你的品牌在答案里彻底隐形——这不是排名靠后，是根本不存在。",
            "你的官网、公众号内容再多，也只是「自己夸自己」。AI 判定缺乏第三方语料佐证，宁可不推你，也不敢拿你的信息去误导用户。",
            "同行早一步做了语料布局，被 AI「越记越牢」。每天有大量高意向采购决策者问完 AI 直接找了对手，你的商机在无声中持续蒸发。",
        ],
        "strategy": "见微针对深圳跨境电商SaaS赛道，先用8大AI平台扫描出你的「隐形地图」——AI到底在哪些采购决策问题上没提你、把客户推给了谁。随后定向投喂结构化行业语料：在AI高频引用的信源（知乎专业问答、垂直B2B平台、行业评测）布局符合AI收录标准的测评与选型指南，把你的产品优势翻译成AI能理解、敢采信的表达。7天为一个作战周期，监测→补语料→复测，让AI从「不认识你」逐步到「优先推荐你」，把跨境采购决策场景的AI流量真正握在自己手里。",
    },
    "hangzhou-industrial-manufacturing": {
        "slug": "hangzhou-industrial-manufacturing",
        "city": "杭州",
        "industry": "工业制造",
        "seo_title": "杭州工业制造行业GEO优化方案 | 见微 - 让AI主动推荐你的工厂",
        "seo_desc": "杭州工业制造企业如何在AI搜索时代抢占先机？见微GEO方案帮制造企业破解「AI搜索隐形」难题，在采购商询价决策链路中占据AI推荐位，把询盘商机从对手手里夺回来。",
        "pain_points": [
            "采购商现在选供应商，第一步就是问AI「XX配件哪家厂靠谱」。你的工厂做了二十年，AI却一次没提过——线下口碑，在AI这里等于零。",
            "你的产品参数、案例全躺在自家官网和画册里，AI抓不到、也不采信。缺乏被广泛引用的结构化语料，AI判定你「信息不可靠」，直接跳过。",
            "外贸和内销的高价值询盘，正越来越多地经由AI初筛完成。AI没把你列进候选，询盘链路第一环就断了，商机在你看不见的地方持续流失。",
        ],
        "strategy": "见微针对杭州工业制造企业，聚焦「采购商AI询价决策链路」做专项优化。先监测出你在核心品类询价问题上的AI可见度与竞品抢占情况，锁定最易突破的精准词（如区域+品类+应用场景）。随后帮你把工厂的资质、产能、真实案例，重构成AI偏爱的结构化语料（选型指南、参数对比、应用问答），定向布局到AI信任的B2B信源与专业问答平台。以7天为周期持续投喂与复测，让你的工厂在采购商问AI时被优先提及、且信息准确，把断掉的询盘链路重新接上。",
    },
    "chengdu-medical-aesthetics": {
        "slug": "chengdu-medical-aesthetics",
        "city": "成都",
        "industry": "医疗美容",
        "seo_title": "成都医美行业GEO优化方案 | 见微 - AI搜索获客与口碑破局",
        "seo_desc": "成都医美机构在AI搜索中被竞品截流？见微GEO方案专为医美设计，解决AI推荐隐形与信息说错问题，让求美者问AI时优先看到你，把高意向到店客户从对手手里抢回来。",
        "pain_points": [
            "求美者做决定前必问AI「成都做XX哪家正规」。AI报的是那几家老对手，你的机构没出现——高意向、高客单的到店客户，就这么被推给了别人。",
            "医美是重信任决策，AI尤其看重第三方口碑与合规信源。你缺乏被AI采信的正向语料，AI宁可不推，甚至可能把你和别家搞混、说错你的项目和价格。",
            "投流成本一年比一年高，转化却越来越难。而AI推荐是「免费的信任背书」，你没占住，等于把最优质的免费获客入口，拱手让给了同行。",
        ],
        "strategy": "见微针对成都医美机构，围绕「求美者AI决策+信任重建」双线优化。先扫描AI在各类项目咨询问题上对你的可见度、竞品抢占度，以及是否存在「说错你」的信息风险（价格、项目、资质混淆）。随后帮你在AI信任的信源上布局合规、客观的科普与真实案例语料，既提升被推荐概率，也校准AI对你的认知，防止AI乱说损害口碑。以7天为周期监测优化，让求美者问AI时既能优先看到你、又能看到准确的你，把高价值到店客户稳稳接住。",
    },

    "shanghai-legal-service": {
        "slug": "shanghai-legal-service",
        "city": "上海",
        "industry": "法律服务",
        "seo_title": "上海律师事务所GEO优化方案 | 见微 - AI搜索获客与案源破局",
        "seo_desc": "上海律所在AI搜索中被同行截流案源？见微GEO方案专为法律服务设计，解决「AI搜索隐形」难题，让当事人问AI「上海哪个律师靠谱」时优先看到你，把高价值案源抢回来。",
        "pain_points": [
            "当事人遇到纠纷，第一时间问AI「上海XX纠纷找哪个律师」。AI报的是那几家大所，你的专业和胜诉案例在答案里彻底隐形——高价值案源就这么流走了。",
            "你的胜诉案例、专业文章都躺在律所官网里，AI抓不到、也不采信。法律是极重信任的决策，缺乏第三方权威语料佐证，AI宁可不推你。",
            "高净值当事人越来越依赖AI初筛律师。你没进AI的推荐名单，等于在最关键的获客入口前就出局了，优质案源持续被大所语料库截流。",
        ],
        "strategy": "见微针对上海律所，聚焦「当事人AI咨询决策+专业信任建立」双线优化。先扫描AI在各类法律咨询问题上对你的可见度与竞品抢占情况，锁定你最擅长的专业领域精准词。随后帮你把胜诉案例、专业解读重构成AI偏爱的结构化语料（法律科普、案例分析、常见问题解答），定向布局到知乎法律话题、专业问答平台等AI信任的信源。以7天为周期监测优化，让当事人问AI时优先看到你的专业度，把高价值案源真正接住。",
    },

    "guangzhou-b2b-manufacturing": {
        "slug": "guangzhou-b2b-manufacturing",
        "city": "广州",
        "industry": "外贸B2B",
        "seo_title": "广州外贸B2B企业GEO优化方案 | 见微 - AI搜索询盘破局专家",
        "seo_desc": "广州外贸企业如何在AI时代抢占询盘？见微GEO方案帮外贸B2B破解「AI搜索隐形」，在采购商询价决策链路中占据AI推荐位，把海内外询盘从对手手里夺回来。",
        "pain_points": [
            "海外及国内采购商选供应商，越来越多先问AI「XX产品哪家供应商靠谱」。你深耕多年，AI却一次没提——线下积累的口碑，在AI这里归零。",
            "你的产品优势、认证资质全在自家网站和展会资料里，AI抓不到、不采信。缺乏被广泛引用的结构化语料，AI判定信息不可靠，直接跳过你。",
            "高价值大额询盘正越来越多经AI初筛完成。AI没把你列入候选，询盘链路第一环就断了，订单商机在你看不见的地方持续蒸发给同行。",
        ],
        "strategy": "见微针对广州外贸B2B企业，专项优化「采购商AI询价决策链路」。先监测你在核心品类询价问题上的AI可见度与竞品抢占，锁定最易突破的精准词（品类+应用+认证）。随后帮你把资质、产能、真实合作案例重构成AI偏爱的结构化语料（选型指南、参数对比、行业问答），定向布局到AI信任的B2B信源与专业平台。以7天为周期持续投喂复测，让采购商问AI时优先提及你、信息准确，把断掉的询盘链路重新接通。",
    },

    "beijing-enterprise-training": {
        "slug": "beijing-enterprise-training",
        "city": "北京",
        "industry": "企业培训",
        "seo_title": "北京企业培训机构GEO优化方案 | 见微 - AI搜索获客破局",
        "seo_desc": "北京企业培训机构在AI搜索中被隐形？见微GEO方案帮培训机构解决AI推荐隐形问题，让HR和企业决策者问AI「北京哪家培训靠谱」时优先看到你，抢回高价值企业客户。",
        "pain_points": [
            "企业HR找培训供应商，先问AI「北京XX培训哪家专业」。AI报的是那几家老牌机构，你的课程和师资在答案里隐形——高客单的企业订单被同行拿走。",
            "你的课程体系、师资背景、客户案例都在官网里躺着，AI抓不到、不采信。缺乏第三方语料佐证，AI不敢把你推给企业决策者。",
            "企业采购培训越来越依赖AI初筛。你没进AI推荐名单，在最关键的B端获客入口就出局了，优质企业客户持续被对手的语料库截流。",
        ],
        "strategy": "见微针对北京企业培训机构，围绕「企业决策者AI选型」优化。先扫描AI在各类培训需求问题上对你的可见度与竞品抢占，锁定你最强的课程领域精准词。随后帮你把课程体系、师资、真实企业案例重构成AI偏爱的结构化语料（培训选型指南、课程解读、效果案例），布局到知乎职场话题、行业平台等AI信任信源。以7天为周期监测优化，让企业决策者问AI时优先看到你的专业，把高价值企业订单接住。",
    },

    "shenzhen-home-decoration": {
        "slug": "shenzhen-home-decoration",
        "city": "深圳",
        "industry": "家装公司",
        "seo_title": "深圳装修公司GEO优化方案 | 见微 - AI搜索获客与口碑破局",
        "seo_desc": "深圳装修公司在AI搜索中被竞品截流？见微GEO方案专为家装设计，解决AI推荐隐形问题，让业主问AI「深圳装修哪家靠谱」时优先看到你，把高意向业主从对手手里抢回来。",
        "pain_points": [
            "业主装修前必问AI「深圳装修哪家公司靠谱」。AI报的是那几家老对手，你的案例和口碑在答案里隐形——高意向、高客单的业主就这么被推给了别人。",
            "你的施工案例、业主好评都在官网和朋友圈里，AI抓不到、不采信。家装是重信任决策，缺乏第三方语料佐证，AI宁可不推你。",
            "获客成本一年比一年高，而AI推荐是免费的信任背书。你没占住，等于把最优质的免费获客入口拱手让给同行，业主商机持续蒸发。",
        ],
        "strategy": "见微针对深圳装修公司，围绕「业主AI决策+口碑建立」双线优化。先扫描AI在各类装修咨询问题上对你的可见度、竞品抢占，以及是否存在说错你信息的风险。随后帮你在AI信任的信源上布局客观的装修科普与真实案例语料，既提升被推荐概率，也校准AI对你的认知。以7天为周期监测优化，让业主问AI时优先看到准确的你，把高价值业主稳稳接住。",
    },

    "hangzhou-medical-checkup": {
        "slug": "hangzhou-medical-checkup",
        "city": "杭州",
        "industry": "口腔诊所",
        "seo_title": "杭州口腔诊所GEO优化方案 | 见微 - AI搜索获客破局专家",
        "seo_desc": "杭州口腔诊所在AI搜索中被同行截流客户？见微GEO方案专为口腔医疗设计，解决AI推荐隐形问题，让患者问AI「杭州种牙/正畸哪家好」时优先看到你，抢回高价值到诊客户。",
        "pain_points": [
            "患者做种牙、正畸前必问AI「杭州XX哪家诊所好」。AI报的是那几家连锁，你的技术和口碑在答案里隐形——高客单的到诊客户被同行拿走。",
            "你的医生资质、真实案例都在诊所官网里，AI抓不到、不采信。口腔医疗是重信任高客单决策，缺乏第三方权威语料，AI不敢推你、甚至可能说错你的项目价格。",
            "投流获客越来越贵，转化越来越难。AI推荐这个免费的信任入口你没占住，高价值患者持续被对手的语料库截流。",
        ],
        "strategy": "见微针对杭州口腔诊所，围绕「患者AI决策+医疗信任」双线优化。先扫描AI在种牙、正畸等项目咨询上对你的可见度、竞品抢占，以及是否存在说错项目价格的风险。随后帮你在AI信任的信源布局合规、客观的口腔科普与真实案例语料，既提升被推荐概率，也防止AI乱说损害口碑。以7天为周期监测优化，让患者问AI时优先看到准确的你，把高价值到诊客户接住。",
    },

    "chengdu-pet-service": {
        "slug": "chengdu-pet-service",
        "city": "成都",
        "industry": "宠物医院",
        "seo_title": "成都宠物医院GEO优化方案 | 见微 - AI搜索获客与口碑破局",
        "seo_desc": "成都宠物医院在AI搜索中被同行截流？见微GEO方案专为宠物服务设计，解决AI推荐隐形问题，让宠主问AI「成都靠谱的宠物医院」时优先看到你，抢回高粘性客户。",
        "pain_points": [
            "宠主给毛孩子看病前必问AI「成都哪家宠物医院靠谱」。AI报的是那几家连锁，你的专业和口碑在答案里隐形——高粘性、高复购的宠主被同行拿走。",
            "你的医疗设备、专业案例、宠主好评都在朋友圈和官网里，AI抓不到、不采信。宠物医疗是重信任决策，缺乏第三方语料佐证，AI不敢推你。",
            "宠物经济高速增长，但AI推荐这个免费信任入口你没占住。宠主越来越依赖AI找医院，你不在名单里，高价值客户持续被对手截流。",
        ],
        "strategy": "见微针对成都宠物医院，围绕「宠主AI决策+专业口碑」双线优化。先扫描AI在各类宠物医疗咨询上对你的可见度、竞品抢占，以及是否存在说错你信息的风险。随后帮你在AI信任的信源布局客观的宠物健康科普与真实诊疗案例语料，既提升被推荐概率，也校准AI对你的认知。以7天为周期监测优化，让宠主问AI时优先看到专业准确的你，把高粘性客户接住。",
    },

}


def _valid_slug(slug: str) -> bool:
    """只允许小写字母、数字、连字符，防注入与异常路径。"""
    return bool(re.fullmatch(r"[a-z0-9\-]{1,80}", slug or ""))


# ============================================================
# 合并数据源：手写精品页 + 批量生成页
# 手写页优先级最高（同 slug 时手写覆盖批量），保证精品内容不被覆盖。
# ============================================================
try:
    from pseo_generator import build_generated_db
    _GENERATED_DB = build_generated_db()
except Exception:
    _GENERATED_DB = {}

# 手写优先：先铺生成的，再用手写的覆盖同名 slug
FULL_DB = {**_GENERATED_DB, **INDUSTRY_DB}


def register_pseo(app):
    """把 pSEO 相关路由注册到主 app 上。"""

    # ---------- 行业方案动态页（核心 SSR 路由）----------
    @app.get("/solutions/{slug}", response_class=HTMLResponse)
    def solution_page(slug: str, request: Request):
        if not _valid_slug(slug):
            raise HTTPException(status_code=404, detail="页面不存在")
        data = FULL_DB.get(slug)
        if not data:
            # 友好 404：返回一个引导回首页/查看全部方案的页面
            # 404 页只展示手写精品页，避免列表过长
            return templates.TemplateResponse(
                "geo_404.html",
                {"request": request, "site_base": SITE_BASE,
                 "all_solutions": list(INDUSTRY_DB.values())},
                status_code=404,
            )
        ctx = dict(data)
        ctx.update({
            "request": request,
            "canonical": f"{SITE_BASE}/solutions/{slug}",
            "site_base": SITE_BASE,
            "year": datetime.now().year,
        })
        return templates.TemplateResponse("geo_template.html", ctx)

    # ---------- 方案总览页（内链聚合，利于收录）----------
    @app.get("/solutions", response_class=HTMLResponse)
    def solutions_index(request: Request):
        return templates.TemplateResponse(
            "geo_index.html",
            {"request": request, "site_base": SITE_BASE,
             "all_solutions": list(FULL_DB.values()),
             "year": datetime.now().year},
        )

    # ---------- sitemap.xml（让爬虫一次发现所有行业页）----------
    @app.get("/sitemap.xml", response_class=PlainTextResponse)
    def sitemap():
        urls = [f"{SITE_BASE}/solutions"]
        urls += [f"{SITE_BASE}/solutions/{s}" for s in FULL_DB]
        items = "".join(
            f"<url><loc>{u}</loc><changefreq>weekly</changefreq>"
            f"<priority>0.8</priority></url>" for u in urls
        )
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               f'{items}</urlset>')
        return PlainTextResponse(content=xml, media_type="application/xml")

    # ---------- robots.txt（引导爬虫抓 solutions，屏蔽后台/接口）----------
    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        txt = (
            "User-agent: *\n"
            "Allow: /solutions\n"
            "Allow: /guide\n"
            "Allow: /tutorial\n"
            "Disallow: /api/\n"      # 屏蔽所有API接口，防爬虫瞎抓消耗资源
            "Disallow: /admin\n"    # 屏蔽后台
            f"Sitemap: {SITE_BASE}/sitemap.xml\n"
        )
        return PlainTextResponse(content=txt, media_type="text/plain")

    # ---------- pSEO 行业页线索收集接口 ----------
    from pydantic import BaseModel
    from fastapi import Depends
    from database import get_session, PseoLead

    class PseoLeadReq(BaseModel):
        name: str = ""
        phone: str = ""
        company: str = ""
        slug: str = ""
        loss_estimate: float = 0.0
        note: str = ""

    @app.post("/api/pseo/lead")
    def collect_pseo_lead(req: PseoLeadReq, request: Request,
                          session=Depends(get_session)):
        """收集 pSEO 行业页留资。带行业来源归因，防刷限流。"""
        # 基础校验：电话必填
        phone = (req.phone or "").strip()
        if not phone or len(phone) < 5:
            raise HTTPException(status_code=400, detail="请填写有效的联系方式")
        # 限流：同一IP每小时最多10条，防恶意灌水
        try:
            _pseo_rate_limit(request)
        except HTTPException:
            raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试")
        # 全局每日上限：防脚本大规模灌爆数据库（保命）
        _pseo_daily_guard()
        # 从 slug 反查行业城市，做归因冗余
        meta = FULL_DB.get(req.slug or "", {})
        lead = PseoLead(
            name=(req.name or "")[:50],
            phone=phone[:30],
            company=(req.company or "")[:80],
            slug=(req.slug or "")[:80],
            industry=meta.get("industry", ""),
            city=meta.get("city", ""),
            loss_estimate=float(req.loss_estimate or 0),
            note=(req.note or "")[:500],
        )
        session.add(lead)
        session.commit()
        return {"ok": True, "msg": "已收到，我们会尽快联系你"}

    return app


# 简易IP限流（pSEO 独立，不依赖 main.py 的限流器）
_pseo_hits = {}

# 全局每日线索熔断（防脚本灌爆数据库）
_pseo_daily = {"date": "", "count": 0}

def _pseo_daily_guard():
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if _pseo_daily["date"] != today:
        _pseo_daily["date"] = today
        _pseo_daily["count"] = 0
    max_daily = int(os.getenv("MAX_DAILY_PSEO_LEADS", "300"))
    if _pseo_daily["count"] >= max_daily:
        raise HTTPException(status_code=503, detail="今日提交量已达上限，请加顾问微信 jenly222")
    _pseo_daily["count"] += 1

def _pseo_rate_limit(request: Request, max_calls: int = 10, window_sec: int = 3600):
    import time as _t
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    now = _t.time()
    key = f"pseolead:{ip}"
    hits = [t for t in _pseo_hits.get(key, []) if now - t < window_sec]
    if len(hits) >= max_calls:
        raise HTTPException(status_code=429, detail="rate limited")
    hits.append(now)
    _pseo_hits[key] = hits
