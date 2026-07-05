"""
GEO 雷达 - 主 API 服务
=======================
FastAPI 应用。提供注册/登录、品牌管理、监测、报告、内容生成等接口。
带 JWT 鉴权和套餐配额控制 —— 这是多商家 SaaS 收钱的基础。

启动:  uvicorn main:app --host 0.0.0.0 --port 8000
文档:  启动后访问 http://localhost:8000/docs 有自动生成的 API 文档
"""

import os
import json
import hashlib
import hmac
import secrets
import asyncio
import time
try:
    import httpx
except ImportError:
    httpx = None  # 懒加载兜底，缺失不影响主服务启动
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select
import jwt

from database import (engine, init_db, get_session, PLANS,
                      User, Brand, Report, GeneratedContent, AIVisit, IndustrySample, Referral, KnowledgeItem, Order, ApiCallLog, TrackEvent, IndexTrack)
from services.monitor import run_monitoring, PLATFORMS, estimate_cost, check_all_keys
from services.generator import generate_questions, generate_content, extract_brand_keywords
from services.knowledge import build_knowledge_base
from services.optimizer import diagnose_score, build_action_plan, compare_reports, estimate_monthly_loss
from services.keyword_opportunity import analyze_keyword_opportunities

import logging as _logging
_sec_logger = _logging.getLogger("geo.security")

# JWT 密钥：生产环境必须通过环境变量配置，否则每次重启会让所有用户登出
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    _sec_logger.warning(
        "⚠️ 未配置 JWT_SECRET 环境变量！当前使用临时密钥，"
        "服务重启后所有用户将被强制登出。生产环境请务必在 Render 配置 JWT_SECRET。"
    )

app = FastAPI(title="GEO 雷达 API", version="1.0.0")

# CORS：限制允许的来源，防止恶意网站盗用用户 token 调用 API。
# 通过 ALLOWED_ORIGINS 环境变量配置（逗号分隔），未配置则用默认白名单。
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if _allowed_origins_env:
    _allowed_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
else:
    # 默认白名单：你的线上域名 + 本地开发
    _allowed_origins = [
        "https://geo-radar.onrender.com",
        "https://jianwei.uno",
        "http://localhost:8000",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

from services.call_tracker import drain as _drain_calls, cost_of as _cost_of

def _flush_call_logs():
    """把 tracker 缓冲区的调用记录聚合后写入数据库。
    解决'钱花了不知道花哪'——所有 AI 调用统一落库。"""
    items = _drain_calls()
    if not items:
        return
    # 按 (platform, scene) 聚合
    agg = {}
    for platform, scene, ok in items:
        k = (platform, scene)
        if k not in agg:
            agg[k] = {"calls": 0, "success": 0, "failed": 0}
        agg[k]["calls"] += 1
        if ok:
            agg[k]["success"] += 1
        else:
            agg[k]["failed"] += 1
    try:
        with Session(engine) as s:
            for (platform, scene), st in agg.items():
                s.add(ApiCallLog(
                    platform=platform, scene=scene,
                    calls=st["calls"], success=st["success"], failed=st["failed"],
                    est_cost=round(st["calls"] * _cost_of(platform), 4),
                ))
            s.commit()
    except Exception:
        pass  # 记账失败绝不影响主流程

@app.middleware("http")
async def _track_middleware(request, call_next):
    response = await call_next(request)
    # 请求结束后异步落库调用记录
    try:
        _flush_call_logs()
    except Exception:
        pass
    return response


@app.on_event("startup")
def _startup():
    init_db()


# ----------------------------- 鉴权工具 -----------------------------

def _hash_pw(pw: str, salt: str = "") -> str:
    """
    加盐哈希。salt为空时退回旧版SHA256（兼容老用户）。
    新用户用 salt$hash 格式存储。
    """
    if not salt:
        return hashlib.sha256(pw.encode()).hexdigest()
    # PBKDF2加盐，10万次迭代
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return dk.hex()


def _make_pw_hash(pw: str) -> str:
    """生成新密码存储串：salt$hash"""
    salt = secrets.token_hex(16)
    return f"{salt}${_hash_pw(pw, salt)}"


def _verify_pw(pw: str, stored: str) -> bool:
    """
    校验密码。兼容两种格式：
    - 新版 salt$hash（加盐）
    - 旧版 纯SHA256（无$）
    """
    if "$" in stored:
        salt, real_hash = stored.split("$", 1)
        return hmac.compare_digest(_hash_pw(pw, salt), real_hash)
    else:
        # 旧版SHA256
        return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)


# ----------------------------- 限流器 -----------------------------
# 内存版限流，防止接口被恶意刷爆（烧API费用/灌数据库）
_rate_buckets = defaultdict(list)

def _rate_limit(key: str, max_calls: int, window_sec: int):
    """
    简单滑动窗口限流。
    key: 限流标识（如 "simulate:1.2.3.4"）
    max_calls: 窗口内最多调用次数
    window_sec: 窗口秒数
    超限抛 429。
    """
    now = time.time()
    bucket = _rate_buckets[key]
    # 清理过期记录
    cutoff = now - window_sec
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= max_calls:
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    bucket.append(now)


