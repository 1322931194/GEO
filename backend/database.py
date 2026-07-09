"""
GEO 雷达 - 数据层
==================
用 SQLModel(SQLAlchemy 封装),默认 SQLite,生产可一行切换 Postgres。
包含多商家 SaaS 的最小可用数据模型:用户、品牌、报告、订阅。
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlmodel import SQLModel, Field, create_engine, Session

# 中国时区时间（UTC+8），解决服务器UTC时间比本地慢8小时的问题
def cn_now():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)

# 默认 SQLite,生产环境改 DATABASE_URL=postgresql://... 即可,无需改代码
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./geo_radar.db")
engine = create_engine(DATABASE_URL, echo=False,
                       connect_args={"check_same_thread": False}
                       if DATABASE_URL.startswith("sqlite") else {})


# 套餐配额配置
# ===== 最终定价体系：免费 / 19.9单次内容包 / 599增长(限次) / 980畅享(不限) / 定制 =====
# 成本核算：1次完整监测≈¥1-3(API)，内容生成≈¥0.5/次，pSEO页=0(模板生成零AI成本)
PLANS = {
    "trial": {
        "name": "免费版", "price_cny": 0, "brands": 1,
        "questions": 20, "samples": 1, "platforms": 3, "freq": "once",
        "monitor_limit": 3,   # 免费给3次监测（让他反复查、看到AI不推他）
        "battle_limit": 0,    # 作战包只给预览，完整要付费
        "content_limit": 0,   # 不能生成内容 → 制造付费动力
        "pseo_limit": 0,
        # 高成本高级功能配额（每月）
        "sandbox_limit": 0,   # 多轮追问沙盒
        "bulk_limit": 0,      # 批量关键词监测
        "semgap_limit": 0,    # 语义差距分析
    },
    "single": {
        "name": "单次内容包", "price_cny": 19.9, "brands": 1,
        "questions": 50, "samples": 1, "platforms": 6, "freq": "once",
        "monitor_limit": 3,   # 保留监测
        "battle_limit": 1,    # 1次完整作战报告（可导出）
        "content_limit": 5,   # 1次解锁：关键词+长尾词+内容生成（5次够落地一轮）
        "pseo_limit": 0,
        "sandbox_limit": 0, "bulk_limit": 0, "semgap_limit": 0,
        "is_onetime": True,   # 单次买断，非订阅
    },
    "monthly": {
        "name": "增长版", "price_cny": 599, "brands": 3,
        "questions": 80, "samples": 1, "platforms": 9, "freq": "weekly",
        "monitor_limit": 50,  # 每月50次监测
        "battle_limit": 50,   # 50次作战报告
        "content_limit": 50,  # 50次内容生成
        "pseo_limit": 3,      # 含3个pSEO获客落地页
        # 高成本功能：每月1次（控制成本，同时是高感知卖点）
        "sandbox_limit": 1,   # 多轮追问沙盒 1次/月
        "bulk_limit": 1,      # 批量关键词监测 1次/月
        "semgap_limit": 1,    # 语义差距分析 1次/月
    },
    "pro_monthly": {
        "name": "畅享版", "price_cny": 980, "brands": 10,
        "questions": 120, "samples": 1, "platforms": 12, "freq": "daily",
        "monitor_limit": 999,  # 不限次监测
        "battle_limit": 999,   # 不限作战报告
        "content_limit": 999,  # 不限内容生成
        "pseo_limit": 15,       # 含15个pSEO落地页
        "index_board": True,    # 独家：收录数据大盘
        # 高成本功能：每月2次
        "sandbox_limit": 2,   # 多轮追问沙盒 2次/月
        "bulk_limit": 2,      # 批量关键词监测 2次/月
        "semgap_limit": 2,    # 语义差距分析 2次/月
    },
    "custom": {
        "name": "企业定制", "price_cny": 0, "brands": 30,
        "questions": 200, "samples": 3, "platforms": 12, "freq": "daily",
        "monitor_limit": 999, "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 100,    # 定制：pSEO矩阵+代运营，价格面议
        "index_board": True,
        "sandbox_limit": 10, "bulk_limit": 10, "semgap_limit": 10,
    },
    # ===== 以下为旧套餐（仅兼容历史付费用户，价格页不再展示）=====
    "starter": {
        "name": "季付版(旧)", "price_cny": 1980, "brands": 5,
        "questions": 120, "samples": 1, "platforms": 6, "freq": "weekly",
        "monitor_limit": 999, "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 3,
    },
    "starter_trial": {
        "name": "体验版(旧)", "price_cny": 39.9, "brands": 1,
        "questions": 30, "samples": 1, "platforms": 4, "freq": "once",
        "monitor_limit": 1, "battle_limit": 1, "content_limit": 1,
        "pseo_limit": 0,
    },
    "pro": {
        "name": "企业版(旧)", "price_cny": 3980, "brands": 5,
        "questions": 150, "samples": 2, "platforms": 6, "freq": "daily",
        "monitor_limit": 999, "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 10,
    },
    "business": {
        "name": "旗舰版(旧)", "price_cny": 9800, "brands": 10,
        "questions": 999, "samples": 3, "platforms": 8, "freq": "daily",
        "monitor_limit": 999, "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 30,
    },
}


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    token_version: int = Field(default=0)   # token版本号，改密码/强制登出时+1使旧token失效
    plan: str = Field(default="trial")
    monitor_count: int = Field(default=0)   # 累计监测次数（试用版限制用）
    battle_count: int = Field(default=0)     # 累计作战包生成次数（额度控制）
    content_count: int = Field(default=0)    # 累计内容生成次数（额度控制）
    is_admin: bool = Field(default=False)   # 管理员标记
    invite_code: str = Field(default="", index=True)  # 自己的专属邀请码
    referred_by: int = Field(default=0)     # 被谁推荐（推荐人用户ID，0=无）
    created_at: datetime = Field(default_factory=cn_now)
    trial_ends_at: Optional[datetime] = None
    # ===== 后台管理/营销分析字段 =====
    total_spent: float = Field(default=0.0)          # 累计消费金额（元）
    last_login_at: Optional[datetime] = None         # 最后登录时间（判断活跃/沉默）
    login_count: int = Field(default=0)              # 累计登录次数
    plan_expires_at: Optional[datetime] = None       # 付费套餐到期日（流失预警用）
    order_count: int = Field(default=0)              # 累计付费订单数
    # ===== 高成本高级功能使用计数（每月重置）=====
    sandbox_count: int = Field(default=0)   # 多轮追问沙盒已用次数
    bulk_count: int = Field(default=0)      # 批量监测已用次数
    semgap_count: int = Field(default=0)    # 语义差距分析已用次数


class Brand(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    website: str = ""
    industry: str = ""
    product: str = ""
    target_market: str = "海外"
    target_lang: str = "en"          # 出海目标语言
    mode: str = "outbound"           # outbound=出海模式  domestic=国内模式
    competitors: str = ""            # 逗号分隔
    brand_facts: str = ""            # 知识库抓取的事实
    # ===== 品牌语料库与人设引擎（资产沉淀）=====
    brand_persona: str = ""          # 品牌人设：语气/风格（如"专业但亲切，像懂行的老朋友"）
    brand_slogans: str = ""          # 核心卖点/口号（每行一条，生成内容必带）
    brand_taboos: str = ""           # 内容禁忌（不能说的话，如"不承诺疗效"）
    region: str = ""                 # 地区（本地商户用：城市/区，生成区域词）
    address: str = ""                # 门店地址（本地GEO用）
    phone: str = ""                  # 联系电话（本地GEO用）
    business_hours: str = ""         # 营业时间（本地GEO用）
    questions_json: str = "[]"       # 问题集
    keywords_cache: str = ""         # 关键词提取结果缓存（避免重复调用AI烧token）
    track_id: str = ""               # AI访客追踪码（首次访问追踪页时生成）
    created_at: datetime = Field(default_factory=cn_now)


class Referral(SQLModel, table=True):
    """分销记录：谁通过谁的邀请码注册/付费"""
    id: Optional[int] = Field(default=None, primary_key=True)
    referrer_id: int = Field(index=True)       # 推广者用户ID
    referred_user_id: int = Field(index=True)  # 被推荐的新用户ID
    referred_email: str = ""                    # 被推荐用户邮箱
    status: str = "registered"                  # registered=已注册 paid=已付费
    commission: float = 0.0                     # 佣金金额
    paid_plan: str = ""                          # 付费的套餐
    created_at: datetime = Field(default_factory=cn_now)
    paid_at: Optional[datetime] = None


class KnowledgeItem(SQLModel, table=True):
    """
    品牌知识库条目：商家存的品牌资料。
    这是产品护城河——存得越多，生成内容越准，迁移成本越高。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True)
    category: str = "fact"        # fact=品牌事实 selling_point=卖点 faq=常见问答 story=品牌故事
    title: str = ""               # 标题/问题
    content: str = ""             # 内容/答案
    source: str = "manual"        # manual=手动添加 crawled=官网抓取 ai=AI生成
    created_at: datetime = Field(default_factory=cn_now)


