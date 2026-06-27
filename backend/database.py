"""
GEO 雷达 - 数据层
==================
用 SQLModel(SQLAlchemy 封装),默认 SQLite,生产可一行切换 Postgres。
包含多商家 SaaS 的最小可用数据模型:用户、品牌、报告、订阅。
"""

import os
from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field, create_engine, Session

# 默认 SQLite,生产环境改 DATABASE_URL=postgresql://... 即可,无需改代码
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./geo_radar.db")
engine = create_engine(DATABASE_URL, echo=False,
                       connect_args={"check_same_thread": False}
                       if DATABASE_URL.startswith("sqlite") else {})


# 套餐配额配置
PLANS = {
    "trial": {
        "name": "免费试用", "price_cny": 0, "brands": 1,
        "questions": 20, "samples": 1, "platforms": 4, "freq": "once",
        "monitor_limit": 0,   # 0=不能监测，必须付9.9才能看报告
    },
    "starter_trial": {
        "name": "9.9元体验", "price_cny": 9.9, "brands": 1,
        "questions": 30, "samples": 1, "platforms": 4, "freq": "once",
        "monitor_limit": 1,   # 只能跑1次完整监测
    },
    "starter": {
        "name": "专业版", "price_cny": 1980, "brands": 3,
        "questions": 50, "samples": 1, "platforms": 4, "freq": "weekly",
        "monitor_limit": 4,   # 每月4次
    },
    "pro": {
        "name": "企业版", "price_cny": 3980, "brands": 5,
        "questions": 150, "samples": 2, "platforms": 4, "freq": "daily",
        "monitor_limit": 999,  # 不限
    },
    "business": {
        "name": "旗舰版", "price_cny": 9800, "brands": 10,
        "questions": 999, "samples": 3, "platforms": 4, "freq": "daily",
        "monitor_limit": 999,  # 不限
    },
}


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    plan: str = Field(default="trial")
    monitor_count: int = Field(default=0)   # 累计监测次数（试用版限制用）
    is_admin: bool = Field(default=False)   # 管理员标记
    invite_code: str = Field(default="", index=True)  # 自己的专属邀请码
    referred_by: int = Field(default=0)     # 被谁推荐（推荐人用户ID，0=无）
    created_at: datetime = Field(default_factory=datetime.utcnow)
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
    track_id: str = ""               # AI访客追踪码（首次访问追踪页时生成）
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Referral(SQLModel, table=True):
    """分销记录：谁通过谁的邀请码注册/付费"""
    id: Optional[int] = Field(default=None, primary_key=True)
    referrer_id: int = Field(index=True)       # 推广者用户ID
    referred_user_id: int = Field(index=True)  # 被推荐的新用户ID
    referred_email: str = ""                    # 被推荐用户邮箱
    status: str = "registered"                  # registered=已注册 paid=已付费
    commission: float = 0.0                     # 佣金金额
    paid_plan: str = ""                          # 付费的套餐
    created_at: datetime = Field(default_factory=datetime.utcnow)
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
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    paid_at: Optional[datetime] = None


class Report(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
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
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIVisit(SQLModel, table=True):
    """AI来源访客记录：商家官网被AI推荐后带来的真实访客"""
    id: Optional[int] = Field(default=None, primary_key=True)
    track_id: str = Field(index=True)        # 品牌的追踪码
    source: str = ""                          # AI来源：chatgpt/perplexity/deepseek/gemini/copilot/other
    referrer: str = ""                        # 完整来源URL
    landing_page: str = ""                    # 落地页
    user_agent: str = ""                      # 设备信息
    visited_at: datetime = Field(default_factory=datetime.utcnow, index=True)


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
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


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
        ("user", "invite_code", "VARCHAR DEFAULT ''"),
        ("user", "referred_by", "INTEGER DEFAULT 0"),
        ("user", "monitor_count", "INTEGER DEFAULT 0"),
        ("user", "is_admin", "BOOLEAN DEFAULT FALSE"),
        ("user", "trial_ends_at", "TIMESTAMP"),
        ("brand", "track_id", "VARCHAR DEFAULT ''"),
        ("brand", "mode", "VARCHAR DEFAULT 'outbound'"),
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