def _client_ip(request: Request) -> str:
    """获取客户端IP（兼容Render代理）"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------- 全局每日监测熔断（保命措施）-------------------
# 防止极端情况下（限流被绕过/大规模攻击）API费用失控。
# 全站每日监测总数超过上限，直接停止，保护你的钱包。
_daily_monitor_count = {"date": "", "count": 0}

def _global_daily_guard():
    """全站每日监测熔断。超过 MAX_DAILY_MONITORS 当天停止监测。"""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if _daily_monitor_count["date"] != today:
        _daily_monitor_count["date"] = today
        _daily_monitor_count["count"] = 0
    max_daily = int(os.getenv("MAX_DAILY_MONITORS", "500"))  # 默认每天最多500次全站监测
    if _daily_monitor_count["count"] >= max_daily:
        raise HTTPException(503, "今日监测量已达上限，请明天再试（系统保护）")
    _daily_monitor_count["count"] += 1


def _jload(s, default=None):
    """安全解析JSON：数据异常时返回默认值，不抛错导致接口500"""
    if default is None:
        default = {}
    try:
        return json.loads(s) if s else default
    except (json.JSONDecodeError, TypeError):
        return default


# ----------------------------- 分销 -----------------------------
# 各套餐佣金比例（35%分润）
COMMISSION_RATE = 0.35
# 套餐价格（用于算佣金）
PLAN_PRICES = {
    "single": 99,
    "monthly": 599,
    # custom 定制版：价格面议，后台手动开通，不走线上支付
    # 以下为旧套餐价格（仅兼容历史订单回调）
    "starter": 1980,
    "pro": 3980,
    "business": 9800,
    "starter_trial": 39.9,
}

def _gen_invite_code() -> str:
    """生成6位邀请码"""
    import string
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(chars) for _ in range(6))


def _make_token(user_id: int, token_version: int = 0) -> str:
    payload = {
        "uid": user_id,
        "tv": token_version,   # token版本号，改密码时旧token失效
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(authorization: str = Header(None),
                 session: Session = Depends(get_session)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "登录已过期,请重新登录")
    user = session.get(User, payload["uid"])
    if not user:
        raise HTTPException(401, "用户不存在")
    # token版本校验：改密码后旧token的tv会对不上，强制失效（防token被盗后长期有效）
    if payload.get("tv", 0) != getattr(user, "token_version", 0):
        raise HTTPException(401, "登录状态已失效，请重新登录")
    return user


def plan_of(user: User) -> dict:
    return PLANS.get(user.plan, PLANS["trial"])


# 一次性/临时邮箱域名黑名单（垃圾注册最常用，拦掉它们零摩擦防滥用）
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "getnada.com",
    "trashmail.com", "maildrop.cc", "fakeinbox.com", "sharklasers.com",
    "guerrillamailblock.com", "dispostable.com", "mailnesia.com", "mintemail.com",
    "tempinbox.com", "spamgourmet.com", "mytemp.email", "tempmailo.com",
    "33mail.com", "emailondeck.com", "mohmal.com", "linshiyouxiang.net",
    "0-mail.com", "1secmail.com", "mail-temp.com", "burnermail.io",
}


# ----------------------------- 请求模型 -----------------------------

class RegisterReq(BaseModel):
    email: str
    password: str
    invite_code: str = ""   # 邀请码（可选，分销用）
    email_code: str = ""    # 邮箱验证码（仅启用邮箱验证时需要）

class BrandReq(BaseModel):
    name: str
    website: str = ""
    industry: str = ""
    product: str = ""
    target_market: str = "海外"
    mode: str = "outbound"   # outbound=出海模式  domestic=国内模式
    competitors: str = ""

class GenContentReq(BaseModel):
    brand_id: int
    gap_question: str
    content_type: str = "website"


# ----------------------------- 账号接口 -----------------------------

class SendCodeReq(BaseModel):
    email: str

@app.post("/api/send-email-code")
def send_email_code(req: SendCodeReq, request: Request):
    """获取邮箱验证码（仅当启用邮箱验证时有效）。"""
    from services.email_verify import is_email_verify_enabled, send_code
    if not is_email_verify_enabled():
        return {"ok": True, "enabled": False}  # 未启用，前端无需走验证码
    # 限流：同IP每小时最多10次
    _rate_limit(f"sendcode:{_client_ip(request)}", max_calls=10, window_sec=3600)
    email = req.email.strip().lower()
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "邮箱格式不正确")
    ok, msg = send_code(email)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "enabled": True, "dev_mode": msg == "dev"}


@app.get("/api/auth-config")
def auth_config():
    """前端查询是否需要邮箱验证（决定要不要显示验证码输入框）。"""
    from services.email_verify import is_email_verify_enabled
    return {"require_email_verify": is_email_verify_enabled()}


@app.post("/api/register")
def register(req: RegisterReq, request: Request, session: Session = Depends(get_session)):
    # 限流：同一IP每小时最多注册5个账号，防批量注册
    _rate_limit(f"register:{_client_ip(request)}", max_calls=5, window_sec=3600)
    # 邮箱标准化：去空格、转小写，避免后续登录因大小写/空格不匹配
    email = req.email.strip().lower()
    # 邮箱格式校验（基本正则）
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "邮箱格式不正确")
    # 拦截一次性/临时邮箱（垃圾注册最常用），真实用户无感
    domain = email.split("@")[-1]
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        raise HTTPException(400, "请使用常用邮箱注册（不支持临时邮箱）")
    # 密码强度：至少8位，且不能是纯数字或纯字母（防弱密码）
    pw = req.password
    if len(pw) < 8:
        raise HTTPException(400, "密码至少需要8位")
    if pw.isdigit() or pw.isalpha():
        raise HTTPException(400, "密码需包含字母和数字，更安全")
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(400, "该邮箱已注册")

    # 邮箱验证码校验（仅当 REQUIRE_EMAIL_VERIFY=true 时启用，默认关闭不影响转化）
    from services.email_verify import is_email_verify_enabled, verify_code
    if is_email_verify_enabled():
        if not verify_code(email, req.email_code):
            raise HTTPException(400, "验证码错误或已过期，请重新获取")

    # 处理邀请码：找到推荐人
    referrer = None
    if req.invite_code:
        referrer = session.exec(
            select(User).where(User.invite_code == req.invite_code.strip())
        ).first()

    user = User(
        email=email, password_hash=_make_pw_hash(req.password),
        plan="trial", trial_ends_at=datetime.utcnow() + timedelta(days=7),
        invite_code=_gen_invite_code(),
        referred_by=referrer.id if referrer else 0,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # 记录分销关系
    if referrer:
        ref = Referral(
            referrer_id=referrer.id,
            referred_user_id=user.id,
            referred_email=email,
            status="registered",
        )
        session.add(ref)
        session.commit()

    return {"token": _make_token(user.id, getattr(user, "token_version", 0)), "plan": user.plan,
            "plan_info": plan_of(user)}


@app.post("/api/login")
def login(req: RegisterReq, request: Request, session: Session = Depends(get_session)):
    # 限流：同一IP每5分钟最多10次登录尝试，防暴力破解
    _rate_limit(f"login:{_client_ip(request)}", max_calls=10, window_sec=300)
    # 邮箱标准化，与注册保持一致
    email = req.email.strip().lower()
    # 同账号失败锁定：单个邮箱5分钟内最多8次尝试，防撞库（针对特定账号的暴力破解）
    _rate_limit(f"login_acct:{email}", max_calls=8, window_sec=300)
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not _verify_pw(req.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    # 旧版SHA256密码，登录成功后自动升级为加盐版
    if "$" not in user.password_hash:
        user.password_hash = _make_pw_hash(req.password)
        session.add(user)
        session.commit()
    return {"token": _make_token(user.id, getattr(user, "token_version", 0)), "plan": user.plan,
            "plan_info": plan_of(user)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    plan = plan_of(user)
    monitor_limit = plan.get("monitor_limit", 999)
    used = getattr(user, "monitor_count", 0) or 0
    # 用量透明：让商家清楚扣费逻辑和剩余额度
    if monitor_limit >= 999:
        remaining_text = "不限次"
        remaining = -1
    else:
        remaining = max(0, monitor_limit - used)
        remaining_text = f"{remaining} 次"
    return {
        "email": user.email, "plan": user.plan, "plan_info": plan,
        "trial_ends_at": user.trial_ends_at,
        "usage": {
            "monitor_used": used,              # 已用监测次数
            "monitor_limit": monitor_limit,    # 套餐总次数
            "monitor_remaining": remaining,    # 剩余次数（-1=不限）
            "remaining_text": remaining_text,
            "samples_per_question": plan.get("samples", 1),  # 每题采样次数
            "billing_note": "计费方式：按「完整监测」次数计。一次监测 = 用问题集跑一遍所有已配置的AI平台。内容生成、知识库提取不单独计费。",
        },
    }


# ----------------------------- 品牌接口 -----------------------------

@app.post("/api/brands")
async def create_brand(req: BrandReq, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    # 配额检查
    count = len(session.exec(select(Brand).where(Brand.user_id == user.id)).all())
    if count >= plan_of(user)["brands"]:
        raise HTTPException(403,
            f"当前套餐最多 {plan_of(user)['brands']} 个品牌,请升级套餐")

    # 自动建知识库(零上传)
    facts = ""
    if req.website:
        kb = await build_knowledge_base(req.website)
        facts = kb.get("brand_facts", "")

    brand = Brand(
        user_id=user.id, name=req.name, website=req.website,
        industry=req.industry, product=req.product,
        target_market=req.target_market,
        mode=req.mode,
        competitors=req.competitors,
        brand_facts=facts,
    )
    session.add(brand)
    session.commit()
    session.refresh(brand)
    return {"id": brand.id, "name": brand.name, "facts_captured": bool(facts)}


@app.get("/api/brands")
def list_brands(user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    brands = session.exec(select(Brand).where(Brand.user_id == user.id)).all()
    result = []
    for b in brands:
        # 查这个品牌有没有监测报告
        reports = session.exec(
            select(Report).where(Report.brand_id == b.id)
            .order_by(Report.generated_at.desc())
        ).all()
        latest = reports[0] if reports else None
        result.append({
            "id": b.id, "name": b.name, "industry": b.industry,
            "website": b.website, "mode": getattr(b, "mode", "outbound"),
            "has_questions": bool(json.loads(b.questions_json or "[]")),
            "report_count": len(reports),
            "latest_rate": round(latest.mention_rate, 1) if latest else None,
            "last_monitor": str(latest.generated_at)[:10] if latest else None,
        })
    return result


@app.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """删除品牌，连带删除其报告、内容、知识库、追踪记录"""
    brand = session.get(Brand, brand_id)
    if not brand or brand.user_id != user.id:
        raise HTTPException(404, "品牌不存在")
    # 删除关联数据（这些表都有 brand_id 字段）
    for model in (Report, GeneratedContent, KnowledgeItem):
        rows = session.exec(select(model).where(model.brand_id == brand_id)).all()
        for r in rows:
            session.delete(r)
    # AIVisit 用 track_id 关联（不是 brand_id）
    track_id = getattr(brand, "track_id", "")
    if track_id:
        visits = session.exec(select(AIVisit).where(AIVisit.track_id == track_id)).all()
        for v in visits:
            session.delete(v)
    session.delete(brand)
    session.commit()
    return {"message": "已删除", "brand_id": brand_id}


@app.get("/api/brands/{brand_id}/keywords")
async def brand_keywords(brand_id: int, refresh: bool = False,
                         user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """提取品牌关键词，让商家确认品牌特征理解是否正确，再生成问题。
    带缓存：同品牌已提取过的直接返回缓存，避免重复调用 AI 烧 token。
    refresh=true 可强制重新提取。"""
    brand = _owned_brand(brand_id, user, session)

    # 优先用缓存（除非强制刷新）
    if not refresh and getattr(brand, "keywords_cache", ""):
        cached = _jload(brand.keywords_cache, None)
        if cached and cached.get("keywords"):
            cached["_cached"] = True
            return cached

    result = await extract_brand_keywords(
        brand.name, brand.industry, brand.product, brand.brand_facts
    )
    # 提取成功才缓存（失败的默认值不缓存，下次还能重试）
    if result and result.get("keywords", {}).get("features"):
        try:
            brand.keywords_cache = json.dumps(result, ensure_ascii=False)
            session.add(brand)
            session.commit()
        except Exception:
            session.rollback()
    return result


@app.post("/api/brands/{brand_id}/questions")
async def gen_questions(brand_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    brand = _owned_brand(brand_id, user, session)
    limit = plan_of(user)["questions"]
    qs = await generate_questions(
        brand.name, brand.industry, brand.product,
        brand.target_market, count=min(50, limit),
        brand_facts=brand.brand_facts,
        mode=getattr(brand, "mode", "outbound"),
    )
    brand.questions_json = json.dumps(qs, ensure_ascii=False)
    session.add(brand)
    session.commit()
    return {"questions": qs, "count": len(qs)}


@app.post("/api/brands/{brand_id}/monitor")
async def monitor(brand_id: int, request: Request, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    # 防薅羊毛：IP级限流。即使有人批量注册账号，同一IP每小时最多10次监测，
    # 防止恶意刷爆 API token 费用。
    _rate_limit(f"monitor:{_client_ip(request)}", max_calls=10, window_sec=3600)
    # 保命：全站每日监测熔断，防止 API 费用失控
    _global_daily_guard()
    brand = _owned_brand(brand_id, user, session)
    q_list = json.loads(brand.questions_json or "[]")
    questions = [q["question"] for q in q_list]
    # 性能优化：体验版/免费版首次监测，限制问题数，让"30秒出结果"体验更快、转化更好。
    # 完整问题集留给专业版以上（深度监测）。
    plan_now = plan_of(user)
    # 只有免费版(price=0)才限速快跑；99单次版及以上都是付费用户，给完整体验
    _is_free = plan_now.get("price_cny", 0) == 0
    if _is_free:  # 仅免费版
        questions = questions[:6]   # 免费版首次只跑6个核心问题，确保30-40秒内出结果
        q_list = q_list[:6]
    # 问题→主题 映射（用于主题维度细分，对标 Goodie Segment by topic）
    q_to_topic = {}
    for q in q_list:
        topic = q.get("category", "") or "其他"
        # 简化主题名（去掉括号说明，取核心词）
        topic = topic.split("（")[0].split("(")[0].strip()
        q_to_topic[q["question"]] = topic
    if not questions:
        raise HTTPException(400, "请先生成问题集再开始监测")

    # 试用版监测次数限制
    plan = plan_of(user)
    monitor_limit = plan.get("monitor_limit", 999)
    user_count = getattr(user, "monitor_count", 0) or 0
    if monitor_limit == 0:
        raise HTTPException(403, "UPGRADE_REQUIRED")
    if 0 < monitor_limit <= user_count:
        raise HTTPException(403, "UPGRADE_REQUIRED")

    competitors = [c.strip() for c in brand.competitors.split(",") if c.strip()]
    report = await run_monitoring(
        brand.name, questions, competitors,
        samples_per_question=plan["samples"],
        mode=getattr(brand, "mode", "outbound"),
        max_platforms=(3 if _is_free else 5),    # 免费版限3平台确保快；付费版5平台
        skip_resample=_is_free,                   # 免费版跳过补采样，首次更快
    )

    # 监测全失败保护：如果一条 AI 回答都没成功，说明 API 配置/网络有问题，
    # 不存假的全0报告（会误导用户以为"AI真的没推你"）
    if report.answered_queries == 0:
        # 收集每个平台的真实错误（用于诊断）
        plat_errs = {}
        for r in (report.raw_results or []):
            if r.get("error"):
                lbl = r.get("platform_label") or r.get("platform") or "?"
                if lbl not in plat_errs:
                    plat_errs[lbl] = str(r.get("error"))[:120]
        err_text = " ".join(plat_errs.values()).lower()
        if "429" in err_text or "rate" in err_text or "frequent" in err_text or "频繁" in err_text:
            reason = "AI 平台暂时限流（请求过于频繁），请等 1-2 分钟后重试。"
        elif "timeout" in err_text or "超时" in err_text or "timed out" in err_text:
            reason = "AI 平台响应超时，请稍等片刻重试一次。"
        elif "key" in err_text or "auth" in err_text or "401" in err_text or "403" in err_text or "api_key" in err_text:
            reason = "AI 平台密钥验证失败，请在管理后台「API 密钥自检」检查。"
        elif "json" in err_text or "parse" in err_text:
            reason = "AI 返回格式异常，请重试一次。"
        else:
            reason = "AI 监测未能获取数据，请稍等重试。"
        # 把每个平台的具体错误附在后面，方便排查
        detail_parts = [f"{k}：{v}" for k, v in plat_errs.items()]
        diag = "；".join(detail_parts) if detail_parts else "无具体错误信息（可能所有平台都未被调用）"
        raise HTTPException(503, f"{reason} 本次不计入监测次数。【诊断信息】{diag}")

    # 更新监测次数
    user.monitor_count = user_count + 1
    session.add(user)

    # ===== 异动预警（对标 Goodie 的 Catch shifts）=====
    # 对比上一次报告：提及率骤降 / 冒出新竞品 / 竞品份额激增 → 主动预警
    alerts = []
    try:
        prev = session.exec(
            select(Report).where(Report.brand_id == brand.id)
            .order_by(Report.generated_at.desc())
        ).first()
        if prev:
            # ① 提及率变化
            prev_rate = prev.mention_rate or 0
            delta = round(report.mention_rate - prev_rate, 1)
            if delta <= -5:
                alerts.append({
                    "type": "down", "level": "danger",
                    "msg": f"提及率下降了 {abs(delta)} 个百分点（{prev_rate}% → {report.mention_rate}%），AI 推荐你的概率在降低，需尽快补内容。"
                })
            elif delta >= 5:
                alerts.append({
                    "type": "up", "level": "good",
                    "msg": f"提及率上升了 {delta} 个百分点（{prev_rate}% → {report.mention_rate}%），优化见效了，继续保持！"
                })
            # ② 新竞品出现
            try:
                prev_comps = set(json.loads(prev.competitor_share_json or "{}").keys())
                now_comps = set(report.competitor_share.keys())
                new_comps = now_comps - prev_comps
                if new_comps:
                    alerts.append({
                        "type": "new_competitor", "level": "warn",
                        "msg": f"出现了新的竞争对手：{('、'.join(list(new_comps)[:3]))}。AI 开始推荐它们，建议关注。"
                    })
                # ③ 竞品份额激增（某竞品涨了≥10个点）
                prev_share = json.loads(prev.competitor_share_json or "{}")
                for comp, share in report.competitor_share.items():
                    rise = share - prev_share.get(comp, 0)
                    if rise >= 10:
                        alerts.append({
                            "type": "competitor_surge", "level": "warn",
                            "msg": f"{comp} 的 AI 声量份额激增了 {round(rise)} 个百分点，正在快速抢占你的市场。"
                        })
                        break
            except Exception:
                pass
    except Exception:
        pass
    report.alerts = alerts  # 挂到报告上返回前端

    # ===== 主题维度细分（对标 Goodie Segment by topic）=====
    # 把问题按主题聚合，算每个主题的提及率，让报告从"平台级"升到"主题级"
    try:
        topic_stats = {}
        for r in (report.raw_results or []):
            if r.get("error"):
                continue
            q = r.get("question", "")
            topic = q_to_topic.get(q, "其他")
            if topic not in topic_stats:
                topic_stats[topic] = {"total": 0, "mentioned": 0}
            topic_stats[topic]["total"] += 1
            if r.get("brand_mentioned"):
                topic_stats[topic]["mentioned"] += 1
        by_topic = []
        for topic, st in topic_stats.items():
            if st["total"] > 0:
                by_topic.append({
                    "topic": topic,
                    "rate": round(100 * st["mentioned"] / st["total"], 1),
                    "total": st["total"],
                })
        by_topic.sort(key=lambda x: x["rate"])  # 表现最差的排前面（最该优化）
        report.by_topic = by_topic
    except Exception:
        report.by_topic = []

    rec = Report(
        brand_id=brand.id, mention_rate=report.mention_rate,
        competitor_share_json=json.dumps(report.competitor_share, ensure_ascii=False),
        platform_breakdown_json=json.dumps(report.platform_breakdown, ensure_ascii=False),
        gaps_json=json.dumps(report.gaps, ensure_ascii=False),
        source_count=report.source_count, sample_note=report.sample_note,
        full_json=json.dumps(report.__dict__, ensure_ascii=False, default=str),
    )
    session.add(rec)

    # 存行业匿名样本（数据护城河，默默积累）
    try:
        _save_industry_sample(brand, report.mention_rate, report.source_count, session)
    except Exception:
        pass  # 样本采集失败不影响主流程

    # 记录 API 调用日志（用于管理后台监控真实消耗）
    try:
        raw = report.raw_results or []
        # 按平台统计成功/失败
        plat_stats = {}
        for r in raw:
            pid = r.get("platform", "unknown")
            if pid not in plat_stats:
                plat_stats[pid] = {"calls": 0, "success": 0, "failed": 0}
            plat_stats[pid]["calls"] += 1
            if r.get("error"):
                plat_stats[pid]["failed"] += 1
            else:
                plat_stats[pid]["success"] += 1
        # 成本等级映射（便宜平台约 ¥0.003/次，贵的约 ¥0.03/次）
        cost_per_call = {"deepseek": 0.003, "qwen": 0.003, "doubao": 0.003,
                         "kimi": 0.003, "wenxin": 0.003,
                         "chatgpt": 0.03, "gemini": 0.01, "claude": 0.03, "perplexity": 0.01}
        for pid, st in plat_stats.items():
            session.add(ApiCallLog(
                user_id=user.id, brand_id=brand.id, platform=pid,
                calls=st["calls"], success=st["success"], failed=st["failed"],
                est_cost=round(st["calls"] * cost_per_call.get(pid, 0.01), 4),
            ))
    except Exception:
        pass  # 日志记录失败不影响主流程

    session.commit()
    session.refresh(rec)
    return report.__dict__


@app.get("/api/brands/{brand_id}/reports")
def reports(brand_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    _owned_brand(brand_id, user, session)
    recs = session.exec(select(Report).where(Report.brand_id == brand_id)
                        .order_by(Report.generated_at.desc())).all()
    result = []
    for r in recs:
        item = {"id": r.id, "generated_at": r.generated_at,
                "mention_rate": r.mention_rate,
                "gaps": _jload(r.gaps_json, []),
                "platform_breakdown": _jload(r.platform_breakdown_json, []),
                "competitor_share": _jload(r.competitor_share_json, []),
                "source_count": r.source_count,
                "sample_note": r.sample_note}
        # 附带完整报告数据，供前端恢复展示
        try:
            item["full"] = _jload(r.full_json, {})
        except Exception:
            item["full"] = None
        result.append(item)
    return result


@app.get("/api/brands/{brand_id}/history")
def history_chart(brand_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """
    折线图数据：返回近30天所有监测的提及率变化，用于前端画"历史战报"折线图。
    这是让商家看到"付了钱之后在上涨"的核心数据，也是续费的最强理由。
    """
    _owned_brand(brand_id, user, session)
    recs = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.asc())
    ).all()

    points = []
    for r in recs:
        pb = json.loads(r.platform_breakdown_json or "{}")
        points.append({
            "date": r.generated_at.strftime("%m/%d"),
            "mention_rate": r.mention_rate,
            "source_count": r.source_count,
            "platforms": pb,
        })

    # 计算趋势：首次 vs 最新
    trend = None
    if len(points) >= 2:
        first = points[0]["mention_rate"]
        last = points[-1]["mention_rate"]
        delta = round(last - first, 1)
        trend = {
            "delta": delta,
            "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "summary": f"相比首次监测，AI 推荐率{'上升' if delta>0 else '下降' if delta<0 else '持平'}了 {abs(delta)} 个百分点"
        }

    return {
        "points": points,
        "trend": trend,
        "total_monitors": len(points),
    }


@app.get("/api/brands/{brand_id}/improve-plan")
def improve_plan(brand_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """
    提升方案:基于最新一次监测,给出评分拆解 + GEO 任务清单。
    这是把"监测"变成"提升"的核心接口 —— 告诉商家具体做什么。
    """
    brand = _owned_brand(brand_id, user, session)
    latest = session.exec(select(Report).where(Report.brand_id == brand_id)
                          .order_by(Report.generated_at.desc())).first()
    if not latest:
        raise HTTPException(400, "请先完成一次监测,再查看提升方案")

    report = _jload(latest.full_json, {})
    diagnosis = diagnose_score(report)
    tasks = build_action_plan(report, diagnosis, brand.name)
    return {"diagnosis": diagnosis, "tasks": tasks,
            "based_on_report_at": latest.generated_at}


@app.get("/api/brands/{brand_id}/progress")
def progress(brand_id: int, user: User = Depends(current_user),
             session: Session = Depends(get_session)):
    """
    复测对比:对比最近两次监测,生成"优化是否见效"的真实证据。
    这是续费理由,也是最好的营销素材。
    """
    _owned_brand(brand_id, user, session)
    recs = session.exec(select(Report).where(Report.brand_id == brand_id)
                        .order_by(Report.generated_at.desc())).all()
    if len(recs) < 2:
        return {"has_comparison": False,
                "message": "完成第二次监测后,这里会显示你的提升对比。"}
    after = _jload(recs[0].full_json, {})
    before = _jload(recs[1].full_json, {})
    result = compare_reports(before, after)
    result["has_comparison"] = True
    result["before_at"] = recs[1].generated_at
    result["after_at"] = recs[0].generated_at
    return result


@app.get("/api/brands/{brand_id}/chat-snapshots")
def chat_snapshots(brand_id: int, demo: bool = False,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """
    全景对话快照：返回'AI真实推荐了你'的对话现场。
    这是活体证据——真实时间、真实节点、真实提问、AI真实回答。
    """
    brand = _owned_brand(brand_id, user, session)

    PLAT_LABELS = {p: PLATFORMS[p].get("label", p) for p in PLATFORMS}

    if demo:
        return {
            "demo": True, "brand_name": brand.name,
            "snapshots": [{
                "platform": "DeepSeek", "node": "上海",
                "queried_at": "2026-06-27 10:26:13",
                "question": f"推荐几个靠谱的{brand.industry or '相关'}品牌",
                "answer": f"根据口碑和专业度，推荐几个值得考虑的品牌：\n\n1. {brand.name} —— 在{brand.industry or '行业'}领域口碑良好，服务专业；\n2. 同类品牌B；\n3. 同类品牌C。\n\n建议结合自身需求进一步对比。",
                "mentioned": True,
            }, {
                "platform": "豆包", "node": "上海",
                "queried_at": "2026-06-27 10:26:41",
                "question": f"{brand.name}怎么样？",
                "answer": f"{brand.name}是{brand.industry or '该领域'}里口碑不错的选择，整体评价正面，可以考虑。",
                "mentioned": True,
            }],
        }

    latest = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).first()
    if not latest:
        raise HTTPException(404, "暂无监测数据")

    full = _jload(latest.full_json, {})
    raw = full.get("raw_results", [])
    brand_name = brand.name

    positive = []   # AI推荐了你
    negative = []   # AI没提你（负面/缺口的真实来源）
    for r in raw:
        if r.get("error") or not r.get("answer_text"):
            continue
        pid = r.get("platform", "")
        item = {
            "platform": PLAT_LABELS.get(pid, pid),
            "node": r.get("node", "上海"),
            "queried_at": r.get("queried_at", ""),
            "question": r.get("question", ""),
            "answer": r.get("answer_text", "")[:800],
            "competitors": r.get("competitors_mentioned", []),
        }
        if r.get("brand_mentioned"):
            item["mentioned"] = True
            positive.append(item)
        else:
            item["mentioned"] = False
            negative.append(item)

    return {
        "demo": False, "brand_name": brand_name,
        "snapshots": positive[:6],          # 兼容旧字段
        "positive": positive[:6],
        "negative": negative[:6],
        "total": len(positive),
        "negative_total": len(negative),
    }


@app.get("/api/brands/{brand_id}/health-radar")
def health_radar(brand_id: int, demo: bool = False,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """
    品牌健康度雷达图数据。
    6个维度，每个含正面推荐/客观提及/负面预警三色占比。
    demo=True 返回演示数据；否则基于真实监测报告计算。
    """
    brand = _owned_brand(brand_id, user, session)

    if demo:
        # 演示数据：制造视觉冲击的典型"待优化"画像
        return {
            "demo": True,
            "brand_name": brand.name,
            "dimensions": [
                {"label": "AI 推荐率", "positive": 15, "neutral": 25, "negative": 10, "max": 100},
                {"label": "内容覆盖", "positive": 20, "neutral": 30, "negative": 5, "max": 100},
                {"label": "竞品压制", "positive": 12, "neutral": 18, "negative": 0, "max": 100},
                {"label": "权威背书", "positive": 8, "neutral": 22, "negative": 0, "max": 100},
                {"label": "舆情安全", "positive": 30, "neutral": 40, "negative": 25, "max": 100},
                {"label": "信息准确", "positive": 35, "neutral": 35, "negative": 8, "max": 100},
            ],
            "summary": {"positive_rate": 18, "neutral_rate": 28, "negative_rate": 9},
        }

    # 真实数据：基于最新报告
    latest = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).first()
    if not latest:
        raise HTTPException(404, "暂无监测数据，请先完成一次监测")

    rate = latest.mention_rate
    full = _jload(latest.full_json, {})
    gaps = _jload(latest.gaps_json, [])
    platforms = _jload(latest.platform_breakdown_json, [])

    # 从真实数据推导各维度（正面=被推荐，客观=被提及未推荐，负面=缺口/未提及）
    gap_penalty = min(len(gaps) * 5, 40)
    plat_count = len(platforms) if platforms else 1
    plat_covered = sum(1 for p in platforms if isinstance(p, dict) and p.get("mentioned")) if platforms else 0
    coverage = round(100 * plat_covered / plat_count) if plat_count else 0

    dims = [
        {"label": "AI 推荐率", "positive": round(rate), "neutral": round(min(rate*0.6, 100-rate)), "negative": 0, "max": 100},
        {"label": "内容覆盖", "positive": coverage, "neutral": round(coverage*0.5), "negative": 0, "max": 100},
        {"label": "竞品压制", "positive": round(max(rate-10, 0)), "neutral": round(rate*0.4), "negative": 0, "max": 100},
        {"label": "权威背书", "positive": round(rate*0.5), "neutral": round(rate*0.6), "negative": 0, "max": 100},
        {"label": "舆情安全", "positive": round(100-gap_penalty), "neutral": round(gap_penalty*0.5), "negative": round(gap_penalty*0.3), "max": 100},
        {"label": "信息准确", "positive": round(min(rate+20, 90)), "neutral": round((100-rate)*0.3), "negative": 0, "max": 100},
    ]
    avg_pos = round(sum(d["positive"] for d in dims) / len(dims))
    avg_neu = round(sum(d["neutral"] for d in dims) / len(dims))
    avg_neg = round(sum(d["negative"] for d in dims) / len(dims))
    return {
        "demo": False,
        "brand_name": brand.name,
        "dimensions": dims,
        "summary": {"positive_rate": avg_pos, "neutral_rate": avg_neu, "negative_rate": avg_neg},
    }


@app.get("/api/brands/{brand_id}/exec-report")
def exec_report_data(brand_id: int, demo: bool = False,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """
    高管级汇报PDF所需数据。
    三页：竞品拦截对比、负面/缺口危机、流量挽回模型。
    demo=True 用演示数据。
    """
    brand = _owned_brand(brand_id, user, session)

    if demo:
        return {
            "demo": True,
            "brand_name": brand.name,
            "industry": brand.industry or "你的行业",
            "intercept": {
                "your_rate": 0,
                "competitors": [
                    {"name": "行业领先竞品 A", "rate": 82},
                    {"name": "竞品 B", "rate": 68},
                    {"name": "竞品 C", "rate": 55},
                ],
            },
            "negatives": [
                {"platform": "ChatGPT", "question": "推荐这个行业靠谱的品牌", "answer_snippet": "推荐了 A、B、C 三个竞品，未提及你的品牌。客户直接获得了竞品信息。"},
                {"platform": "DeepSeek", "question": "哪个品牌质量更好", "answer_snippet": "重点介绍了竞品 A 的优势，你的品牌完全没有出现在回答中。"},
            ],
            "recovery": {
                "current_monthly_loss": 8400,
                "recoverable": 6200,
                "timeline_months": 3,
                "projection": [
                    {"month": "现状", "rate": 0},
                    {"month": "第1月", "rate": 12},
                    {"month": "第2月", "rate": 28},
                    {"month": "第3月", "rate": 45},
                ],
            },
        }

    latest = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).first()
    if not latest:
        raise HTTPException(404, "暂无监测数据，请先完成一次监测")

    rate = latest.mention_rate
    comp_share = _jload(latest.competitor_share_json, [])
    gaps = _jload(latest.gaps_json, [])

    competitors = []
    for c in comp_share[:3]:
        if isinstance(c, dict):
            competitors.append({"name": c.get("name", "竞品"), "rate": round(c.get("share", 0))})
    if not competitors:
        competitors = [{"name": "行业竞品", "rate": round(max(rate+30, 50))}]

    # 负面/缺口（真实缺口问题）
    negatives = []
    for g in gaps[:3]:
        q = g if isinstance(g, str) else g.get("question", "")
        if q:
            negatives.append({"platform": "AI 平台", "question": q,
                              "answer_snippet": "AI 在回答这个问题时未推荐你的品牌，客户获得的是其他品牌的信息。"})

    # 流量挽回模型（基于当前提及率的合理预估）
    base = round(rate)
    return {
        "demo": False,
        "brand_name": brand.name,
        "industry": brand.industry or "你的行业",
        "intercept": {"your_rate": round(rate), "competitors": competitors},
        "negatives": negatives,
        "recovery": {
            "current_monthly_loss": None,
            "recoverable": None,
            "timeline_months": 3,
            "projection": [
                {"month": "现状", "rate": base},
                {"month": "第1月", "rate": min(base+12, 100)},
                {"month": "第2月", "rate": min(base+28, 100)},
                {"month": "第3月", "rate": min(base+45, 100)},
            ],
        },
    }


@app.get("/api/brands/{brand_id}/dashboard")
def growth_dashboard(brand_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """
    增长看板：商家每周必看的核心页面。
    汇总该品牌所有历史监测，生成趋势数据、环比变化、各平台表现、目标进度。
    这是让商家持续回来的核心钩子——看到自己的提及率涨没涨。
    """
    brand = _owned_brand(brand_id, user, session)
    recs = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.asc())
    ).all()

    if not recs:
        return {"has_data": False, "message": "完成第一次监测后，这里会显示你的增长看板"}

    # 趋势数据（按时间正序）
    trend = []
    for r in recs:
        trend.append({
            "date": r.generated_at.strftime("%m-%d") if hasattr(r.generated_at, "strftime") else str(r.generated_at)[:10],
            "full_date": str(r.generated_at)[:19],
            "mention_rate": round(r.mention_rate, 1),
            "source_count": r.source_count,
        })

    latest = recs[-1]
    first = recs[0]
    prev = recs[-2] if len(recs) >= 2 else None

    # 环比变化（相比上一次）
    current_rate = round(latest.mention_rate, 1)
    prev_rate = round(prev.mention_rate, 1) if prev else None
    change_vs_prev = round(current_rate - prev_rate, 1) if prev_rate is not None else None

    # 累计变化（相比第一次）
    first_rate = round(first.mention_rate, 1)
    change_vs_first = round(current_rate - first_rate, 1)

    # 各平台当前表现
    platform_now = {}
    try:
        platform_now = json.loads(latest.platform_breakdown_json or "{}")
    except Exception:
        platform_now = {}

    # 各平台变化（相比上次）
    platform_change = {}
    if prev:
        try:
            platform_prev = json.loads(prev.platform_breakdown_json or "{}")
            for pf, rate in platform_now.items():
                old = platform_prev.get(pf, 0)
                platform_change[pf] = round(rate - old, 1)
        except Exception:
            pass

    # 目标进度（默认目标提及率60%）
    target = 60
    progress_pct = min(100, round(current_rate / target * 100)) if target else 0

    # 增长状态判断
    if change_vs_prev is None:
        status = "first"
        status_text = "这是你的第一次监测，完成优化后再次监测即可看到增长曲线"
    elif change_vs_prev > 0:
        status = "up"
        status_text = f"📈 相比上次提升了 {change_vs_prev} 个百分点，优化见效了！"
    elif change_vs_prev < 0:
        status = "down"
        status_text = f"📉 相比上次下降了 {abs(change_vs_prev)} 个百分点，竞品可能在加速，需要加强优化"
    else:
        status = "flat"
        status_text = "数据与上次持平，GEO 优化通常需要 2-4 周显现，继续保持"

    return {
        "has_data": True,
        "brand_name": brand.name,
        "monitor_count": len(recs),
        "current_rate": current_rate,
        "prev_rate": prev_rate,
        "change_vs_prev": change_vs_prev,
        "first_rate": first_rate,
        "change_vs_first": change_vs_first,
        "source_count": latest.source_count,
        "trend": trend,
        "platform_now": platform_now,
        "platform_change": platform_change,
        "target": target,
        "progress_pct": progress_pct,
        "status": status,
        "status_text": status_text,
        "last_monitor_date": str(latest.generated_at)[:19],
    }


# ----------------------------- AI访客追踪 -----------------------------

import re as _re_track

# AI平台来源识别规则
AI_SOURCE_PATTERNS = {
    "chatgpt": ["openai.com", "chatgpt.com", "chat.openai"],
    "perplexity": ["perplexity.ai"],
    "gemini": ["gemini.google", "bard.google"],
    "copilot": ["copilot.microsoft", "bing.com/chat"],
    "claude": ["claude.ai"],
    "deepseek": ["deepseek.com", "chat.deepseek"],
    "doubao": ["doubao.com"],
    "kimi": ["kimi.moonshot", "kimi.ai"],
    "tongyi": ["tongyi.aliyun", "qianwen"],
    "wenxin": ["yiyan.baidu", "wenxin"],
}

def _detect_ai_source(referrer: str) -> str:
    """从referrer识别是否来自AI平台"""
    if not referrer:
        return ""
    ref_low = referrer.lower()
    for source, patterns in AI_SOURCE_PATTERNS.items():
        if any(p in ref_low for p in patterns):
            return source
    return ""


def _get_or_create_track_id(brand: Brand, session: Session) -> str:
    """获取或生成品牌的追踪码"""
    if not brand.track_id:
        brand.track_id = "geo_" + secrets.token_hex(8)
        session.add(brand)
        session.commit()
        session.refresh(brand)
    return brand.track_id


@app.get("/api/brands/{brand_id}/tracking-code")
def get_tracking_code(brand_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """
    获取商家官网要嵌入的追踪代码。
    商家把这段JS贴到自己官网，就能追踪从AI来的访客。
    """
    brand = _owned_brand(brand_id, user, session)
    track_id = _get_or_create_track_id(brand, session)
    # 服务器地址
    base_url = os.getenv("PUBLIC_URL", "https://geo-radar.onrender.com")
    tracking_js = f"""<!-- GEO雷达 AI访客追踪代码 -->