class Order(SQLModel, table=True):
    """
    支付订单。支付成功后自动开通套餐 + 结算分销佣金。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: str = Field(index=True, unique=True)   # 商户订单号
    user_id: int = Field(index=True)
    plan: str = ""                  # 购买的套餐
    amount: float = 0.0             # 金额（元）
    status: str = "pending"         # pending=待支付 paid=已支付 failed=失败
    pay_method: str = ""            # wxpay / alipay
    pay_no: str = ""                # 支付平台流水号
    created_at: datetime = Field(default_factory=cn_now, index=True)
    paid_at: Optional[datetime] = None


class ApiCallLog(SQLModel, table=True):
    """
    AI API 调用日志。每次监测后汇总记录，用于在管理后台看真实消耗。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, default=0)
    brand_id: int = Field(default=0)
    platform: str = ""              # 调用的平台（deepseek/doubao等）
    scene: str = "other"            # 调用场景（monitor/extract/questions/content/check_keys）
    calls: int = 0                  # 本次调用次数
    success: int = 0                # 成功次数
    failed: int = 0                 # 失败次数
    est_cost: float = 0.0           # 估算成本（元）
    created_at: datetime = Field(default_factory=cn_now, index=True)


class Report(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True)
    generated_at: datetime = Field(default_factory=cn_now)
    mention_rate: float = 0.0
    competitor_share_json: str = "{}"
    platform_breakdown_json: str = "{}"
    gaps_json: str = "[]"
    source_count: int = 0
    sample_note: str = ""
    full_json: str = "{}"          # 完整报告留档


