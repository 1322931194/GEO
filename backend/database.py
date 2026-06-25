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
        "name": "基础版", "price_cny": 299, "brands": 2,
        "questions": 50, "samples": 1, "platforms": 4, "freq": "weekly",
        "monitor_limit": 4,   # 每月4次
    },
    "pro": {
        "name": "专业版", "price_cny": 899, "brands": 5,
        "questions": 150, "samples": 2, "platforms": 4, "freq": "daily",
        "monitor_limit": 999,  # 不限
    },
    "business": {
        "name": "企业版", "price_cny": 2999, "brands": 10,
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


def get_session():
    with Session(engine) as session:
        yield session