<script>
(function(){{
  try{{
    var ref = document.referrer || '';
    if(!ref) return;
    var img = new Image();
    img.src = '{base_url}/api/track?tid={track_id}'
      + '&ref=' + encodeURIComponent(ref)
      + '&page=' + encodeURIComponent(location.href);
  }}catch(e){{}}
}})();
</script>
<!-- GEO雷达追踪代码结束 -->"""
    return {
        "track_id": track_id,
        "tracking_code": tracking_js,
        "install_guide": "把这段代码粘贴到你官网每个页面的 </body> 标签前即可。安装后，从 ChatGPT、Perplexity 等 AI 点链接进来的访客就会被记录。",
    }


@app.get("/api/track")
def track_visit(tid: str, request: Request, ref: str = "", page: str = "",
                user_agent: str = Header(None),
                session: Session = Depends(get_session)):
    """
    接收追踪数据（公开接口，无需登录）。
    商家官网的追踪代码会调用这个接口上报访客来源。
    只记录来自AI平台的访客。
    """
    if not tid:
        return {"ok": False}
    # 限流：同一IP每分钟最多30次上报，防恶意刷假访客污染数据
    try:
        _rate_limit(f"track:{_client_ip(request)}", max_calls=30, window_sec=60)
    except HTTPException:
        # 超限静默丢弃，不报错（避免影响商家页面）
        return {"ok": True, "tracked": False}
    # 识别是否AI来源
    source = _detect_ai_source(ref)
    if not source:
        # 不是AI来源，不记录
        return {"ok": True, "tracked": False}

    visit = AIVisit(
        track_id=tid,
        source=source,
        referrer=ref[:500],
        landing_page=page[:500],
        user_agent=(user_agent or "")[:300],
    )
    session.add(visit)
    session.commit()
    return {"ok": True, "tracked": True}


class ConversionReq(BaseModel):
    event_type: str = "lead"   # lead/consult/order
    value: float = 0.0
    source: str = ""
    note: str = ""

@app.post("/api/track-conversion")
def track_conversion(tid: str, req: ConversionReq, request: Request,
                     session: Session = Depends(get_session)):
    """上报转化事件（公开接口）。商家在留资/下单页埋点调用，形成 ROI 归因。"""
    if not tid:
        return {"ok": False}
    try:
        _rate_limit(f"conv:{_client_ip(request)}", max_calls=30, window_sec=60)
    except HTTPException:
        return {"ok": True, "tracked": False}
    from database import Conversion
    conv = Conversion(
        track_id=tid, event_type=req.event_type[:20],
        value=max(0, req.value), source=req.source[:50], note=req.note[:200],
    )
    session.add(conv)
    session.commit()
    return {"ok": True, "tracked": True}


@app.post("/api/brands/{brand_id}/manual-conversion")
def manual_conversion(brand_id: int, req: ConversionReq,
                      user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """商家手动登记一笔转化（没埋点也能用，简单可行）。"""
    brand = _owned_brand(brand_id, user, session)
    tid = getattr(brand, "track_id", "") or _get_or_create_track_id(brand, session)
    from database import Conversion
    conv = Conversion(
        track_id=tid, event_type=req.event_type[:20],
        value=max(0, req.value), source=req.source[:50] or "manual", note=req.note[:200],
    )
    session.add(conv)
    session.commit()
    return {"ok": True}


@app.get("/api/brands/{brand_id}/roi")
def brand_roi(brand_id: int, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """ROI 归因看板：AI访客 → 转化 → 价值的完整链路。"""
    brand = _owned_brand(brand_id, user, session)
    tid = getattr(brand, "track_id", "")
    if not tid:
        return {"has_data": False, "visits": 0, "conversions": 0, "total_value": 0,
                "conversion_rate": 0, "by_source": {}, "note": "尚未安装追踪代码"}

    from database import Conversion
    visits = session.exec(select(AIVisit).where(AIVisit.track_id == tid)).all()
    convs = session.exec(select(Conversion).where(Conversion.track_id == tid)).all()

    total_value = round(sum(c.value for c in convs), 2)
    conv_rate = round(100 * len(convs) / len(visits), 1) if visits else 0

    # 按来源归因（哪个AI平台带来的转化最值钱）
    by_source = {}
    for v in visits:
        s = v.source or "other"
        by_source.setdefault(s, {"visits": 0, "conversions": 0, "value": 0.0})
        by_source[s]["visits"] += 1
    for c in convs:
        s = c.source or "other"
        by_source.setdefault(s, {"visits": 0, "conversions": 0, "value": 0.0})
        by_source[s]["conversions"] += 1
        by_source[s]["value"] += c.value
    for s in by_source:
        by_source[s]["value"] = round(by_source[s]["value"], 2)

    return {
        "has_data": len(visits) > 0 or len(convs) > 0,
        "visits": len(visits),
        "conversions": len(convs),
        "total_value": total_value,
        "conversion_rate": conv_rate,
        "by_source": by_source,
        "note": "ROI 归因基于追踪代码记录的真实 AI 访客与转化数据。",
    }


@app.get("/api/brands/{brand_id}/ai-traffic")
def ai_traffic_stats(brand_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """
    AI流量统计：商家看到从AI来的真实访客数据。
    这是从"虚指标"到"真客户"的质变——告诉商家AI到底带来了多少访客。
    """
    brand = _owned_brand(brand_id, user, session)
    if not brand.track_id:
        return {"has_tracking": False,
                "message": "还没有安装追踪代码。安装后即可看到从AI来的真实访客。"}

    visits = session.exec(
        select(AIVisit).where(AIVisit.track_id == brand.track_id)
        .order_by(AIVisit.visited_at.desc())
    ).all()

    if not visits:
        return {"has_tracking": True, "has_data": False,
                "track_id": brand.track_id,
                "message": "追踪代码已就绪，但还没有从AI来的访客。继续做GEO优化，让AI开始推荐你。"}

    # 按来源统计
    by_source = {}
    for v in visits:
        by_source[v.source] = by_source.get(v.source, 0) + 1

    # 按日期统计（近30天）
    from collections import defaultdict
    by_date = defaultdict(int)
    now = datetime.utcnow()
    for v in visits:
        days_ago = (now - v.visited_at).days
        if days_ago <= 30:
            date_key = v.visited_at.strftime("%m-%d")
            by_date[date_key] += 1

    # 近7天 vs 前7天对比
    last_7 = sum(1 for v in visits if (now - v.visited_at).days <= 7)
    prev_7 = sum(1 for v in visits if 7 < (now - v.visited_at).days <= 14)
    week_change = last_7 - prev_7

    source_labels = {
        "chatgpt": "ChatGPT", "perplexity": "Perplexity", "gemini": "Gemini",
        "copilot": "Copilot", "claude": "Claude", "deepseek": "DeepSeek",
        "doubao": "豆包", "kimi": "Kimi", "tongyi": "通义千问", "wenxin": "文心一言",
    }

    return {
        "has_tracking": True,
        "has_data": True,
        "total_visits": len(visits),
        "last_7_days": last_7,
        "prev_7_days": prev_7,
        "week_change": week_change,
        "by_source": [{"source": source_labels.get(k, k), "count": v}
                      for k, v in sorted(by_source.items(), key=lambda x: -x[1])],
        "by_date": [{"date": k, "count": v} for k, v in sorted(by_date.items())],
        "recent_visits": [{
            "source": source_labels.get(v.source, v.source),
            "landing_page": v.landing_page,
            "visited_at": str(v.visited_at)[:19],
        } for v in visits[:10]],
    }


# ----------------------------- 行业大盘（数据护城河） -----------------------------

INDUSTRY_KEYWORDS = {
    "茶饮/茶叶": ["茶", "tea", "莓茶", "养生茶"],
    "美妆护肤": ["美妆", "护肤", "化妆", "skincare", "cosmetic", "beauty", "面膜", "精华"],
    "服装鞋帽": ["服装", "衣服", "鞋", "帽", "clothing", "apparel", "fashion", "shoe"],
    "3C数码": ["数码", "3c", "电子", "手机", "电脑", "耳机", "充电", "charger", "electronic", "digital"],
    "家居家具": ["家居", "家具", "furniture", "home", "床", "沙发", "灯"],
    "食品饮料": ["食品", "零食", "饮料", "food", "snack", "drink", "咖啡", "coffee"],
    "母婴用品": ["母婴", "婴儿", "儿童", "baby", "kids", "玩具", "toy"],
    "户外运动": ["户外", "运动", "健身", "outdoor", "sport", "fitness", "露营", "camping"],
    "宠物用品": ["宠物", "pet", "猫", "狗", "dog", "cat"],
    "保健品": ["保健", "营养", "supplement", "vitamin", "膳食"],
    "电商/软件": ["电商", "跨境", "ecommerce", "saas", "软件", "software"],
    "餐饮": ["餐饮", "餐厅", "restaurant", "外卖"],
    "教育培训": ["教育", "培训", "课程", "education", "course", "training"],
}

def _normalize_industry(industry_raw: str) -> str:
    if not industry_raw:
        return "其他"
    low = industry_raw.lower()
    for std, keywords in INDUSTRY_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            return std
    return "其他"


def _save_industry_sample(brand: Brand, mention_rate: float,
                          source_count: int, session: Session):
    """存一条行业匿名样本。用品牌ID哈希去重，不存品牌名。"""
    industry_raw = brand.industry or brand.product or ""
    industry_std = _normalize_industry(industry_raw)
    brand_hash = hashlib.sha256(f"brand_{brand.id}".encode()).hexdigest()[:16]
    sample = IndustrySample(
        industry=industry_std,
        industry_raw=industry_raw[:100],
        mode=getattr(brand, "mode", "outbound"),
        mention_rate=mention_rate,
        source_count=source_count,
        brand_id_hash=brand_hash,
    )
    session.add(sample)


def _industry_seed_baseline(industry: str) -> dict:
    """行业经验参考基准（非真实样本，诚实标注）。
    给早期用户一个参照系，避免大盘空荡荡。数值为行业普遍经验区间。"""
    # 不同行业的 AI 提及率经验值（头部/平均/起步）
    SEED = {
        "医美": {"top": 35, "avg": 12, "entry": 3},
        "装修": {"top": 30, "avg": 10, "entry": 2},
        "教育": {"top": 40, "avg": 15, "entry": 4},
        "法律": {"top": 32, "avg": 11, "entry": 3},
        "金融": {"top": 38, "avg": 14, "entry": 4},
        "电商": {"top": 28, "avg": 9, "entry": 2},
        "出海": {"top": 25, "avg": 8, "entry": 2},
    }
    for key, val in SEED.items():
        if key in (industry or ""):
            return val
    # 默认通用基准
    return {"top": 30, "avg": 10, "entry": 3}


@app.get("/api/brands/{brand_id}/industry-benchmark")
def industry_benchmark(brand_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """行业大盘：商家看自己在行业里的排名。数据不足时诚实显示积累中。"""
    brand = _owned_brand(brand_id, user, session)
    industry_std = _normalize_industry(brand.industry or brand.product or "")

    samples = session.exec(
        select(IndustrySample).where(IndustrySample.industry == industry_std)
        .order_by(IndustrySample.created_at.desc())
    ).all()

    # 按品牌哈希去重
    seen = set()
    unique = []
    for s in samples:
        if s.brand_id_hash not in seen:
            seen.add(s.brand_id_hash)
            unique.append(s)
    sample_count = len(unique)

    my_recs = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).all()
    my_rate = round(my_recs[0].mention_rate, 1) if my_recs else 0

    MIN_SAMPLES = 5
    if sample_count < MIN_SAMPLES:
        # 样本不足时，给一个基于行业常识的"参考基准线"，让早期用户也有参照
        # 诚实标注这是经验参考值，非真实样本统计
        seed_baseline = _industry_seed_baseline(industry_std)
        return {
            "has_benchmark": False, "industry": industry_std,
            "sample_count": sample_count, "needed": MIN_SAMPLES, "my_rate": my_rate,
            "seed_baseline": seed_baseline,  # 经验参考基准
            "seed_note": "以下为基于行业经验的参考基准（非真实样本统计）。随着更多品牌加入，将逐步显示真实行业大盘。",
            "message": f"「{industry_std}」真实样本积累中（{sample_count}/{MIN_SAMPLES}）。先给你一个行业经验参考：",
        }

    rates = sorted([s.mention_rate for s in unique], reverse=True)
    avg_rate = round(sum(rates) / len(rates), 1)
    max_rate = round(max(rates), 1)
    median_rate = round(rates[len(rates)//2], 1)
    below_me = sum(1 for r in rates if r < my_rate)
    percentile = round(below_me / len(rates) * 100)

    if percentile >= 70:
        rank_text = f"🏆 你超过了行业 {percentile}% 的品牌，处于领先地位！"
        rank_color = "good"
    elif percentile >= 40:
        rank_text = f"📊 你超过了行业 {percentile}% 的品牌，处于中游，还有提升空间。"
        rank_color = "neutral"
    else:
        rank_text = f"⚠️ 你只超过了行业 {percentile}% 的品牌，落后于多数同行，需加快优化。"
        rank_color = "bad"

    return {
        "has_benchmark": True, "industry": industry_std,
        "sample_count": sample_count, "my_rate": my_rate,
        "industry_avg": avg_rate, "industry_max": max_rate, "industry_median": median_rate,
        "percentile": percentile, "rank_text": rank_text, "rank_color": rank_color,
        "gap_to_avg": round(my_rate - avg_rate, 1),
        "gap_to_top": round(max_rate - my_rate, 1),
        "data_source": f"基于见微平台上 {sample_count} 个「{industry_std}」行业品牌的真实监测数据（匿名聚合），样本越多越准确。",
    }


@app.get("/api/industry-stats")
def industry_stats_public(session: Session = Depends(get_session)):
    """公开行业统计（无需登录）。展示各行业积累的样本量。"""
    samples = session.exec(select(IndustrySample)).all()
    by_industry = {}
    for s in samples:
        by_industry.setdefault(s.industry, {})[s.brand_id_hash] = s.mention_rate
    result = []
    for industry, brands in by_industry.items():
        rates = list(brands.values())
        if rates:
            result.append({
                "industry": industry, "brand_count": len(rates),
                "avg_rate": round(sum(rates) / len(rates), 1),
            })
    result.sort(key=lambda x: -x["brand_count"])
    return {
        "total_samples": len(samples),
        "total_brands": sum(r["brand_count"] for r in result),
        "industries": result,
    }


@app.post("/api/content/generate")
async def gen_content(req: GenContentReq, request: Request, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    _rate_limit(f"gencontent:{_client_ip(request)}", max_calls=20, window_sec=3600)
    brand = _owned_brand(req.brand_id, user, session)
    # 额度检查：体验版可用1次，专业版以上不限
    _cplan = plan_of(user)
    _climit = _cplan.get("content_limit", 0)
    _cused = getattr(user, "content_count", 0) or 0
    if _climit < 999 and _cused >= _climit:
        raise HTTPException(403, "内容生成体验次数已用完。升级专业版可不限次生成符合AI口味的优质内容。")
    # 融合知识库：把知识库条目拼进品牌资料，让生成内容更准、更像品牌
    kb_items = session.exec(
        select(KnowledgeItem).where(KnowledgeItem.brand_id == brand.id)
    ).all()
    kb_text = brand.brand_facts or ""
    if kb_items:
        kb_text += "\n\n【品牌知识库】\n"
        for it in kb_items:
            cat_label = {"selling_point": "卖点", "faq": "问答",
                         "fact": "事实", "story": "故事"}.get(it.category, "")
            kb_text += f"[{cat_label}] {it.title}：{it.content}\n"

    result = await generate_content(
        brand.name, req.gap_question, brand.product,
        content_type=req.content_type, brand_facts=kb_text,
    )
    gc = GeneratedContent(
        brand_id=brand.id, gap_question=req.gap_question,
        content_type=req.content_type, title=result.get("title", ""),
        body=result.get("body", ""), publish_tip=result.get("publish_tip", ""),
    )
    session.add(gc)
    # 内容生成计数+1
    user.content_count = _cused + 1
    session.add(user)
    session.commit()
    return result


class ContentPackReq(BaseModel):
    brand_id: int
    topic: str
    platforms: list = []

# ===== 增长引擎 · 4大高阶功能 =====
# ===== 关键词收录情况 & 收录追踪 =====
class KeywordAnalyzeReq(BaseModel):
    brand_id: int
    keywords: list = []

@app.post("/api/keyword/analyze")
async def keyword_analyze(req: KeywordAnalyzeReq, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """关键词相对热度分析（老实方案，不编精确搜索量）。"""
    try:
        brand = _owned_brand(req.brand_id, user, session)
        kws = req.keywords
        if not kws:
            # 没传就用品牌的问题集
            qs = json.loads(brand.questions_json or "[]")
            kws = [q.get("question", "") for q in qs[:12] if q.get("question")]
        from services.generator import analyze_keyword_index
        return await analyze_keyword_index(brand.name, brand.industry, kws)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"关键词分析失败：{type(e).__name__}: {e}")

class IndexTrackReq(BaseModel):
    brand_id: int
    keyword: str
    platform: str = ""
    url: str = ""

@app.post("/api/index-track/add")
def index_track_add(req: IndexTrackReq, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    """添加一条收录追踪：我在某平台发了关于某关键词的内容，追踪是否被AI收录。"""
    brand = _owned_brand(req.brand_id, user, session)
    it = IndexTrack(user_id=user.id, brand_id=brand.id,
                    keyword=req.keyword[:100], platform=req.platform[:40], url=req.url[:300])
    session.add(it); session.commit(); session.refresh(it)
    return {"ok": True, "id": it.id}

@app.get("/api/index-track/list")
def index_track_list(brand_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """列出某品牌的收录追踪记录。"""
    brand = _owned_brand(brand_id, user, session)
    items = session.exec(
        select(IndexTrack).where(IndexTrack.brand_id == brand.id)
        .order_by(IndexTrack.created_at.desc())
    ).all()
    return {"items": [{
        "id": it.id, "keyword": it.keyword, "platform": it.platform, "url": it.url,
        "status": it.status, "check_count": it.check_count,
        "first_indexed": it.first_indexed_at.strftime("%Y-%m-%d") if it.first_indexed_at else None,
        "created": it.created_at.strftime("%m-%d") if it.created_at else "",
    } for it in items]}

@app.post("/api/index-track/check/{track_id}")
async def index_track_check(track_id: int, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """检测某条追踪：这个关键词现在 AI 回答里有没有引用到你的内容。
    基于该品牌最近的监测数据判断（复用已有信源数据，不额外烧钱）。"""
    it = session.get(IndexTrack, track_id)
    if not it or it.user_id != user.id:
        raise HTTPException(404, "记录不存在")
    brand = session.get(Brand, it.brand_id)
    # 取最近监测报告，看信源里有没有该平台/URL
    reports = session.exec(
        select(Report).where(Report.brand_id == it.brand_id)
        .order_by(Report.generated_at.desc())
    ).all()
    indexed = False
    if reports:
        full = _jload(reports[0].full_json, {})
        sources = full.get("citation_targets", [])
        raw = full.get("raw_results", [])
        # 判断：品牌是否被提及 + 信源里有没有匹配的平台
        brand_mentioned = any(r.get("brand_mentioned") for r in raw)
        platform_hit = False
        if it.platform:
            for s in sources:
                if it.platform.lower() in str(s.get("source", "")).lower():
                    platform_hit = True; break
        indexed = brand_mentioned and (platform_hit or not it.platform)
    it.check_count += 1
    it.last_check_at = cn_now()
    if indexed and it.status != "indexed":
        it.status = "indexed"
        it.first_indexed_at = cn_now()
    elif not indexed and it.status == "pending":
        it.status = "not_yet"
    session.add(it); session.commit()
    return {
        "status": it.status,
        "indexed": indexed,
        "msg": "✅ 已被 AI 收录引用！" if indexed else "暂未检测到被 AI 引用，继续投喂内容或过几天再测。",
        "note": "基于最近一次监测的信源数据判断。建议先重新监测再检测，结果更准。",
    }


@app.get("/api/growth/media-matrix")
async def growth_media_matrix(user: User = Depends(current_user)):
    """独家高权重媒体直发矩阵。本地数据零成本，对所有登录用户开放（引流钩子）。"""
    try:
        from services.generator import get_media_matrix
        return get_media_matrix()
    except Exception as e:
        raise HTTPException(500, f"媒体矩阵生成失败：{type(e).__name__}: {e}")

class SchemaInjectReq(BaseModel):
    brand_id: int
    address: str = ""
    phone: str = ""
    url: str = ""

@app.post("/api/growth/schema-inject")
async def growth_schema_inject(req: SchemaInjectReq, user: User = Depends(current_user),
                               session: Session = Depends(get_session)):
    """Schema结构化数据一键注入。本地生成零成本，对所有登录用户开放。"""
    try:
        brand = _owned_brand(req.brand_id, user, session)
        from services.generator import generate_schema_inject
        return generate_schema_inject(brand.name, brand.product, brand.industry,
                                      req.address, req.phone, req.url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Schema生成失败：{type(e).__name__}: {e}")

class GrowthTopicReq(BaseModel):
    brand_id: int
    topic: str = ""

@app.post("/api/growth/rag-corpus")
async def growth_rag_corpus(req: GrowthTopicReq, request: Request,
                            user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """逆向RAG专家语料生成引擎。"""
    try:
        _rate_limit(f"rag:{_client_ip(request)}", max_calls=15, window_sec=3600)
        if plan_of(user).get("content_limit", 0) <= 0:
            raise HTTPException(403, "增长引擎是付费功能，升级 AI Growth Pro 解锁。")
        brand = _owned_brand(req.brand_id, user, session)
        kb = brand.brand_facts or ""
        for it in session.exec(select(KnowledgeItem).where(KnowledgeItem.brand_id == brand.id)).all():
            kb += f"\n{it.title}：{it.content}"
        from services.generator import generate_rag_corpus
        return await generate_rag_corpus(brand.name, brand.product, kb, req.topic or brand.industry)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"RAG语料生成失败：{type(e).__name__}: {e}")

@app.post("/api/growth/intent-titles")
async def growth_intent_titles(req: GrowthTopicReq, request: Request,
                               user: User = Depends(current_user),
                               session: Session = Depends(get_session)):
    """高转化意图拦截与标题工程。"""
    try:
        _rate_limit(f"intent:{_client_ip(request)}", max_calls=15, window_sec=3600)
        if plan_of(user).get("content_limit", 0) <= 0:
            raise HTTPException(403, "增长引擎是付费功能，升级 AI Growth Pro 解锁。")
        brand = _owned_brand(req.brand_id, user, session)
        kb = brand.brand_facts or ""
        for it in session.exec(select(KnowledgeItem).where(KnowledgeItem.brand_id == brand.id)).all():
            kb += f"\n{it.title}：{it.content}"
        from services.generator import generate_intent_titles
        return await generate_intent_titles(brand.name, brand.product, brand.industry, kb)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"意图标题生成失败：{type(e).__name__}: {e}")


@app.get("/api/brands/{brand_id}/perception")
async def brand_perception(brand_id: int, user: User = Depends(current_user),
                           session: Session = Depends(get_session)):
    """AI 品牌认知报告：基于最近一次监测，分析『AI眼中的你』。
    复用已有监测数据，仅一次AI调用聚合，成本可控。"""
    brand = _owned_brand(brand_id, user, session)
    # 取最近一次监测报告
    reports = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).all()
    if not reports:
        return {"has_data": False, "msg": "还没有监测数据，请先完成一次 AI 监测。"}
    full = _jload(reports[0].full_json, {})
    raw_results = full.get("raw_results", [])
    # 提取各平台回答
    platform_answers = []
    for r in raw_results:
        platform_answers.append({
            "platform": r.get("platform_label") or r.get("platform", "AI"),
            "answer": r.get("answer_text", ""),
            "mentioned": r.get("brand_mentioned", False),
        })
    # 融合知识库作为对照基准
    kb_items = session.exec(
        select(KnowledgeItem).where(KnowledgeItem.brand_id == brand.id)
    ).all()
    kb_text = brand.brand_facts or ""
    if kb_items:
        for it in kb_items:
            kb_text += f"\n{it.title}：{it.content}"
    from services.generator import analyze_brand_perception
    result = await analyze_brand_perception(
        brand.name, brand.product, kb_text, platform_answers)
    result["brand_name"] = brand.name
    result["report_date"] = reports[0].generated_at.strftime("%Y-%m-%d") if reports[0].generated_at else ""
    return result


@app.post("/api/content/pack")
async def gen_content_pack(req: ContentPackReq, request: Request,
                           user: User = Depends(current_user),
                           session: Session = Depends(get_session)):
    """多平台内容包：一次生成同一主题的多个平台适配版本。
    每版套用AI收录结构模板，付费功能（走content_limit额度，一次算多次消耗）。"""
    _rate_limit(f"contentpack:{_client_ip(request)}", max_calls=10, window_sec=3600)
    brand = _owned_brand(req.brand_id, user, session)
    _cplan = plan_of(user)
    _climit = _cplan.get("content_limit", 0)
    _cused = getattr(user, "content_count", 0) or 0
    # 内容包是付费功能：免费版不可用
    if _climit <= 0:
        raise HTTPException(403, "多平台内容包是付费功能，升级后可一次生成多平台版本。")
    if _climit < 999 and _cused >= _climit:
        raise HTTPException(403, "内容生成次数已用完，升级增长版可不限次。")
    # 限制平台数量，防滥用烧token
    pfs = req.platforms or ["zhihu", "souhu", "baijiahao", "gongzhonghao"]
    pfs = pfs[:5]
    # 融合知识库
    kb_items = session.exec(
        select(KnowledgeItem).where(KnowledgeItem.brand_id == brand.id)
    ).all()
    kb_text = brand.brand_facts or ""
    if kb_items:
        kb_text += "\n\n【品牌知识库】\n"
        for it in kb_items:
            kb_text += f"{it.title}：{it.content}\n"
    from services.generator import generate_content_pack
    result = await generate_content_pack(
        brand.name, req.topic, brand.product, brand_facts=kb_text, platforms=pfs,
    )
    # 计数（内容包按生成的版本数计入额度）
    user.content_count = _cused + len(result.get("versions", []))
    session.add(user)
    session.commit()
    return result


@app.post("/api/content/schema-faq")
async def gen_schema_faq(req: GenContentReq, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """
    一键生成 Schema + FAQ：
    - 输出可直接复制到网站的 JSON-LD Schema 代码
    - 输出基于品牌特征的 FAQ 问答对
    这两个是让 AI 更容易引用你网站的最高效手段
    """
    brand = _owned_brand(req.brand_id, user, session)
    questions_raw = json.loads(brand.questions_json or "[]")
    # 取前8个问题作为FAQ基础
    faq_questions = [q.get("question","") for q in questions_raw[:8] if q.get("question")]

    from services.generator import _chat, _safe_parse_json
    system = "你是网站SEO和GEO优化专家，擅长生成让AI搜索引擎更容易引用的结构化内容。"
    prompt = f"""