class AIEvidence(SQLModel, table=True):
    """AI推荐证据库：保存每次AI回答原文、时间、平台、问题、竞品。
    需求⑤：这是商家续费的核心证据——证明'AI以前不推你/现在推你了'。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    brand_id: int = Field(default=0, index=True)
    report_id: int = Field(default=0, index=True)  # 关联哪次监测
    question: str = ""                             # 客户问的问题
    platform: str = ""                             # 哪个AI平台
    answer_text: str = ""                          # AI回答原文（核心证据）
    mentioned: bool = False                        # 这次有没有提到你
    competitors_found: str = ""                    # 这次回答里出现的竞品（逗号分隔）
    cited_sources: str = ""                        # AI引用了哪些来源
    # ===== P0新增：情绪倾向 + 推荐位置权重 =====
    sentiment: str = ""            # 情绪：positive/neutral/negative/absent
    sentiment_reason: str = ""     # 判断依据（原文片段）
    position_ratio: float = -1.0   # 品牌词在回答中的位置比例(0~1)，-1=未提及
    position_level: str = ""       # core(前20%) / middle / tail(后20%) / absent
    captured_at: datetime = Field(default_factory=cn_now, index=True)


class GeneratedContent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True)
    gap_question: str = ""
    content_type: str = "website"
    title: str = ""
    body: str = ""
    publish_tip: str = ""
    status: str = "draft"          # draft / published(人工标记)
    # 需求⑥：多平台分发追踪（记录发到哪些平台、是否收录、是否被AI引用）
    distribute_json: str = "{}"    # {"官网":true,"知乎":true,"公众号":false,"百家号":false}
    is_indexed: bool = False       # 是否被搜索/AI收录
    is_cited: bool = False         # 是否被AI引用（最终目标）
    created_at: datetime = Field(default_factory=cn_now)


class AIVisit(SQLModel, table=True):
    """AI来源访客记录：商家官网被AI推荐后带来的真实访客"""
    id: Optional[int] = Field(default=None, primary_key=True)
    track_id: str = Field(index=True)        # 品牌的追踪码
    source: str = ""                          # AI来源：chatgpt/perplexity/deepseek/gemini/copilot/other
    referrer: str = ""                        # 完整来源URL
    landing_page: str = ""                    # 落地页
    user_agent: str = ""                      # 设备信息
    visited_at: datetime = Field(default_factory=cn_now, index=True)


class Conversion(SQLModel, table=True):
    """转化事件：AI访客产生的留资/咨询/下单，用于 ROI 归因。
    商家通过追踪代码或手动上报，形成'AI访客→转化'的归因闭环。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    track_id: str = Field(index=True)         # 品牌追踪码
    event_type: str = "lead"                  # lead=留资 consult=咨询 order=下单
    value: float = 0.0                        # 转化价值（订单金额，元）
    source: str = ""                          # 归因来源（哪个AI平台带来的）
    note: str = ""                            # 备注
    created_at: datetime = Field(default_factory=cn_now, index=True)


