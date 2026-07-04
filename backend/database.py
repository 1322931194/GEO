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
# ===== 新4档体系：免费 / 99单次 / 699增长 / 定制 =====
# 成本核算：1次完整监测≈¥1-3(API)，内容生成≈¥0.5/次，pSEO页=0(模板生成零AI成本)
PLANS = {
    "trial": {
        "name": "免费版", "price_cny": 0, "brands": 1,
        "questions": 20, "samples": 1, "platforms": 3, "freq": "once",
        "monitor_limit": 1,   # 免费给1次监测（3平台，让他看到AI不推他）
        "battle_limit": 0,    # 作战包只给预览（前端控制），完整要付费
        "content_limit": 0,
        "pseo_limit": 0,
    },
    "single": {
        "name": "单次版", "price_cny": 99, "brands": 1,
        "questions": 50, "samples": 1, "platforms": 5, "freq": "once",
        "monitor_limit": 1,   # 1次完整监测（5平台）
        "battle_limit": 1,    # 完整作战包
        "content_limit": 3,   # 3次内容生成（成本低，给足让他把方案落地）
        "pseo_limit": 0,
    },
    "monthly": {
        "name": "AI Growth Pro", "price_cny": 599, "brands": 3,
        "questions": 80, "samples": 1, "platforms": 6, "freq": "weekly",
        "monitor_limit": 8,   # 每月8次监测（够每周复测）
        "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 3,      # ★ 含3个pSEO获客落地页（零边际成本，高感知价值）
    },
    "custom": {
        "name": "定制版", "price_cny": 0, "brands": 10,
        "questions": 200, "samples": 3, "platforms": 8, "freq": "daily",
        "monitor_limit": 999, "battle_limit": 999, "content_limit": 999,
        "pseo_limit": 30,     # 定制客户：pSEO矩阵+代运营，价格面议，后台手动开通
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


class Brand(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    website: str = ""
    industry: str = ""
    product: str = ""
    target_market: str = "海外"
    mode: str = "outbound"           # outbound=出海模式  domestic=国内模式
    competitors: str = ""            # 逗号分隔
    brand_facts: str = ""            # 知识库抓取的事实
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


class GeneratedContent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True)
    gap_question: str = ""
    content_type: str = "website"
    title: str = ""
    body: str = ""
    publish_tip: str = ""
    status: str = "draft"          # draft / published(人工标记)
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
        ("customerpseopage", "address", "VARCHAR DEFAULT ''"),
        ("customerpseopage", "buy_link", "VARCHAR DEFAULT ''"),
        ("user", "invite_code", "VARCHAR DEFAULT ''"),
        ("user", "referred_by", "INTEGER DEFAULT 0"),
        ("user", "monitor_count", "INTEGER DEFAULT 0"),
        ("user", "battle_count", "INTEGER DEFAULT 0"),
        ("user", "content_count", "INTEGER DEFAULT 0"),
        ("user", "token_version", "INTEGER DEFAULT 0"),
        ("user", "is_admin", "BOOLEAN DEFAULT FALSE"),
        ("user", "trial_ends_at", "TIMESTAMP"),
        ("brand", "track_id", "VARCHAR DEFAULT ''"),
        ("brand", "mode", "VARCHAR DEFAULT 'outbound'"),
        ("brand", "keywords_cache", "VARCHAR DEFAULT ''"),
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