品牌：{brand.name}
行业：{brand.industry}
主营产品：{brand.product}
品牌官网信息：{brand.brand_facts[:500] if brand.brand_facts else '暂无'}

基于以上信息，生成：

1. FAQ（常见问题解答）：基于以下问题，每个写一个简洁有力的回答（2-3句话）
{chr(10).join(f'- {q}' for q in faq_questions[:6])}

2. JSON-LD Schema代码：生成 FAQPage schema，包含上述FAQ

只返回JSON：
{{"faq":[{{"q":"问题","a":"回答"}}],"schema_code":"JSON-LD代码字符串","publish_tip":"发布建议"}}
"""
    raw = await _chat(prompt, system, json_mode=True)
    data = _safe_parse_json(raw)
    if not data:
        raise HTTPException(500, "生成失败，请重试")
    return data


@app.get("/api/brands/{brand_id}/monthly-loss")
def monthly_loss(brand_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """
    月损失AI流量估算：
    基于最新监测的提及率，估算每月损失多少次AI推荐曝光
    这个数字让商家感受到损失的量级，是付费的强力触发器
    """
    brand = _owned_brand(brand_id, user, session)
    recs = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).all()
    if not recs:
        return {"has_data": False, "message": "请先完成一次监测"}
    latest = recs[0]
    result = estimate_monthly_loss(latest.mention_rate, brand.industry)
    result["has_data"] = True
    result["based_on_report_date"] = latest.generated_at
    return result


class CompetitorReq(BaseModel):
    brand_id: int
    competitor_url: str   # 竞品官网URL
    competitor_name: str = ""  # 竞品名称（可选）

@app.post("/api/brands/{brand_id}/competitor-compare")
async def competitor_compare(brand_id: int, req: CompetitorReq,
                             user: User = Depends(current_user),
                             session: Session = Depends(get_session)):
    """
    竞品对比：
    输入竞品URL → 抓取竞品官网 → 用同样的问题集监测竞品 → 并排对比
    让商家清楚看到：竞品为什么比我被AI推荐得多？
    """
    brand = _owned_brand(brand_id, user, session)
    questions_raw = json.loads(brand.questions_json or "[]")
    if not questions_raw:
        raise HTTPException(400, "请先生成问题集")

    # 抓取竞品官网
    comp_name = req.competitor_name or req.competitor_url
    comp_facts = ""
    try:
        comp_facts = await build_knowledge_base(req.competitor_url)
    except Exception:
        comp_facts = ""

    # 用前10个问题监测竞品（节省成本）
    questions = [q.get("question","") for q in questions_raw[:10] if q.get("question")]
    competitors = []
    mode = getattr(brand, "mode", "outbound")

    comp_report = await run_monitoring(
        comp_name, questions, competitors,
        samples_per_question=1, mode=mode
    )
    # 获取品牌自己最新报告
    brand_recs = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).all()

    brand_rate = brand_recs[0].mention_rate if brand_recs else 0
    comp_rate = comp_report.mention_rate

    # 分析差距原因
    gap = comp_rate - brand_rate
    if gap > 20:
        analysis = f"竞品 {comp_name} 的 AI 推荐率比你高 {gap:.0f} 个百分点，差距显著。主要原因：竞品在 AI 训练数据中的内容覆盖更广，被引用来源（{comp_report.source_count} 个）多于你。"
        action = "建议优先补充官网结构化内容，并在 Reddit/知乎等高权重平台发布品牌内容，缩短与竞品的差距。"
    elif gap > 0:
        analysis = f"竞品 {comp_name} 的 AI 推荐率比你高 {gap:.0f} 个百分点，差距较小，有追平机会。"
        action = "针对竞品覆盖的问题场景，补充 2-3 篇针对性内容即可追平。"
    else:
        analysis = f"恭喜！你的 AI 推荐率（{brand_rate}%）高于竞品 {comp_name}（{comp_rate}%），保持优势。"
        action = "继续完成 GEO 任务清单，扩大领先优势。"

    return {
        "brand_name": brand.name,
        "brand_mention_rate": brand_rate,
        "competitor_name": comp_name,
        "competitor_url": req.competitor_url,
        "competitor_mention_rate": comp_rate,
        "competitor_source_count": comp_report.source_count,
        "competitor_platform_breakdown": comp_report.platform_breakdown,
        "gap": round(gap, 1),
        "analysis": analysis,
        "action": action,
        "questions_tested": len(questions),
    }


@app.get("/api/brands/{brand_id}/keyword-opportunities")
async def keyword_opportunities(brand_id: int, user: User = Depends(current_user),
                                session: Session = Depends(get_session)):
    """
    关键词商机分析：
    告诉商家这个行业哪些关键词在AI时代值得抢占
    输出：AI热度 + 竞争度 + 商机评分 + 内容建议，按商机评分排序
    """
    brand = _owned_brand(brand_id, user, session)
    result = await analyze_keyword_opportunities(
        industry=brand.industry or brand.product or brand.name,
        brand_name=brand.name,
        product=brand.product,
        mode=getattr(brand, "mode", "outbound"),
        count=12,
    )
    if result.get("error"):
        raise HTTPException(500, result.get("message", "分析失败"))
    return result


# 发布平台映射：根据关键词意图，推荐最该发的平台
def _publish_platforms(mode: str):
    if mode == "domestic":
        return [
            {"name": "知乎", "why": "AI高频引用，专业问答最易被收录", "how": "在相关问题下回答，或开品牌专栏写深度内容"},
            {"name": "百度（百科/百家号）", "why": "中文权威源，AI信任度高", "how": "做品牌百科词条 + 百家号发科普内容"},
            {"name": "小红书", "why": "消费决策类AI爱引用", "how": "做真实测评/种草笔记，带关键词"},
        ]
    return [
        {"name": "Reddit", "why": "海外AI高频引用的讨论源", "how": "在相关subreddit真实参与讨论"},
        {"name": "Quora", "why": "英文问答，AI常引用", "how": "回答相关问题，自然带出品牌"},
        {"name": "行业媒体/独立站", "why": "建立权威背书", "how": "投稿行业媒体或优化自己官网的FAQ页"},
    ]


@app.get("/api/brands/{brand_id}/battle-plan")
async def battle_plan(brand_id: int, request: Request, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """7天上推荐作战包：
    核心 = 用「本品牌最近一次监测里 AI 真实引用的源」作为精确发布目标，
    而不是通用猜测。挑词→真实信源→7天行动表，编排成可执行计划。
    复用已有数据，不新增监测、不碰现有数据。"""
    _rate_limit(f"battle:{_client_ip(request)}", max_calls=20, window_sec=3600)
    brand = _owned_brand(brand_id, user, session)
    mode = getattr(brand, "mode", "outbound")

    # 额度检查：体验版可用1次，专业版以上不限
    _bplan = plan_of(user)
    _blimit = _bplan.get("battle_limit", 0)
    _bused = getattr(user, "battle_count", 0) or 0
    if _blimit < 999 and _bused >= _blimit:
        raise HTTPException(403, "作战包体验次数已用完。升级专业版可不限次生成专属作战计划，持续帮你上推荐。")

    # === 关键：取本品牌最近一次报告里 AI 真实引用的源 ===
    last_report = session.exec(
        select(Report).where(Report.brand_id == brand.id)
        .order_by(Report.generated_at.desc())
    ).first()
    real_targets = []          # AI 真实引用、但本品牌还没露出的高价值源
    real_present = []          # AI 引用且已有本品牌的源（守住）
    has_real_data = False
    if last_report and last_report.full_json:
        try:
            full = json.loads(last_report.full_json)
            cts = full.get("citation_targets", []) or []
            has_real_data = len(cts) > 0
            for ct in cts:
                item = {
                    "source": ct.get("source", ""),
                    "cited_count": ct.get("cited_count", 0),
                    "advice": _source_action(ct.get("source", "")),
                }
                if ct.get("brand_present"):
                    real_present.append(item)
                else:
                    real_targets.append(item)
            # 按被引用次数排序（AI 越常引用 = 越该攻克）
            real_targets.sort(key=lambda x: -x["cited_count"])
        except Exception:
            has_real_data = False

    # === 关键词：挑速效词 ===
    kw_result = await analyze_keyword_opportunities(
        industry=brand.industry or brand.product or brand.name,
        brand_name=brand.name, product=brand.product, mode=mode, count=12,
    )
    keywords = kw_result.get("keywords", []) if not kw_result.get("error") else []
    quick_win = [
        k for k in keywords
        if k.get("competition", 100) < 50
        and k.get("intent") in ("购买决策", "品牌寻找", "问题解决")
    ]
    quick_win.sort(key=lambda k: k.get("opportunity_score", 0), reverse=True)
    target_keywords = quick_win[:2] if quick_win else keywords[:2]

    # === 发布目标：真实信源优先，否则降级到行业通用建议 ===
    if has_real_data and real_targets:
        publish_targets = real_targets[:6]
        targets_source = "real"   # 来自真实监测数据
        top_names = "、".join(t["source"] for t in publish_targets[:2])
    else:
        publish_targets = _publish_platforms(mode)  # 行业通用建议（降级）
        targets_source = "fallback"
        top_names = "、".join(p["name"] for p in publish_targets[:2])

    kw_names = "、".join(k.get("keyword", "") for k in target_keywords) or "你的核心词"
    timeline = [
        {"day": "Day 1-2", "title": "挑词 + 建库",
         "tasks": [f"锁定速效词：{kw_names}", "确保品牌知识库已建好（内容工作台 → AI自动提取）"]},
        {"day": "Day 3-4", "title": "生成结构化内容",
         "tasks": ["用内容工作台，针对速效词一键生成内容", "确认内容含小标题、FAQ问答、权威数据", "复制Schema/FAQ代码备用"]},
        {"day": "Day 5-6", "title": "精准发布",
         "tasks": [f"把内容发到 AI 真实引用的源：{top_names}", "官网FAQ页放上结构化内容和Schema代码", "确保关键信息不藏在图片/JS里"]},
        {"day": "Day 7", "title": "复测验证",
         "tasks": ["回见微再监测一次", "对比提及率是否提升", "看AI是否开始在答案里提到你"]},
    ]

    note = ("以上发布目标，来自 AI 在回答你所在行业问题时**真实引用过的信息源**（按引用频次排序），"
            "不是通用猜测。攻克这些源 = 在 AI 已经信任的地方让它看到你。"
            if targets_source == "real"
            else "你还没有监测数据，以下是行业通用建议。**先做一次监测**，系统就能告诉你 AI 在你这个行业实际引用了哪些精确的源——那才是最该攻克的目标。")

    # 成功生成，计数+1（额度控制）
    user.battle_count = _bused + 1
    session.add(user)
    session.commit()

    # === 新增：竞品弱点深度分析（把信源数据变成"竞品弱在哪、你怎么抢"）===
    competitor_battle = None
    try:
        competitors = [c.strip() for c in (brand.competitors or "").split(",") if c.strip()]
        if competitors and has_real_data and last_report:
            full_cts = json.loads(last_report.full_json).get("citation_targets", [])
            from services.generator import generate_battle_plan
            competitor_battle = await generate_battle_plan(
                brand.name, brand.industry or "", brand.product or "",
                competitors, full_cts, last_report.mention_rate or 0)
    except Exception:
        competitor_battle = None

    return {
        "brand": brand.name,
        "target_keywords": target_keywords,
        "publish_targets": publish_targets,
        "targets_source": targets_source,         # real=真实数据 / fallback=通用建议
        "present_sources": real_present[:5],      # AI已引用且有你的源（守住）
        "timeline": timeline,
        "targets_note": note,
        "competitor_battle": competitor_battle,   # ★新增：竞品弱点+我方缺口+行动清单
        "honest_note": "诚实提醒：冷门高意图词约1周可见效；热门大词需要持续积累。这套流程把你上推荐的概率做到最高，但不保证必上——做了大概率有效，谁也不能保证100%。",
        "summary": kw_result.get("top_advice", ""),
    }


def _source_action(src: str) -> str:
    """把引用源翻译成具体发布动作（后端版，与前端 sourceAdvice 对应）"""
    s = (src or "").lower()
    rules = [
        ("zhihu", "去知乎相关问题下回答，或开品牌专栏"),
        ("baidu", "做百度百科词条 / 百家号内容"),
        ("xiaohongshu", "在小红书做测评/种草笔记"), ("xhs", "在小红书做测评/种草笔记"),
        ("weibo", "在微博发布或找 KOL 提及"),
        ("reddit", "参与相关 subreddit 讨论"),
        ("quora", "在 Quora 回答相关问题"),
        ("g2", "建立有真实评价的产品资料页"), ("capterra", "建立有真实评价的产品资料页"),
        ("wikipedia", "完善维基词条（需符合收录标准）"), ("wiki", "完善维基词条"),
        ("youtube", "做视频内容或找博主合作"), ("bilibili", "做B站视频或找UP主合作"),
        ("linkedin", "在领英发布行业内容"),
        ("zhihu.com", "去知乎布局问答"),
        ("amazon", "优化电商平台产品详情和评价"),
        ("taobao", "优化店铺详情和评价"), ("tmall", "优化天猫详情和评价"),
        ("jd", "优化京东产品详情和评价"),
        ("dianping", "优化大众点评店铺信息和评价"), ("meituan", "优化美团商户信息"),
        ("xinyang", "在新氧布局案例和口碑"),
    ]
    for key, act in rules:
        if key in s:
            return act
    return f"争取在 {src} 发布内容或被提及"


# ----------------------------- 工具 -----------------------------

def _owned_brand(brand_id: int, user: User, session: Session) -> Brand:
    brand = session.get(Brand, brand_id)
    if not brand or brand.user_id != user.id:
        raise HTTPException(404, "品牌不存在")
    return brand


# ============================= 支付模块 =============================
# 对接 YunGouOS（个人可签约的微信支付宝服务商）
# 需要在 Render 配置：YUNGOUOS_MCH_ID（商户号）、YUNGOUOS_KEY（密钥）
# 未配置时，下单走"联系客服"降级模式，不影响其他功能

import hashlib as _hashlib_pay

YUNGOUOS_MCH_ID = os.getenv("YUNGOUOS_MCH_ID", "")
YUNGOUOS_KEY = os.getenv("YUNGOUOS_KEY", "")
YUNGOUOS_WXPAY_URL = "https://api.pay.yungouos.com/api/pay/wxpay/nativePay"
YUNGOUOS_ALIPAY_URL = "https://api.pay.yungouos.com/api/pay/alipay/nativePay"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://geo-radar.onrender.com")


def _pay_enabled() -> bool:
    return bool(YUNGOUOS_MCH_ID and YUNGOUOS_KEY)


def _yungouos_sign(params: dict) -> str:
    """YunGouOS 签名：参数按key升序拼接 + 密钥，MD5大写"""
    # 过滤空值和sign本身
    items = {k: v for k, v in params.items() if v != "" and k != "sign"}
    sorted_keys = sorted(items.keys())
    sign_str = "&".join(f"{k}={items[k]}" for k in sorted_keys)
    sign_str += f"&key={YUNGOUOS_KEY}"
    return _hashlib_pay.md5(sign_str.encode()).hexdigest().upper()


class CreateOrderReq(BaseModel):
    plan: str                       # 要购买的套餐
    pay_method: str = "wxpay"       # wxpay 或 alipay


@app.post("/api/order/create")
async def create_order(req: CreateOrderReq, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """
    创建支付订单，返回支付二维码链接。
    未配置支付时返回"联系客服"降级提示。
    """
    if req.plan not in PLAN_PRICES:
        raise HTTPException(400, "套餐不存在")
    amount = PLAN_PRICES[req.plan]
    plan_name = PLANS.get(req.plan, {}).get("name", req.plan)

    # 生成订单号
    order_no = "GEO" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3).upper()
    order = Order(
        order_no=order_no, user_id=user.id, plan=req.plan,
        amount=amount, status="pending", pay_method=req.pay_method,
    )
    session.add(order)
    session.commit()

    # 未配置支付：降级为联系客服
    if not _pay_enabled():
        return {
            "order_no": order_no,
            "pay_enabled": False,
            "amount": amount,
            "plan_name": plan_name,
            "message": "在线支付即将开通，当前请联系客服微信 jenly222 开通，备注订单号即可",
            "service_wechat": "jenly222",
        }

    # 调用 YunGouOS 生成支付二维码
    params = {
        "mch_id": YUNGOUOS_MCH_ID,
        "out_trade_no": order_no,
        "total_fee": f"{amount:.2f}",
        "body": f"GEO雷达-{plan_name}",
        "notify_url": f"{PUBLIC_URL}/api/order/notify",
    }
    params["sign"] = _yungouos_sign(params)
    url = YUNGOUOS_WXPAY_URL if req.pay_method == "wxpay" else YUNGOUOS_ALIPAY_URL

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, data=params, timeout=20)
            data = r.json()
    except Exception as e:
        raise HTTPException(500, f"支付下单失败：{str(e)[:100]}")

    # YunGouOS 返回 code=0 成功，data 是二维码内容
    if str(data.get("code")) != "0":
        raise HTTPException(500, f"支付下单失败：{data.get('msg', '未知错误')}")

    return {
        "order_no": order_no,
        "pay_enabled": True,
        "amount": amount,
        "plan_name": plan_name,
        "qr_content": data.get("data", ""),   # 二维码内容，前端生成二维码图
        "pay_method": req.pay_method,
    }


@app.post("/api/order/notify")
async def order_notify(request: Request, session: Session = Depends(get_session)):
    """
    YunGouOS 支付回调（公开接口）。
    支付成功后：自动开通套餐 + 重置次数 + 结算分销佣金。
    """
    form = await request.form()
    data = dict(form)

    # 验签
    recv_sign = data.get("sign", "")
    calc_sign = _yungouos_sign(data)
    if not hmac.compare_digest(recv_sign, calc_sign):
        return "fail"

    order_no = data.get("out_trade_no", "")
    pay_status = data.get("code", "")   # YunGouOS 成功通常 code=1 或 success

    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if not order:
        return "fail"
    if order.status == "paid":
        return "SUCCESS"   # 已处理过，幂等

    # 标记订单已支付
    order.status = "paid"
    order.pay_no = data.get("pay_no", "")
    order.paid_at = datetime.utcnow()
    session.add(order)

    # 自动开通套餐
    user = session.get(User, order.user_id)
    if user and order.plan in PLANS:
        user.plan = order.plan
        user.monitor_count = 0   # 重置监测次数
        session.add(user)

        # 分销佣金自动结算
        if user.referred_by and order.plan in PLAN_PRICES:
            ref = session.exec(
                select(Referral).where(
                    Referral.referrer_id == user.referred_by,
                    Referral.referred_user_id == user.id,
                )
            ).first()
            if ref and ref.status != "paid":
                ref.status = "paid"
                ref.commission = round(order.amount * COMMISSION_RATE, 2)
                ref.paid_plan = order.plan
                ref.paid_at = datetime.utcnow()
                session.add(ref)

    session.commit()
    return "SUCCESS"


@app.get("/api/order/{order_no}/status")
def order_status(order_no: str, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """前端轮询订单状态，支付成功后前端自动跳转"""
    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if not order or order.user_id != user.id:
        raise HTTPException(404, "订单不存在")
    result = {"order_no": order_no, "status": order.status}
    if order.status == "paid":
        result["plan"] = order.plan
        result["plan_info"] = plan_of(user)
    return result


# ----------------------------- 分销接口 -----------------------------

@app.get("/api/my-referral")
def my_referral(user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """我的推广：邀请码、推广链接、已邀请用户、佣金统计"""
    # 确保有邀请码
    if not user.invite_code:
        user.invite_code = _gen_invite_code()
        session.add(user)
        session.commit()
        session.refresh(user)

    # 我推荐的所有记录
    refs = session.exec(
        select(Referral).where(Referral.referrer_id == user.id)
        .order_by(Referral.created_at.desc())
    ).all()

    total_invited = len(refs)
    total_paid = sum(1 for r in refs if r.status == "paid")
    total_commission = sum(r.commission for r in refs)
    pending_commission = sum(r.commission for r in refs if r.status == "paid")

    base_url = os.getenv("PUBLIC_URL", "https://geo-radar.onrender.com")
    invite_link = f"{base_url}/?ref={user.invite_code}"

    return {
        "invite_code": user.invite_code,
        "invite_link": invite_link,
        "total_invited": total_invited,
        "total_paid": total_paid,
        "total_commission": round(total_commission, 2),
        "pending_commission": round(pending_commission, 2),
        "commission_rate": int(COMMISSION_RATE * 100),
        "referrals": [{
            "email": _mask_email(r.referred_email),
            "status": r.status,
            "status_text": "已付费" if r.status == "paid" else "已注册",
            "commission": round(r.commission, 2),
            "plan": r.paid_plan,
            "date": str(r.created_at)[:10],
        } for r in refs],
    }


def _mask_email(email: str) -> str:
    """邮箱脱敏：ab***@qq.com"""
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return name[0] + "***@" + domain
    return name[:2] + "***@" + domain


# ----------------------------- 品牌知识库 -----------------------------

class KnowledgeReq(BaseModel):
    brand_id: int
    category: str = "fact"
    title: str = ""
    content: str

@app.get("/api/brands/{brand_id}/knowledge")
def list_knowledge(brand_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """获取品牌知识库所有条目"""
    _owned_brand(brand_id, user, session)
    items = session.exec(
        select(KnowledgeItem).where(KnowledgeItem.brand_id == brand_id)
        .order_by(KnowledgeItem.created_at.desc())
    ).all()
    # 按类别分组统计
    by_cat = {}
    for it in items:
        by_cat[it.category] = by_cat.get(it.category, 0) + 1
    return {
        "items": [{"id": it.id, "category": it.category, "title": it.title,
                   "content": it.content, "source": it.source,
                   "date": str(it.created_at)[:10]} for it in items],
        "total": len(items),
        "by_category": by_cat,
    }


@app.post("/api/brands/{brand_id}/knowledge")
def add_knowledge(brand_id: int, req: KnowledgeReq,
                  user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """手动添加知识库条目"""
    _owned_brand(brand_id, user, session)
    if not req.content.strip():
        raise HTTPException(400, "内容不能为空")
    item = KnowledgeItem(
        brand_id=brand_id, category=req.category,
        title=req.title.strip(), content=req.content.strip(),
        source="manual",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return {"id": item.id, "message": "已添加到知识库"}


@app.delete("/api/brands/{brand_id}/knowledge/{item_id}")
def delete_knowledge(brand_id: int, item_id: int,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """删除知识库条目"""
    _owned_brand(brand_id, user, session)
    item = session.get(KnowledgeItem, item_id)
    if item and item.brand_id == brand_id:
        session.delete(item)
        session.commit()
    return {"message": "已删除"}


@app.post("/api/brands/{brand_id}/knowledge/auto-extract")
async def auto_extract_knowledge(brand_id: int,
                                 user: User = Depends(current_user),
                                 session: Session = Depends(get_session)):
    """
    自动从品牌官网+已有资料提取知识库条目。
    帮商家快速建立知识库，降低门槛。
    """
    brand = _owned_brand(brand_id, user, session)
    from services.generator import _chat, _safe_parse_json
    facts = brand.brand_facts or ""
    system = "你是品牌信息架构专家，擅长从品牌资料中提炼结构化的知识点。"
    prompt = f"""