class IndustrySample(SQLModel, table=True):
    """
    行业匿名样本：每次监测后存一条。
    只存行业+提及率+模式，不存品牌名，完全匿名。
    积累足够后用于生成"行业大盘"，告诉商家在行业里的排名。
    这是数据护城河——越多人用越准，且别人做不出来。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    industry: str = Field(index=True)         # 行业（标准化后）
    industry_raw: str = ""                     # 原始行业输入
    mode: str = "outbound"                      # outbound/domestic
    mention_rate: float = 0.0                  # 提及率
    source_count: int = 0                      # 被引用来源数
    brand_id_hash: str = ""                    # 品牌ID哈希（去重用，不可反推品牌）
    created_at: datetime = Field(default_factory=cn_now, index=True)


class PseoLead(SQLModel, table=True):
    """程序化建站(pSEO)行业页产生的销售线索。
    通过 /solutions/{slug} 行业页的留资表单收集，带行业来源归因。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""                             # 联系人
    phone: str = Field(default="", index=True) # 电话/微信
    company: str = ""                          # 公司/品牌
    slug: str = Field(default="", index=True)  # 来源行业页 slug
    industry: str = ""                         # 行业（冗余，便于统计）
    city: str = ""                             # 城市
    loss_estimate: float = 0.0                 # 页面测算器算出的预估月损失（元）
    note: str = ""                             # 备注/留言
    created_at: datetime = Field(default_factory=cn_now, index=True)


class CustomerPseoPage(SQLModel, table=True):
    """客户自助生成的 pSEO 行业落地页。
    付费客户在产品内填写自己的行业/城市/优势，生成专属落地页，
    通过 /s/{page_slug} 独立访问，帮客户做 AI/搜索获客。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)           # 归属用户
    page_slug: str = Field(default="", index=True)  # URL标识（全站唯一）
    city: str = ""                             # 城市
    industry: str = ""                         # 行业
    brand_name: str = ""                       # 客户品牌名
    advantages: str = ""                       # 客户优势（用于生成内容）
    contact: str = ""                          # 客户自己的联系方式（页面展示）
    address: str = ""                          # 门店/公司地址（生成LocalBusiness结构化数据）
    buy_link: str = ""                         # 购买/预约链接（官网、店铺、小程序等）
    seo_title: str = ""                        # 生成的SEO标题
    seo_desc: str = ""                         # 生成的SEO描述
    pain_points_json: str = "[]"               # 痛点（JSON数组）
    strategy: str = ""                         # 策略正文
    views: int = 0                             # 页面浏览量
    is_active: bool = True                     # 是否上线
    created_at: datetime = Field(default_factory=cn_now, index=True)


class TrackEvent(SQLModel, table=True):
    """用户行为事件追踪，用于运营看板分析转化。
    event 类型：page_view(访问)、register(注册)、click_check(点免费检测)、
    click_upgrade(点升级)、click_pay(点支付)、monitor(做监测) 等。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    event: str = Field(index=True, default="page_view")  # 事件类型
    user_id: int = Field(default=0, index=True)           # 登录用户ID(未登录=0)
    visitor_id: str = Field(default="", index=True)       # 匿名访客标识(前端生成)
    page: str = ""                                        # 页面路径
    referrer: str = ""                                    # 来源
    meta: str = ""                                        # 附加信息(JSON)
    created_at: datetime = Field(default_factory=cn_now, index=True)


class IndexRecord(SQLModel, table=True):
    """收录明细：收录数据大盘用。每行 = 某拓展词在某平台某端的收录记录。
    支持点击'查看'跳转到对应 AI 平台验证。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    brand_id: int = Field(default=0, index=True)
    main_keyword: str = ""                     # 主关键词（蒸馏词）
    expand_keyword: str = ""                   # 拓展词
    platform: str = ""                         # deepseek/doubao/yuanbao/qwen/wenxin/nano/kimi/zhipu
    device: str = "mobile"                     # mobile移动端 / pc电脑端
    query_url: str = ""                        # 点"查看"跳转的验证链接
    indexed_at: datetime = Field(default_factory=cn_now, index=True)


class IndexTrack(SQLModel, table=True):
    """收录追踪：用户发布内容后，追踪该内容/关键词是否被 AI 引用收录。
    这是 GEO 的核心闭环——发了之后，AI 到底认不认。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    brand_id: int = Field(default=0, index=True)
    keyword: str = ""                          # 关键词/主题
    platform: str = ""                         # 发布平台(知乎/搜狐等)
    url: str = ""                              # 发布内容URL(可选)
    status: str = "pending"                    # pending待检测 / indexed已收录 / not_yet暂未收录
    first_indexed_at: Optional[datetime] = None  # 首次被AI引用时间
    check_count: int = 0                       # 已检测次数
    last_check_at: Optional[datetime] = None
    note: str = ""
    created_at: datetime = Field(default_factory=cn_now, index=True)