品牌：{brand.name}
行业：{brand.industry}
产品：{brand.product}
已有资料：{facts[:1500] if facts else '暂无，请基于品牌名和行业推断常见知识点'}

请提炼这个品牌的知识库条目，分为三类：
1. 品牌卖点（selling_point）：3-5条核心卖点
2. 常见问答（faq）：5条用户最可能问的问题及答案
3. 品牌事实（fact）：3-5条关键事实（成立、定位、特色等）

只返回JSON：
{{"items":[{{"category":"selling_point","title":"卖点标题","content":"详细说明"}},{{"category":"faq","title":"问题","content":"答案"}}]}}
"""
    try:
        raw = await _chat(prompt, system, json_mode=True)
    except Exception as e:
        # 把_chat的真实错误传给前端（密钥无效/余额不足/超时等）
        raise HTTPException(500, f"AI调用失败：{str(e)[:200]}")

    data = _safe_parse_json(raw)
    if not data or "items" not in data:
        # 解析失败，返回AI实际返回的内容片段帮助定位
        raise HTTPException(500, f"AI返回格式异常，无法解析。返回内容：{(raw or '空')[:150]}")

    # 存入知识库
    count = 0
    for it in data["items"]:
        ki = KnowledgeItem(
            brand_id=brand_id,
            category=it.get("category", "fact"),
            title=it.get("title", "")[:200],
            content=it.get("content", ""),
            source="ai",
        )
        session.add(ki)
        count += 1
    session.commit()
    return {"message": f"已自动提取 {count} 条知识入库", "count": count}


# ----------------------------- 内容工作台 -----------------------------

@app.get("/api/brands/{brand_id}/content-workspace")
def content_workspace(brand_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """
    内容工作台：把热搜问题、关键词商机、内容缺口汇集成"选题库"。
    商家不用自己想写什么，清单列好，按优先级排好。
    """
    brand = _owned_brand(brand_id, user, session)
    topics = []

    # 1. 从最新报告的内容缺口提取选题
    latest = session.exec(
        select(Report).where(Report.brand_id == brand_id)
        .order_by(Report.generated_at.desc())
    ).first()
    if latest:
        try:
            gaps = json.loads(latest.gaps_json or "[]")
            for g in gaps:
                q = g if isinstance(g, str) else g.get("question", "")
                if q:
                    topics.append({
                        "question": q, "source": "内容缺口",
                        "priority": "high", "reason": "AI在这个问题上没提到你",
                    })
        except Exception:
            pass

    # 2. 从问题集提取选题
    try:
        questions = json.loads(brand.questions_json or "[]")
        for qobj in questions[:10]:
            q = qobj.get("question", "") if isinstance(qobj, dict) else str(qobj)
            if q and not any(t["question"] == q for t in topics):
                topics.append({
                    "question": q, "source": "监测问题集",
                    "priority": "medium", "reason": "用户常向AI问的问题",
                })
    except Exception:
        pass

    # 已创作的内容
    contents = session.exec(
        select(GeneratedContent).where(GeneratedContent.brand_id == brand_id)
        .order_by(GeneratedContent.created_at.desc())
    ).all()
    created_questions = {c.gap_question for c in contents}

    # 标记哪些选题已创作
    for t in topics:
        t["created"] = t["question"] in created_questions

    # 知识库条目数
    kb_count = len(session.exec(
        select(KnowledgeItem).where(KnowledgeItem.brand_id == brand_id)
    ).all())

    return {
        "brand_name": brand.name,
        "topics": topics,
        "topic_count": len(topics),
        "created_count": sum(1 for t in topics if t["created"]),
        "knowledge_count": kb_count,
        "contents": [{
            "id": c.id, "question": c.gap_question, "title": c.title,
            "body": c.body, "content_type": c.content_type,
            "publish_tip": c.publish_tip, "status": c.status,
            "date": str(c.created_at)[:10],
        } for c in contents],
    }


@app.post("/api/brands/{brand_id}/content/{content_id}/mark-published")
def mark_published(brand_id: int, content_id: int,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """标记内容为已发布"""
    _owned_brand(brand_id, user, session)
    c = session.get(GeneratedContent, content_id)
    if c and c.brand_id == brand_id:
        c.status = "published" if c.status != "published" else "draft"
        session.add(c)
        session.commit()
        return {"status": c.status}
    raise HTTPException(404, "内容不存在")


@app.post("/api/brands/{brand_id}/content/{content_id}/submit-approval")
def submit_approval(brand_id: int, content_id: int,
                    user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    """提交内容给老板审批：状态变为 pending_approval，返回可分享的审批链接"""
    _owned_brand(brand_id, user, session)
    c = session.get(GeneratedContent, content_id)
    if not c or c.brand_id != brand_id:
        raise HTTPException(404, "内容不存在")
    c.status = "pending_approval"
    session.add(c)
    session.commit()
    # 生成审批令牌（简单签名，老板凭链接审批，无需登录）
    token = hashlib.sha256(f"{content_id}-{c.brand_id}-{JWT_SECRET}".encode()).hexdigest()[:16]
    base = os.getenv("PUBLIC_URL", "https://geo-radar.onrender.com")
    return {
        "status": "pending_approval",
        "approval_url": f"{base}/approve?cid={content_id}&t={token}",
        "message": "已提交审批，把链接发给老板，他点开就能一键同意/打回",
    }


@app.get("/approve")
def approval_page(cid: int, t: str, session: Session = Depends(get_session)):
    """老板审批页面（极简，无需登录，凭令牌）"""
    from fastapi.responses import HTMLResponse
    c = session.get(GeneratedContent, cid)
    if not c:
        return HTMLResponse("<h2 style='font-family:sans-serif;text-align:center;padding:60px'>内容不存在</h2>")
    token = hashlib.sha256(f"{cid}-{c.brand_id}-{JWT_SECRET}".encode()).hexdigest()[:16]
    if t != token:
        return HTMLResponse("<h2 style='font-family:sans-serif;text-align:center;padding:60px'>链接无效</h2>")

    status_label = {"pending_approval": "待你确认", "published": "已通过", "draft": "已打回"}.get(c.status, c.status)
    body_html = (c.body or "").replace("<", "&lt;").replace("\n", "<br>")
    done = c.status in ("published", "draft")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>内容审批 · 见微</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC",sans-serif}}
body{{background:#f4f1ea;color:#26221c;padding:20px;max-width:600px;margin:0 auto}}
.card{{background:#fbf9f4;border:1px solid rgba(38,34,28,.1);border-radius:14px;padding:24px;margin-top:20px}}
.brand{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
.brand .m{{width:32px;height:32px;border-radius:8px;background:#26221c;color:#f4f1ea;display:grid;place-items:center;font-weight:600}}
h1{{font-size:18px;margin:20px 0 6px}}
.sub{{font-size:13px;color:#6b655b;margin-bottom:20px}}
.title{{font-size:17px;font-weight:700;margin-bottom:14px}}
.body{{font-size:14px;line-height:1.8;color:#3a352c;background:#fff;border-radius:10px;padding:18px;border:1px solid rgba(38,34,28,.08)}}
.btns{{display:flex;gap:12px;margin-top:24px}}
.btn{{flex:1;padding:16px;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}}
.ok{{background:#3a6b4a;color:#fff}}
.no{{background:#fff;color:#b0524a;border:1px solid #b0524a}}
.done{{text-align:center;padding:40px;font-size:18px;font-weight:700}}
.shield{{display:inline-flex;align-items:center;gap:6px;background:rgba(90,125,90,.1);color:#5a7d5a;font-size:12px;font-weight:600;padding:5px 12px;border-radius:20px;margin-bottom:14px}}
</style></head><body>
<div class="brand"><span class="m">見</span><b>见微 · 内容审批</b></div>
<div class="card">
<h1>请确认这篇内容是否可以发布</h1>
<div class="sub">状态：{status_label}</div>
<div class="shield">🛡️ 已通过广告法违禁词扫描</div>
<div class="title">{c.title or '内容'}</div>
<div class="body">{body_html}</div>
{'<div class="done" style="color:#5a7d5a">✅ 你已确认通过，内容可以发布了</div>' if c.status=="published" else '<div class="done" style="color:#b0524a">↩️ 你已打回，团队会重新修改</div>' if c.status=="draft" else f'''
<div class="btns">
  <button class="btn ok" onclick="act('approve')">✅ 同意发布</button>
  <button class="btn no" onclick="act('reject')">↩️ 打回重写</button>
</div>'''}
</div>
<script>
async function act(action){{
  const r=await fetch('/api/approve-action',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{cid:{cid},t:'{t}',action}})}});
  if(r.ok){{ location.reload(); }} else {{ alert('操作失败，请重试'); }}
}}
</script>
</body></html>"""
    return HTMLResponse(html)


class ApproveActionReq(BaseModel):
    cid: int
    t: str
    action: str  # approve / reject

@app.post("/api/approve-action")
def approve_action(req: ApproveActionReq, session: Session = Depends(get_session)):
    """老板审批操作（凭令牌，无需登录）"""
    c = session.get(GeneratedContent, req.cid)
    if not c:
        raise HTTPException(404, "内容不存在")
    token = hashlib.sha256(f"{req.cid}-{c.brand_id}-{JWT_SECRET}".encode()).hexdigest()[:16]
    if req.t != token:
        raise HTTPException(403, "令牌无效")
    if req.action == "approve":
        c.status = "published"
    elif req.action == "reject":
        c.status = "draft"
    session.add(c)
    session.commit()
    return {"status": c.status}


@app.get("/simulator")
def simulator_page():
    """AI推荐模拟器独立页面，无需登录可直接访问"""
    from fastapi.responses import FileResponse
    import pathlib
    p = pathlib.Path(__file__).parent.parent / "frontend" / "simulator.html"
    return FileResponse(str(p))


@app.get("/guide")
def guide_page():
    """GEO 运营指南页面，无需登录可直接访问"""
    from fastapi.responses import FileResponse
    import pathlib
    p = pathlib.Path(__file__).parent.parent / "frontend" / "guide.html"
    return FileResponse(str(p))


@app.get("/tutorial")
def tutorial_page():
    """GEO 实操运营教程页面，无需登录可直接访问"""
    from fastapi.responses import FileResponse
    import pathlib
    p = pathlib.Path(__file__).parent.parent / "frontend" / "tutorial.html"
    return FileResponse(str(p))


@app.get("/api/health")
def health():
    configured = [p for p in ("OPENAI_API_KEY", "GEMINI_API_KEY",
                              "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY",
                              "DEEPSEEK_API_KEY")
                  if os.getenv(p)]
    return {"status": "ok", "ai_platforms_configured": len(configured)}


# ----------------------------- AI 推荐模拟器（无需登录） -----------------------------

class SimulateReq(BaseModel):
    keyword: str          # 用户输入的关键词，如 "best CRM tools"
    website: str = ""     # 可选：用户自己的网站，检测有没有被引用
    mode: str = "outbound"  # outbound=英文查海外AI  domestic=中文查国内AI

@app.post("/api/simulate")
async def simulate(req: SimulateReq, request: Request):
    """
    AI推荐模拟器：无需登录，输入关键词立刻查
    限流：同一IP每小时最多15次，防止恶意刷爆烧API费用
    """
    _rate_limit(f"simulate:{_client_ip(request)}", max_calls=15, window_sec=3600)
    try:
        return await _do_simulate(req)
    except HTTPException:
        raise
    except Exception as e:
        # 任何意外错误都返回友好提示，不返回500
        import traceback
        return {
            "keyword": req.keyword,
            "website": req.website,
            "mode": req.mode,
            "results": [],
            "summary": {
                "total_platforms": 0, "mentioned_count": 0,
                "mention_rate": 0, "your_site_found": False,
                "verdict": f"查询出错：{str(e)[:150]}",
            },
            "error_detail": traceback.format_exc()[-500:],
        }


async def _do_simulate(req: SimulateReq):
    keyword = req.keyword.strip()
    if not keyword or len(keyword) > 200:
        raise HTTPException(400, "关键词不能为空，且不超过200字")

    # 候选平台列表（便宜平台优先，模拟器是免费钩子，控制成本）
    if req.mode == "domestic":
        candidate_keys = ["deepseek", "qwen", "doubao", "kimi", "wenxin"]
        lang_hint = "用中文回答"
    else:
        # 海外模式：便宜的 DeepSeek/通义 在前，贵的 GPT/Gemini 在后
        candidate_keys = ["deepseek", "qwen", "gemini", "chatgpt", "perplexity", "claude"]
        lang_hint = "Answer in English"

    # 只保留有密钥的平台
    available = {
        pid: cfg for pid, cfg in PLATFORMS.items()
        if pid in candidate_keys and os.getenv(cfg["api_key_env"])
    }

    # 按候选顺序排序
    available = {
        pid: available[pid]
        for pid in candidate_keys
        if pid in available
    }
    # 模拟器是免费功能，限制最多3个平台，控制 token 叠加成本
    if len(available) > 3:
        available = dict(list(available.items())[:3])

    if not available:
        # 没有任何API密钥时降级演示
        return _simulate_demo(keyword, req.website, req.mode)

    # 向每个AI发问（只执行一次！）
    results = []
    debug_errors = []
    async with httpx.AsyncClient() as client:
        tasks = []
        for pid, cfg in available.items():
            tasks.append(_simulate_one(client, pid, cfg, keyword, req.website, lang_hint))
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    for (pid, cfg), result in zip(available.items(), raw_results):
        if isinstance(result, Exception):
            debug_errors.append(f"{cfg['label']}: {str(result)[:150]}")
            results.append({
                "platform": cfg["label"],
                "pid": pid,
                "mentioned": False,
                "answer_snippet": f"该平台查询失败：{str(result)[:100]}",
                "cited_urls": [],
                "your_site_found": False,
                "error": True,
            })
        else:
            results.append(result)

    # 汇总统计
    total = len(results)
    mentioned_count = sum(1 for r in results if r.get("mentioned"))
    your_site_found = any(r.get("your_site_found") for r in results)

    # 如果全部失败，返回错误信息方便调试
    if mentioned_count == 0 and debug_errors:
        return {
            "keyword": keyword, "website": req.website, "mode": req.mode,
            "results": results,
            "summary": {
                "total_platforms": total, "mentioned_count": 0,
                "mention_rate": 0, "your_site_found": False,
                "verdict": "所有平台查询失败，可能是 API 密钥问题：" + "; ".join(debug_errors[:2]),
            }
        }

    return {
        "keyword": keyword,
        "website": req.website,
        "mode": req.mode,
        "results": results,
        "summary": {
            "total_platforms": total,
            "mentioned_count": mentioned_count,
            "mention_rate": round(mentioned_count / total * 100) if total else 0,
            "your_site_found": your_site_found,
            "verdict": _get_verdict(mentioned_count, total, your_site_found, req.website),
        }
    }


async def _simulate_one(client, pid, cfg, keyword, website, lang_hint):
    """向单个AI发送关键词查询，返回结构化结果"""
    key = os.getenv(cfg["api_key_env"], "")
    if not key:
        raise RuntimeError("no key")

    prompt = f"{keyword}"
    messages = [{"role": "user", "content": prompt}]
    body = {"model": cfg["model"], "messages": messages, "temperature": 0.7, "max_tokens": 800}

    try:
        # Gemini 特殊格式
        if pid == "gemini":
            url = f"{cfg['url']}?key={key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            r = await client.post(url, json=payload, timeout=22)
            r.raise_for_status()
            answer = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        # Claude 特殊格式
        elif pid == "claude":
            r = await client.post(cfg["url"],
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": cfg["model"], "max_tokens": 800, "messages": messages},
                timeout=22)
            r.raise_for_status()
            answer = r.json()["content"][0]["text"]
        else:
            # OpenAI 兼容格式（ChatGPT/DeepSeek/Perplexity/通义/Kimi/豆包）
            r = await client.post(cfg["url"],
                headers={"Authorization": f"Bearer {key}"},
                json=body, timeout=22)
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"{cfg['label']} 查询失败: {str(e)[:100]}")

    answer_low = answer.lower()
    # 检测网站是否被引用
    your_site_found = False
    if website:
        site_clean = website.lower().replace("https://","").replace("http://","").replace("www.","").rstrip("/")
        your_site_found = site_clean in answer_low

    # 提取URL
    import re as _re
    cited_urls = list(set(_re.findall(r'https?://[^\s\)\]\"\'<>]+', answer)))[:5]

    # 截取前400字摘要
    snippet = answer[:400].strip()
    if len(answer) > 400:
        snippet += "…"

    return {
        "platform": cfg["label"],
        "pid": pid,
        "mentioned": True,
        "answer_snippet": snippet,
        "cited_urls": cited_urls,
        "your_site_found": your_site_found,
        "error": False,
    }


def _get_verdict(mentioned, total, site_found, website):
    if not total:
        return "暂无数据"
    rate = mentioned / total * 100
    if website and site_found:
        return "✅ 你的网站出现在 AI 回答里！继续优化保持领先"
    elif website and not site_found:
        return "❌ AI 回答里没有你的网站，你正在损失流量"
    elif rate >= 80:
        return "✅ AI 正在积极回答这个话题"
    else:
        return "⚠️ AI 对这个关键词覆盖有限"


def _simulate_demo(keyword, website, mode):
    """没有API密钥时返回演示数据，让商家看到功能"""
    return {
        "keyword": keyword,
        "website": website,
        "mode": mode,
        "demo": True,
        "results": [
            {"platform": "ChatGPT", "pid": "chatgpt", "mentioned": True,
             "answer_snippet": f"关于「{keyword}」，以下是一些主流推荐...(演示数据，配置API密钥后显示真实结果)",
             "cited_urls": [], "your_site_found": False, "error": False},
            {"platform": "DeepSeek", "pid": "deepseek", "mentioned": True,
             "answer_snippet": f"针对「{keyword}」的问题...(演示数据，配置API密钥后显示真实结果)",
             "cited_urls": [], "your_site_found": False, "error": False},
        ],
        "summary": {
            "total_platforms": 2, "mentioned_count": 2,
            "mention_rate": 100, "your_site_found": False,
            "verdict": "演示模式：配置 AI 密钥后显示真实数据"
        }
    }


# ----------------------------- 管理员接口 -----------------------------
# 用法：在浏览器访问 /admin?key=你设置的ADMIN_KEY
# 或用 POST /api/admin/upgrade 升级用户套餐

# 安全：必须在 Render 环境变量设置 ADMIN_KEY，否则管理功能禁用
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

def _check_admin(key: str):
    # 未配置 ADMIN_KEY 时，管理功能完全禁用（防止弱默认密钥被猜中）
    if not ADMIN_KEY:
        raise HTTPException(503, "管理功能未启用：请先在服务器配置 ADMIN_KEY 环境变量")
    # 用 hmac.compare_digest 防时序攻击
    if not hmac.compare_digest(key or "", ADMIN_KEY):
        raise HTTPException(403, "管理员密钥错误")


# ===== 用户行为追踪 =====
class TrackReq(BaseModel):
    event: str
    visitor_id: str = ""
    page: str = ""
    referrer: str = ""
    meta: str = ""

@app.post("/api/track")
async def track_event(req: TrackReq, request: Request,
                      session: Session = Depends(get_session)):
    """前端打点上报。无需登录（未登录访客也追踪）。
    带 token 的会解析出 user_id。"""
    uid = 0
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
            uid = int(payload.get("uid", 0))
        except Exception:
            uid = 0
    # 限制事件类型白名单，防污染
    allowed = {"page_view","register","click_check","click_upgrade","click_pay",
               "click_demo","monitor","content_gen","login"}
    ev = req.event if req.event in allowed else "other"
    try:
        session.add(TrackEvent(
            event=ev, user_id=uid, visitor_id=(req.visitor_id or "")[:40],
            page=(req.page or "")[:100], referrer=(req.referrer or "")[:200],
            meta=(req.meta or "")[:300]))
        session.commit()
    except Exception:
        session.rollback()
    return {"ok": True}


@app.get("/api/admin/operations")
def admin_operations(key: str, session: Session = Depends(get_session)):
    """运营数据看板：今日访问/注册/点击/转化 + 今日AI调用明细。"""
    _check_admin(key)
    from sqlalchemy import func as _f
    now = cn_now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def count_ev(event, since):
        return session.exec(
            select(_f.count(TrackEvent.id)).where(
                TrackEvent.event == event, TrackEvent.created_at >= since)
        ).one()

    def count_uv(since):
        # 独立访客数（按visitor_id去重）
        rows = session.exec(
            select(TrackEvent.visitor_id).where(
                TrackEvent.event == "page_view", TrackEvent.created_at >= since)
        ).all()
        return len(set(v for v in rows if v))

    # 今日各项
    today = {
        "uv": count_uv(today0),
        "pv": count_ev("page_view", today0),
        "register": count_ev("register", today0),
        "click_check": count_ev("click_check", today0),
        "click_upgrade": count_ev("click_upgrade", today0),
        "click_pay": count_ev("click_pay", today0),
        "monitor": count_ev("monitor", today0),
    }
    # 今日新注册用户（从User表精确统计）
    today_users = session.exec(
        select(User).where(User.created_at >= today0).order_by(User.created_at.desc())
    ).all()
    today["register_actual"] = len(today_users)

    # 今日AI调用明细（哪个用户调了什么）
    ai_logs = session.exec(
        select(ApiCallLog).where(ApiCallLog.created_at >= today0)
        .order_by(ApiCallLog.created_at.desc())
    ).all()
    # 按用户聚合
    user_ai = {}
    total_cost = 0.0
    total_calls = 0
    for log in ai_logs:
        u = user_ai.setdefault(log.user_id, {"calls": 0, "cost": 0.0, "scenes": {}})
        u["calls"] += log.calls
        u["cost"] += log.est_cost
        u["scenes"][log.scene] = u["scenes"].get(log.scene, 0) + log.calls
        total_cost += log.est_cost
        total_calls += log.calls
    # 补上用户邮箱
    ai_by_user = []
    for uid, data in sorted(user_ai.items(), key=lambda x: -x[1]["cost"]):
        u = session.get(User, uid) if uid else None
        ai_by_user.append({
            "user_id": uid,
            "email": u.email if u else ("匿名/系统" if uid == 0 else f"用户{uid}"),
            "plan": u.plan if u else "-",
            "calls": data["calls"],
            "cost": round(data["cost"], 2),
            "scenes": data["scenes"],
        })

    # 累计概览
    total_users = session.exec(select(_f.count(User.id))).one()
    paid_users = session.exec(
        select(_f.count(User.id)).where(User.plan != "trial")).one()

    # 转化漏斗（今日）
    funnel = {
        "visit": today["uv"] or today["pv"],
        "register": today["register_actual"],
        "monitor": today["monitor"],
        "pay": today["click_pay"],
    }

    # 最近7天注册趋势
    trend = []
    for i in range(6, -1, -1):
        day = (today0 - timedelta(days=i))
        day_end = day + timedelta(days=1)
        cnt = session.exec(
            select(_f.count(User.id)).where(
                User.created_at >= day, User.created_at < day_end)
        ).one()
        trend.append({"date": day.strftime("%m-%d"), "count": cnt})

    return {
        "today": today,
        "today_new_users": [
            {"email": u.email, "plan": u.plan,
             "time": u.created_at.strftime("%H:%M") if u.created_at else ""}
            for u in today_users[:20]
        ],
        "ai_today": {
            "total_calls": total_calls,
            "total_cost": round(total_cost, 2),
            "by_user": ai_by_user[:30],
        },
        "overview": {
            "total_users": total_users,
            "paid_users": paid_users,
            "conversion_rate": round(paid_users / total_users * 100, 1) if total_users else 0,
        },
        "funnel": funnel,
        "register_trend": trend,
    }