def init_db():
    SQLModel.metadata.create_all(engine)
    _auto_migrate()


def _auto_migrate():
    """
    自动迁移：给已存在的表补充新增字段。
    create_all 不会给旧表加新列，所以手动 ALTER。
    每条都用 try 包裹，字段已存在时静默跳过。
    兼容 SQLite 和 PostgreSQL。
    """
    from sqlalchemy import text
    # 需要补的字段：(表名, 字段名, 类型与默认值)
    migrations = [
        ("aievidence", "sentiment", "VARCHAR DEFAULT ''"),
        ("aievidence", "sentiment_reason", "VARCHAR DEFAULT ''"),
        ("aievidence", "position_ratio", "FLOAT DEFAULT -1"),
        ("aievidence", "position_level", "VARCHAR DEFAULT ''"),
        ("customerpseopage", "address", "VARCHAR DEFAULT ''"),
        ("customerpseopage", "buy_link", "VARCHAR DEFAULT ''"),
        ("user", "invite_code", "VARCHAR DEFAULT ''"),
        ("user", "referred_by", "INTEGER DEFAULT 0"),
        ("user", "total_spent", "FLOAT DEFAULT 0"),
        ("user", "last_login_at", "TIMESTAMP"),
        ("user", "login_count", "INTEGER DEFAULT 0"),
        ("user", "plan_expires_at", "TIMESTAMP"),
        ("user", "order_count", "INTEGER DEFAULT 0"),
        ("user", "sandbox_count", "INTEGER DEFAULT 0"),
        ("user", "bulk_count", "INTEGER DEFAULT 0"),
        ("user", "semgap_count", "INTEGER DEFAULT 0"),
        ("user", "monitor_count", "INTEGER DEFAULT 0"),
        ("user", "battle_count", "INTEGER DEFAULT 0"),
        ("user", "content_count", "INTEGER DEFAULT 0"),
        ("user", "token_version", "INTEGER DEFAULT 0"),
        ("user", "is_admin", "BOOLEAN DEFAULT FALSE"),
        ("user", "trial_ends_at", "TIMESTAMP"),
        ("brand", "track_id", "VARCHAR DEFAULT ''"),
        ("brand", "mode", "VARCHAR DEFAULT 'outbound'"),
        ("brand", "keywords_cache", "VARCHAR DEFAULT ''"),
        ("brand", "region", "VARCHAR DEFAULT ''"),
        ("brand", "target_lang", "VARCHAR DEFAULT 'en'"),
        ("brand", "brand_persona", "VARCHAR DEFAULT ''"),
        ("brand", "brand_slogans", "VARCHAR DEFAULT ''"),
        ("brand", "brand_taboos", "VARCHAR DEFAULT ''"),
        ("brand", "address", "VARCHAR DEFAULT ''"),
        ("brand", "phone", "VARCHAR DEFAULT ''"),
        ("brand", "business_hours", "VARCHAR DEFAULT ''"),
        ("generatedcontent", "distribute_json", "VARCHAR DEFAULT '{}'"),
        ("generatedcontent", "is_indexed", "BOOLEAN DEFAULT FALSE"),
        ("generatedcontent", "is_cited", "BOOLEAN DEFAULT FALSE"),
        ("apicalllog", "scene", "VARCHAR DEFAULT 'other'"),
    ]
    is_sqlite = str(engine.url).startswith("sqlite")
    with engine.connect() as conn:
        for table, col, coltype in migrations:
            # 表名在不同库的引用方式
            tbl = f'"{table}"' if not is_sqlite else table
            try:
                conn.execute(text(f'ALTER TABLE {tbl} ADD COLUMN {col} {coltype}'))
                conn.commit()
            except Exception:
                # 字段已存在或表不存在，跳过
                try:
                    conn.rollback()
                except Exception:
                    pass


def get_session():
    with Session(engine) as session:
        yield session