@app.get("/api/admin/user-journey")
def admin_user_journey(key: str, email: str = "", user_id: int = 0,
                       session: Session = Depends(get_session)):
    """单个用户的完整行为轨迹：访问→注册→做了什么→卡在哪。用于精准跟进。"""
    _check_admin(key)
    # 按邮箱或ID找用户
    user = None
    if email:
        user = session.exec(select(User).where(User.email == email)).first()
    elif user_id:
        user = session.get(User, user_id)
    if not user:
        return {"found": False, "msg": "未找到该用户"}

    # 1. 基本信息
    info = {
        "id": user.id, "email": user.email, "plan": user.plan,
        "registered": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
        "monitor_count": user.monitor_count or 0,
        "content_count": user.content_count or 0,
        "referred_by": user.referred_by or 0,
    }

    # 2. 行为事件时间线（TrackEvent）
    events = session.exec(
        select(TrackEvent).where(TrackEvent.user_id == user.id)
        .order_by(TrackEvent.created_at.asc())
    ).all()
    ev_label = {
        "page_view": "访问页面", "register": "注册账号", "login": "登录",
        "click_check": "点击免费检测", "click_upgrade": "点击升级",
        "click_pay": "点击支付", "click_demo": "点击预约演示",
        "monitor": "发起监测", "content_gen": "生成内容", "other": "其他操作",
    }
    timeline = [{
        "time": e.created_at.strftime("%m-%d %H:%M") if e.created_at else "",
        "event": ev_label.get(e.event, e.event),
        "raw": e.event, "meta": e.meta or "",
    } for e in events]

    # 3. 品牌和监测记录
    brands = session.exec(select(Brand).where(Brand.user_id == user.id)).all()
    brand_list = []
    for br in brands:
        reports = session.exec(
            select(Report).where(Report.brand_id == br.id)
            .order_by(Report.generated_at.desc())
        ).all()
        brand_list.append({
            "name": br.name, "industry": br.industry,
            "monitor_times": len(reports),
            "latest_rate": round(reports[0].mention_rate, 1) if reports else None,
            "last_monitor": reports[0].generated_at.strftime("%m-%d %H:%M") if reports and reports[0].generated_at else "",
        })

    # 4. AI调用记录
    ai_logs = session.exec(
        select(ApiCallLog).where(ApiCallLog.user_id == user.id)
    ).all()
    ai_total = sum(l.calls for l in ai_logs)
    ai_cost = round(sum(l.est_cost for l in ai_logs), 2)

    # 5. 订单
    orders = session.exec(select(Order).where(Order.user_id == user.id)).all()
    order_list = [{
        "plan": o.plan, "amount": o.amount,
        "status": o.status,
        "time": o.created_at.strftime("%m-%d %H:%M") if o.created_at else "",
    } for o in orders]

    # 6. 智能诊断"卡在哪"
    stuck = "未知"
    if not brands:
        stuck = "⚠️ 注册后未做任何监测——卡在「第一次成功」，建议引导做首次监测"
    elif not any(b["monitor_times"] for b in brand_list):
        stuck = "⚠️ 创建了品牌但没监测成功——可能遇到问题，值得主动联系"
    elif user.plan == "trial" and any(b["monitor_times"] for b in brand_list):
        stuck = "💡 已体验监测但未付费——高意向！可推送升级或案例"
    elif user.plan != "trial":
        stuck = "✅ 已付费用户——重点维护，防流失"
    else:
        stuck = "已注册，观察中"

    return {
        "found": True,
        "info": info,
        "timeline": timeline,
        "brands": brand_list,
        "ai": {"total_calls": ai_total, "total_cost": ai_cost},
        "orders": order_list,
        "diagnosis": stuck,
    }


@app.get("/api/admin/check-keys")
async def admin_check_keys(key: str):
    """
    密钥自检（管理员）：逐个测试每个 AI 平台的密钥能否真实调用。
    这是排查'数据全0'问题的最直接工具。
    """
    _check_admin(key)
    results = await check_all_keys()
    ok_count = sum(1 for r in results if r["ok"])
    configured = sum(1 for r in results if r["configured"])
    return {
        "results": results,
        "total": len(results),
        "configured": configured,
        "working": ok_count,
        "summary": f"{ok_count} 个平台密钥正常可用" if ok_count else "⚠️ 没有任何平台密钥可用，监测会全部失败！",
    }


@app.get("/api/admin/api-usage")
def admin_api_usage(key: str, days: int = 30, session: Session = Depends(get_session)):
    """
    真实 API 调用统计（管理员）。
    看实际消耗了多少次、成功率、各平台分布、估算花费。
    """
    _check_admin(key)
    from datetime import timedelta
    # 容错：表可能尚未创建或为空，出错时返回空统计而非崩溃
    try:
        since = cn_now() - timedelta(days=days)
        logs = session.exec(
            select(ApiCallLog).where(ApiCallLog.created_at >= since)
        ).all()
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return {
            "period_days": days, "total_calls": 0, "total_success": 0,
            "total_failed": 0, "success_rate": 0, "estimated_cost_rmb": 0,
            "monitor_runs": 0, "by_platform": {},
            "note": "暂无 API 调用记录。完成一次监测后即可看到消耗数据。",
        }

    total_calls = sum(l.calls for l in logs)
    total_success = sum(l.success for l in logs)
    total_failed = sum(l.failed for l in logs)
    total_cost = round(sum(l.est_cost for l in logs), 2)

    # 按平台聚合
    by_platform = {}
    for l in logs:
        p = l.platform or "unknown"
        if p not in by_platform:
            by_platform[p] = {"calls": 0, "success": 0, "failed": 0, "cost": 0.0}
        by_platform[p]["calls"] += l.calls
        by_platform[p]["success"] += l.success
        by_platform[p]["failed"] += l.failed
        by_platform[p]["cost"] += l.est_cost
    for p in by_platform:
        by_platform[p]["cost"] = round(by_platform[p]["cost"], 3)
        c = by_platform[p]["calls"]
        by_platform[p]["success_rate"] = round(100 * by_platform[p]["success"] / c) if c else 0

    # 按场景聚合（让你知道钱花在哪个功能上）
    SCENE_NAMES = {
        "monitor": "品牌监测", "extract": "品牌提取", "questions": "生成问题集",
        "content": "生成内容", "opportunity": "关键词分析", "check_keys": "密钥自检",
        "other": "其他",
    }
    by_scene = {}
    for l in logs:
        sc = getattr(l, "scene", None) or "other"
        name = SCENE_NAMES.get(sc, sc)
        if name not in by_scene:
            by_scene[name] = {"calls": 0, "cost": 0.0}
        by_scene[name]["calls"] += l.calls
        by_scene[name]["cost"] += l.est_cost
    for sc in by_scene:
        by_scene[sc]["cost"] = round(by_scene[sc]["cost"], 3)

    # 监测次数（去重日志条目近似）
    monitor_runs = len(set((l.user_id, l.brand_id, str(l.created_at)[:16]) for l in logs))

    return {
        "period_days": days,
        "total_calls": total_calls,
        "total_success": total_success,
        "total_failed": total_failed,
        "success_rate": round(100 * total_success / total_calls) if total_calls else 0,
        "estimated_cost_rmb": total_cost,
        "monitor_runs": monitor_runs,
        "by_platform": by_platform,
        "by_scene": by_scene,
        "note": "成本为估算值，真实扣费请以各 AI 平台官方账单为准。",
    }


@app.get("/api/admin/cost-estimate")
def admin_cost_estimate(key: str):
    """
    成本估算（管理员）：看各套餐配置大概烧多少 token 成本。
    用于定价决策，确保毛利。
    """
    _check_admin(key)
    # 各套餐的典型配置（问题数 × 平台数）
    scenarios = [
        {"name": "模拟器(免费,1次)", "questions": 1, "platforms": 3, "cost_level": "cheap"},
        {"name": "¥39.9体验版(30问)", "questions": 30, "platforms": 3, "cost_level": "cheap"},
        {"name": "基础版单次(50问)", "questions": 50, "platforms": 4, "cost_level": "cheap"},
        {"name": "基础版/月(4次)", "questions": 50, "platforms": 4, "cost_level": "cheap", "times": 4},
        {"name": "专业版/月(估20次)", "questions": 50, "platforms": 4, "cost_level": "cheap", "times": 20},
    ]
    results = []
    for s in scenarios:
        est = estimate_cost(s["questions"], s["platforms"], s["cost_level"])
        times = s.get("times", 1)
        monthly_cost = round(est["estimated_cost_rmb"] * times, 2)
        results.append({
            "scenario": s["name"],
            "calls_per_run": est["calls"],
            "cost_per_run": est["estimated_cost_rmb"],
            "times": times,
            "total_cost": monthly_cost,
        })
    # 套餐价格对照
    plan_prices = {"single": 99, "monthly": 599, "starter_trial": 39.9, "starter": 1980, "pro": 3980, "business": 9800}
    return {
        "scenarios": results,
        "plan_prices": plan_prices,
        "note": "成本为粗估(基于国产模型¥2/百万token)。实际因模型和问题长度而异。毛利=售价-成本。",
        "advice": "经济模式下成本极低，毛利率普遍80%+。专业版不限次需关注重度用户成本。",
    }


@app.get("/api/showcase")
def showcase():
    """
    首页案例展示。
    现在是精选示范案例,等你有真实种子客户后,把这里换成真实数据即可。
    诚实标注:示范案例需注明,不可冒充真实客户。
    """
    return {
        "stats": {
            "brands_checked": 1200,      # 已体检品牌数(可随真实增长更新)
            "platforms": 8,
            "avg_improvement": 34,        # 平均提及率提升
        },
        "cases": [
            {
                "industry": "养生茶 · 出海独立站",
                "before": 8, "after": 42,
                "days": 21,
                "story": "补齐了官网FAQ和产品结构化内容后,ChatGPT和Perplexity开始在'养生茶推荐'类问题里提到该品牌。",
                "quote": "以前问AI根本搜不到我们,现在能被推荐了,独立站咨询明显变多。",
            },
            {
                "industry": "便携充电器 · 跨境电商",
                "before": 15, "after": 58,
                "days": 30,
                "story": "针对竞品对比类问题创作了多篇真实测评向内容,在AI回答中的出现率显著提升。",
                "quote": "看到竞品被推荐而我们没有,很着急。做了内容优化后,差距追回来了。",
            },
            {
                "industry": "护肤品牌 · 国内DTC",
                "before": 5, "after": 38,
                "days": 28,
                "story": "通过知识库沉淀品牌卖点,批量生成符合DeepSeek、豆包引用偏好的内容。",
                "quote": "知识库建好后,生成的内容真的像我们自己写的,省了好多事。",
            },
        ],
        "is_demo": True,   # 标注为示范案例
    }


@app.get("/terms")
def terms_page():
    """服务条款 + 合规声明页面"""
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GEO雷达 · 服务条款与合规声明</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f8faff;color:#16182b;line-height:1.8;padding:20px}
.wrap{max-width:760px;margin:0 auto;background:#fff;border-radius:14px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
h1{font-size:24px;color:#4f46e5;margin-bottom:8px}
.sub{color:#5a5f73;font-size:14px;margin-bottom:24px}
h2{font-size:17px;margin:24px 0 10px;color:#16182b}
p{font-size:14px;color:#3a3f54;margin-bottom:10px}
.box{background:#f0fdf4;border:1px solid #a7f3d0;border-radius:10px;padding:16px;margin:16px 0}
.box.warn{background:#fffbeb;border-color:#fde68a}
a{color:#4f46e5}
.back{display:inline-block;margin-top:24px;color:#4f46e5;text-decoration:none}
</style></head><body><div class="wrap">
<h1>服务条款与合规声明</h1>
<p class="sub">GEO 雷达 · AI 能见度监测与内容优化平台 · 最后更新 2026年6月</p>

<div class="box">
<p style="font-weight:600;color:#065f46">✅ 我们承诺白帽优化</p>
<p style="margin:0">GEO 雷达只提供合法、合规的 AI 能见度监测与内容优化建议。我们不做、不教任何"黑帽"操作（如刷量、伪造、操纵 AI 输出、批量灌水）。我们帮助品牌通过<b>真实、优质的内容</b>提升被 AI 引用的概率。</p>
</div>

<h2>一、服务内容</h2>
<p>本平台提供:AI 平台能见度监测、品牌提及率分析、内容缺口诊断、内容创作建议、Schema 生成、关键词商机分析、增长追踪等工具。所有功能基于公开可用的 AI 平台数据和品牌自行提供的资料。</p>

<h2>二、效果说明（重要）</h2>
<div class="box warn">
<p style="margin:0">我们提供的是<b>优化工具和建议</b>,而非效果保证。AI 是否引用某个品牌取决于内容质量、平台算法、行业竞争等多种因素,<b>任何机构都无法保证"必定被 AI 推荐"</b>。本平台的提及率、月损失流量等数据为基于行业基准的<b>估算参考值</b>,不构成精确承诺。</p>
</div>

<h2>三、内容合规责任</h2>
<p>本平台生成的内容均为<b>草稿建议</b>,品牌方应在发布前自行审核,确保:</p>
<p>· 内容真实,不含虚假宣传<br>· 不使用"第一""最佳"等违反《广告法》的绝对化用语<br>· 食品、保健品、医疗等特殊行业不宣称疗效<br>· 不侵犯他人商标、著作权</p>
<p>因品牌方发布未经审核的内容产生的法律责任,由品牌方自行承担。</p>

<h2>四、数据与隐私</h2>
<p>我们仅收集为提供服务所必需的信息。品牌监测数据用于生成报告;行业大盘采用<b>匿名聚合</b>方式,不会泄露任何单个品牌的具体数据。AI 访客追踪仅统计公开的来源信息,不收集访客个人隐私。</p>

<h2>五、费用与退款</h2>
<p>具体套餐价格以平台展示为准。虚拟服务一经开通即时生效,如对服务有疑问,请联系客服微信 <b>jenly222</b> 协商。</p>

<h2>六、联系我们</h2>
<p>客服微信:<b>jenly222</b><br>如有任何合规、隐私或服务问题,欢迎随时联系。</p>

<a href="/" class="back">← 返回首页</a>
</div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/admin")
def admin_page():
    """手机/电脑都能访问的管理后台页面"""
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GEO雷达 管理后台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f4f6fb;color:#16182b;padding:16px}
.wrap{max-width:600px;margin:0 auto}
h1{font-size:20px;color:#4f46e5;margin-bottom:6px}
.sub{color:#5a5f73;font-size:13px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e7e8ef;border-radius:14px;padding:18px;margin-bottom:16px}
.card h2{font-size:15px;margin-bottom:14px}
label{font-size:13px;color:#5a5f73;display:block;margin-bottom:5px}
input,select{width:100%;padding:11px;border:1.5px solid #e7e8ef;border-radius:8px;font-size:15px;margin-bottom:12px;font-family:inherit}
button{width:100%;padding:12px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
.result{margin-top:12px;padding:12px;border-radius:8px;font-size:13px;display:none}
.result.ok{background:#d1fae5;color:#065f46}
.result.err{background:#fee2e2;color:#991b1b}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th{text-align:left;padding:8px;background:#f8faff;color:#5a5f73;font-size:12px}
td{padding:8px;border-bottom:1px solid #f0f0f0}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;background:#eef2ff;color:#3730a3}
.up-btn{width:auto;padding:4px 10px;font-size:12px}
</style></head><body><div class="wrap">
<h1>⚙️ GEO雷达 管理后台</h1>
<p class="sub">手机电脑都能用 · 给用户开通套餐</p>
<div class="card" id="loginCard">
<h2>🔑 输入管理员密钥</h2>
<input type="password" id="key" placeholder="你在Render设置的ADMIN_KEY">
<button onclick="login()">登录</button>
<div class="result" id="loginR"></div>
</div>
<div id="main" style="display:none">
<div class="card">
<h2>🚀 给用户开通套餐</h2>
<label>客户邮箱</label>
<input type="email" id="email" placeholder="客户注册邮箱">
<label>套餐</label>
<select id="plan">
<option value="single">单次版¥99（1次完整监测+作战包+3次内容）</option>
<option value="monthly">AI Growth Pro ¥599/月（无限监测+多平台内容+pSEO落地页）</option>
<option value="custom">定制版（价格面议：全平台+pSEO矩阵30页+代运营）</option>
<option value="trial">退回免费版</option>
<option value="starter">[旧]季付版¥1980</option>
<option value="pro">[旧]企业版¥3980</option>
<option value="business">[旧]旗舰版¥9800</option>
</select>
<button onclick="upgrade()">确认开通</button>
<div class="result" id="upR"></div>
</div>
<div class="card">
<h2>🔑 API 密钥自检 <button onclick="checkKeys()" style="width:auto;padding:4px 12px;font-size:12px;float:right">立即检测</button></h2>
<div style="font-size:13px;color:#888;margin-bottom:10px">点"立即检测"测试每个平台密钥能不能用。这是排查"监测数据全是0"的最快方法。</div>
<div id="keycheck">点"立即检测"开始</div>
</div>
<div class="card">
<h2>📈 运营数据（今日）<button onclick="loadOps()" style="width:auto;padding:4px 12px;font-size:12px;float:right">刷新</button></h2>
<div id="opsBox" style="margin-bottom:20px"><p style="color:#888">点刷新加载今日运营数据</p></div>
<h2>🔍 用户行为轨迹</h2>
<div style="margin-bottom:12px"><input id="journeyEmail" placeholder="输入用户邮箱查询轨迹" style="width:70%"><button onclick="loadJourney()" style="width:auto;padding:8px 16px;margin-left:8px">查询</button></div>
<div id="journeyBox" style="margin-bottom:20px"></div>
<h2>📊 API 消耗监控 <button onclick="loadUsage()" style="width:auto;padding:4px 12px;font-size:12px;float:right">刷新</button></h2>
<div id="usage">点刷新加载</div>
</div>
<div class="card">
<h2>👥 用户列表 <button onclick="loadUsers()" style="width:auto;padding:4px 12px;font-size:12px;float:right">刷新</button></h2>
<div id="users">点刷新加载</div>
</div>
</div>
<script>
let KEY='';
function show(id,msg,ok){var e=document.getElementById(id);e.textContent=msg;e.className='result '+(ok?'ok':'err');e.style.display='block';}
async function login(){
  KEY=document.getElementById('key').value.trim();
  if(!KEY){show('loginR','请输入密钥',false);return;}
  try{
    const r=await fetch('/api/admin/users?key='+encodeURIComponent(KEY));
    if(r.status===403){show('loginR','❌密钥错误',false);return;}
    if(!r.ok){show('loginR','❌连接失败',false);return;}
    document.getElementById('loginCard').style.display='none';
    document.getElementById('main').style.display='block';
    loadUsers();
    loadUsage();
  }catch(e){show('loginR','❌'+e.message,false);}
}

async function checkKeys(){
  document.getElementById('keycheck').innerHTML='⏳ 正在逐个测试各平台密钥，请稍候（约10-20秒）…';
  try{
    const r=await fetch('/api/admin/check-keys?key='+encodeURIComponent(KEY));
    if(!r.ok){ document.getElementById('keycheck').innerHTML='<span style="color:#b0524a">检测接口错误（'+r.status+'）。请确认已部署最新的 monitor.py 和 main.py。</span>'; return; }
    const d=await r.json();
    var html='<div style="font-size:14px;font-weight:700;margin-bottom:12px;padding:10px;border-radius:8px;background:'+(d.working>0?'#f0f5ee;color:#5a7d5a':'#fdf3f2;color:#b0524a')+'">'+d.summary+'</div>';
    html+='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="text-align:left;color:#888"><th style="padding:6px">平台</th><th>状态</th><th>说明</th></tr>';
    d.results.forEach(function(it){
      var color=it.ok?'#5a7d5a':(it.configured?'#b0524a':'#999');
      var icon=it.ok?'✅':(it.configured?'❌':'⚪');
      html+='<tr style="border-top:1px solid #eee"><td style="padding:8px 6px;font-weight:600">'+it.label+'<div style="font-size:11px;color:#aaa;font-weight:400">'+it.env_name+'</div></td><td style="color:'+color+';font-weight:600;white-space:nowrap">'+icon+' '+it.status+'</td><td style="font-size:12px;color:#666">'+it.detail+'</td></tr>';
    });
    html+='</table>';
    html+='<div style="font-size:12px;color:#888;margin-top:12px;line-height:1.6">💡 ⚪未配置=没填密钥；❌失败=密钥错了或没钱；✅正常=可用。只要有1个✅，监测就能跑。</div>';
    document.getElementById('keycheck').innerHTML=html;
  }catch(e){ document.getElementById('keycheck').innerHTML='<span style="color:#b0524a">检测失败：'+e.message+'</span>'; }
}

async function loadOps(){
  const box=document.getElementById("opsBox");
  box.innerHTML="<p style='color:#888'>加载中…</p>";
  try{
    const d=await fetch("/api/admin/operations?key="+encodeURIComponent(KEY)).then(r=>r.json());
    if(d.detail){ box.innerHTML="<p style='color:#e34'>"+d.detail+"</p>"; return; }
    const t=d.today, o=d.overview, f=d.funnel;
    let h="";
    // 今日核心指标卡
    h+="<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px'>";
    const cards=[
      ["访问 UV",t.uv,"#5a7d5a"],["浏览 PV",t.pv,"#5a7d5a"],
      ["注册",t.register_actual,"#c99a52"],["点免费检测",t.click_check,"#5a7d5a"],
      ["做监测",t.monitor,"#5a7d5a"],["点支付",t.click_pay,"#c96a5f"],
    ];
    cards.forEach(c=>{
      h+="<div style='background:#f7f5f0;border-radius:10px;padding:12px;text-align:center'><div style='font-size:26px;font-weight:800;color:"+c[2]+"'>"+c[1]+"</div><div style='font-size:12px;color:#888'>"+c[0]+"</div></div>";
    });
    h+="</div>";
    // 转化漏斗
    h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px;margin-bottom:16px'>";
    h+="<div style='font-weight:700;margin-bottom:10px'>🔻 今日转化漏斗</div>";
    const steps=[["访问",f.visit],["注册",f.register],["监测",f.monitor],["付费",f.pay]];
    steps.forEach((s,i)=>{
      const pct=steps[0][1]?Math.round(s[1]/steps[0][1]*100):0;
      h+="<div style='display:flex;align-items:center;gap:10px;margin-bottom:6px'><span style='width:50px;font-size:13px'>"+s[0]+"</span><div style='flex:1;background:#e8e3d8;border-radius:4px;height:22px;position:relative'><div style='width:"+Math.max(pct,3)+"%;background:#5a7d5a;height:100%;border-radius:4px'></div></div><span style='width:70px;font-size:13px;text-align:right'>"+s[1]+" ("+pct+"%)</span></div>";
    });
    h+="</div>";
    // 累计概览
    h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px;margin-bottom:16px'>";
    h+="<div style='font-weight:700;margin-bottom:8px'>📊 累计</div>";
    h+="<div style='display:flex;gap:20px;flex-wrap:wrap'><span>总用户 <b>"+o.total_users+"</b></span><span>付费 <b style='color:#c99a52'>"+o.paid_users+"</b></span><span>付费率 <b>"+o.conversion_rate+"%</b></span></div>";
    h+="</div>";
    // 今日新注册用户
    if(d.today_new_users && d.today_new_users.length){
      h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px;margin-bottom:16px'>";
      h+="<div style='font-weight:700;margin-bottom:8px'>🆕 今日新注册（"+d.today_new_users.length+"人）</div>";
      d.today_new_users.forEach(u=>{
        h+="<div style='font-size:13px;padding:4px 0;border-bottom:1px solid #eee'>"+u.time+"　"+u.email+"　<span style='color:#888'>"+u.plan+"</span></div>";
      });
      h+="</div>";
    }
    // 今日AI调用明细
    const ai=d.ai_today;
    h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px'>";
    h+="<div style='font-weight:700;margin-bottom:8px'>🤖 今日AI调用：共 "+ai.total_calls+" 次，成本 ¥"+ai.total_cost+"</div>";
    if(ai.by_user && ai.by_user.length){
      h+="<table style='width:100%;font-size:13px;border-collapse:collapse'><tr style='text-align:left;color:#888'><th style='padding:4px'>用户</th><th>套餐</th><th>调用</th><th>成本</th><th>场景</th></tr>";
      ai.by_user.forEach(u=>{
        const scenes=Object.entries(u.scenes||{}).map(([k,v])=>k+":"+v).join(" ");
        h+="<tr style='border-top:1px solid #eee'><td style='padding:6px 4px'>"+u.email+"</td><td>"+u.plan+"</td><td>"+u.calls+"</td><td>¥"+u.cost+"</td><td style='color:#888;font-size:11px'>"+scenes+"</td></tr>";
      });
      h+="</table>";
    } else { h+="<p style='color:#888;font-size:13px'>今日暂无AI调用</p>"; }
    h+="</div>";
    box.innerHTML=h;
  }catch(e){ box.innerHTML="<p style='color:#e34'>加载失败："+e.message+"</p>"; }
}

async function loadJourney(){
  const email=document.getElementById("journeyEmail").value.trim();
  const box=document.getElementById("journeyBox");
  if(!email){ box.innerHTML="<p style='color:#e34'>请输入邮箱</p>"; return; }
  box.innerHTML="<p style='color:#888'>查询中…</p>";
  try{
    const d=await fetch("/api/admin/user-journey?key="+encodeURIComponent(KEY)+"&email="+encodeURIComponent(email)).then(r=>r.json());
    if(!d.found){ box.innerHTML="<p style='color:#e34'>"+(d.msg||d.detail||"未找到")+"</p>"; return; }
    const i=d.info;
    let h="";
    // 诊断卡（最重要，先显示）
    h+="<div style='background:#fff8e1;border-radius:10px;padding:14px;margin-bottom:14px;border:1px solid #f0d890'>";
    h+="<div style='font-weight:700;margin-bottom:4px'>🎯 跟进诊断</div><div style='font-size:14px'>"+d.diagnosis+"</div></div>";
    // 基本信息
    h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px;margin-bottom:14px'>";
    h+="<div style='font-weight:700;margin-bottom:8px'>👤 "+i.email+"</div>";
    h+="<div style='font-size:13px;color:#555;display:flex;gap:16px;flex-wrap:wrap'>";
    h+="<span>套餐 <b>"+i.plan+"</b></span><span>注册 "+i.registered+"</span><span>监测 "+i.monitor_count+"次</span><span>内容 "+i.content_count+"次</span>";
    if(i.referred_by) h+="<span>推荐人ID "+i.referred_by+"</span>";
    h+="</div></div>";
    // AI消耗+订单
    h+="<div style='display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap'>";
    h+="<div style='flex:1;min-width:140px;background:#f7f5f0;border-radius:10px;padding:12px'><div style='font-size:12px;color:#888'>AI调用</div><div style='font-size:20px;font-weight:800'>"+d.ai.total_calls+"次</div><div style='font-size:12px;color:#888'>成本 ¥"+d.ai.total_cost+"</div></div>";
    h+="<div style='flex:1;min-width:140px;background:#f7f5f0;border-radius:10px;padding:12px'><div style='font-size:12px;color:#888'>订单</div><div style='font-size:20px;font-weight:800'>"+d.orders.length+"笔</div>";
    if(d.orders.length){ const paid=d.orders.filter(o=>o.status==='paid').length; h+="<div style='font-size:12px;color:#5a7d5a'>已付 "+paid+"笔</div>"; }
    h+="</div></div>";
    // 品牌
    if(d.brands.length){
      h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px;margin-bottom:14px'>";
      h+="<div style='font-weight:700;margin-bottom:8px'>📁 品牌（"+d.brands.length+"）</div>";
      d.brands.forEach(b=>{
        h+="<div style='font-size:13px;padding:5px 0;border-bottom:1px solid #eee'>"+b.name+" · "+b.industry+" · 监测"+b.monitor_times+"次";
        if(b.latest_rate!==null) h+=" · 提及率"+b.latest_rate+"%";
        h+="</div>";
      });
      h+="</div>";
    }
    // 行为时间线
    h+="<div style='background:#f7f5f0;border-radius:10px;padding:14px'>";
    h+="<div style='font-weight:700;margin-bottom:10px'>🕐 行为时间线（"+d.timeline.length+"条）</div>";
    if(d.timeline.length){
      d.timeline.slice(-30).reverse().forEach(t=>{
        h+="<div style='display:flex;gap:12px;font-size:13px;padding:5px 0;border-bottom:1px solid #eee'><span style='color:#888;width:90px;flex-shrink:0'>"+t.time+"</span><span>"+t.event+"</span></div>";
      });
    } else { h+="<p style='color:#888;font-size:13px'>暂无行为记录（埋点部署后才有）</p>"; }
    h+="</div>";
    box.innerHTML=h;
  }catch(e){ box.innerHTML="<p style='color:#e34'>查询失败："+e.message+"</p>"; }
}

async function loadUsage(){
  try{
    const r=await fetch('/api/admin/api-usage?key='+encodeURIComponent(KEY)+'&days=30');
    if(!r.ok){ document.getElementById('usage').innerHTML='<span style="color:#b0524a">接口返回错误（'+r.status+'）。可能是 database.py 未更新到最新版，请确认已部署含 ApiCallLog 表的版本。</span>'; return; }
    const d=await r.json();
    var pnames={deepseek:'DeepSeek',doubao:'豆包',qwen:'通义',kimi:'Kimi',wenxin:'文心',chatgpt:'ChatGPT',gemini:'Gemini',claude:'Claude',perplexity:'Perplexity'};
    var html='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px">';
    html+='<div style="background:#f7f4ec;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:800">'+d.total_calls+'</div><div style="font-size:12px;color:#888">总调用次数</div></div>';
    html+='<div style="background:#f7f4ec;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:800;color:'+(d.success_rate>=90?'#5a7d5a':'#b0524a')+'">'+d.success_rate+'%</div><div style="font-size:12px;color:#888">成功率</div></div>';
    html+='<div style="background:#f7f4ec;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:800;color:#b0524a">'+d.total_failed+'</div><div style="font-size:12px;color:#888">失败次数</div></div>';
    html+='<div style="background:#f7f4ec;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:800;color:#5a7d5a">¥'+d.estimated_cost_rmb+'</div><div style="font-size:12px;color:#888">估算成本</div></div>';
    html+='</div>';
    if(d.total_failed>0 && d.success_rate<90){
      html+='<div style="background:#fdf3f2;border:1px solid #e8c5c0;border-radius:8px;padding:10px 14px;font-size:13px;color:#b0524a;margin-bottom:14px">⚠️ 失败率偏高，可能是某个平台的 API 密钥失效或余额不足，请检查下方各平台明细</div>';
    }
    html+='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="text-align:left;color:#888"><th style="padding:6px">平台</th><th>调用</th><th>成功率</th><th>失败</th><th>成本</th></tr>';
    for(var p in d.by_platform){
      var s=d.by_platform[p];
      var rateColor=s.success_rate>=90?'#5a7d5a':'#b0524a';
      html+='<tr style="border-top:1px solid #eee"><td style="padding:8px 6px;font-weight:600">'+(pnames[p]||p)+'</td><td>'+s.calls+'</td><td style="color:'+rateColor+';font-weight:600">'+s.success_rate+'%</td><td>'+s.failed+'</td><td>¥'+s.cost+'</td></tr>';
    }
    html+='</table>';
    // 按功能场景花费（让你知道钱花在哪个功能上）
    if(d.by_scene && Object.keys(d.by_scene).length){
      html+='<div style="font-size:13px;font-weight:700;margin:18px 0 8px;color:#26221c">💰 钱花在哪个功能上</div>';
      html+='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="text-align:left;color:#888"><th style="padding:6px">功能</th><th>调用次数</th><th>估算花费</th></tr>';
      var scenes=Object.entries(d.by_scene).sort(function(a,b){return b[1].cost-a[1].cost;});
      scenes.forEach(function(e){
        html+='<tr style="border-top:1px solid #eee"><td style="padding:8px 6px;font-weight:600">'+e[0]+'</td><td>'+e[1].calls+'</td><td style="color:#b0524a;font-weight:600">¥'+e[1].cost+'</td></tr>';
      });
      html+='</table>';
    }
    html+='<div style="font-size:11px;color:#aaa;margin-top:12px">近30天 · '+d.note+'</div>';
    document.getElementById('usage').innerHTML=html;
  }catch(e){ document.getElementById('usage').innerHTML='<span style="color:#b0524a">加载失败：'+e.message+'</span>'; }
}
async function upgrade(){
  const email=document.getElementById('email').value.trim();
  const plan=document.getElementById('plan').value;
  if(!email){show('upR','请填邮箱',false);return;}
  try{
    const r=await fetch('/api/admin/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,plan,key:KEY})});
    const d=await r.json();
    if(!r.ok){show('upR','❌'+(d.detail||'失败'),false);return;}
    show('upR','✅ '+d.message,true);
    document.getElementById('email').value='';
    loadUsers();
  }catch(e){show('upR','❌'+e.message,false);}
}
const PN={trial:'免费',starter_trial:'¥39.9',starter:'基础',pro:'专业',business:'企业'};
async function loadUsers(){
  const t=document.getElementById('users');t.innerHTML='加载中…';
  try{
    const r=await fetch('/api/admin/users?key='+encodeURIComponent(KEY));
    const us=await r.json();
    if(!us.length){t.innerHTML='暂无用户';return;}
    t.innerHTML='<table><tr><th>邮箱</th><th>套餐</th><th>监测</th><th></th></tr>'+
      us.map(u=>'<tr><td>'+u.email+'</td><td><span class="badge">'+(PN[u.plan]||u.plan)+'</span></td><td>'+(u.monitor_count||0)+'</td><td><button class="up-btn" onclick="quick(\\''+u.email+'\\')">开通</button></td></tr>').join('')+'</table>';
  }catch(e){t.innerHTML='加载失败:'+e.message;}
}
function quick(email){document.getElementById('email').value=email;document.getElementById('email').scrollIntoView({behavior:'smooth'});}
</script></div></body></html>"""
    return HTMLResponse(content=html)

@app.get("/api/admin/users")
def admin_list_users(key: str, session: Session = Depends(get_session)):
    """列出所有用户（管理员用）"""
    _check_admin(key)
    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    return [{"id": u.id, "email": u.email, "plan": u.plan,
             "monitor_count": getattr(u, "monitor_count", 0),
             "created_at": u.created_at} for u in users]

class UpgradeReq(BaseModel):
    email: str
    plan: str   # trial / starter_trial / starter / pro / business
    key: str

@app.post("/api/admin/upgrade")
def admin_upgrade(req: UpgradeReq, session: Session = Depends(get_session)):
    """
    管理员给用户升级套餐。
    用法示例（用 curl 或 Postman）：
    POST /api/admin/upgrade
    {"email": "user@qq.com", "plan": "starter", "key": "geo-admin-2026"}
    """
    _check_admin(req.key)
    if req.plan not in PLANS:
        raise HTTPException(400, f"套餐不存在，可选：{list(PLANS.keys())}")
    email = req.email.strip().lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        raise HTTPException(404, f"用户 {email} 不存在")
    old_plan = user.plan
    user.plan = req.plan
    # 升级时重置监测次数
    user.monitor_count = 0
    session.add(user)

    # 分销佣金结算：如果这个用户是被推荐来的，给推荐人算佣金
    commission_info = ""
    if user.referred_by and req.plan in PLAN_PRICES:
        ref = session.exec(
            select(Referral).where(
                Referral.referrer_id == user.referred_by,
                Referral.referred_user_id == user.id,
            )
        ).first()
        if ref and ref.status != "paid":
            price = PLAN_PRICES.get(req.plan, 0)
            commission = round(price * COMMISSION_RATE, 2)
            ref.status = "paid"
            ref.commission = commission
            ref.paid_plan = req.plan
            ref.paid_at = datetime.utcnow()
            session.add(ref)
            commission_info = f"，推荐人获得佣金 ¥{commission}"

    session.commit()
    return {
        "success": True,
        "email": req.email,
        "old_plan": old_plan,
        "new_plan": req.plan,
        "plan_name": PLANS[req.plan]["name"],
        "message": f"✅ {req.email} 已升级为 {PLANS[req.plan]['name']}{commission_info}"
    }


# 托管前端(单文件 SPA)
# 用绝对路径计算 frontend 位置,兼容本地和 Render 等云平台
_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(_HERE, "..", "frontend")

# ===== 注册程序化建站 (pSEO) 动态渲染系统 =====
# 提供 /solutions/{slug} 行业方案页、/solutions 总览、/sitemap.xml、/api/pseo/lead
# 纯增量，不影响任何现有路由。
try:
    from pseo import register_pseo, register_baidu_push
    register_pseo(app)
    register_baidu_push(app)
except Exception as _e:
    import logging as _lg
    _lg.getLogger("uvicorn.error").warning(f"pSEO 模块加载失败（不影响主服务）: {_e}")

if os.path.isdir(_FRONTEND):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_FRONTEND, "index.html"))
    @app.get("/pricing")
    def pricing_page():
        return FileResponse(os.path.join(_FRONTEND, "pricing.html"))
    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")
