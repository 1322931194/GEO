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
import secrets
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select
import jwt

from database import (engine, init_db, get_session, PLANS,
                      User, Brand, Report, GeneratedContent, AIVisit, IndustrySample)
from services.monitor import run_monitoring, PLATFORMS
from services.generator import generate_questions, generate_content, extract_brand_keywords
from services.knowledge import build_knowledge_base
from services.optimizer import diagnose_score, build_action_plan, compare_reports, estimate_monthly_loss
from services.keyword_opportunity import analyze_keyword_opportunities

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
app = FastAPI(title="GEO 雷达 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


# ----------------------------- 鉴权工具 -----------------------------

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _make_token(user_id: int) -> str:
    payload = {"uid": user_id, "exp": datetime.utcnow() + timedelta(days=30)}
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
    return user


def plan_of(user: User) -> dict:
    return PLANS.get(user.plan, PLANS["trial"])


# ----------------------------- 请求模型 -----------------------------

class RegisterReq(BaseModel):
    email: str
    password: str

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

@app.post("/api/register")
def register(req: RegisterReq, session: Session = Depends(get_session)):
    # 邮箱标准化：去空格、转小写，避免后续登录因大小写/空格不匹配
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "邮箱格式不正确")
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少需要6位")
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(400, "该邮箱已注册")
    user = User(
        email=email, password_hash=_hash_pw(req.password),
        plan="trial", trial_ends_at=datetime.utcnow() + timedelta(days=7),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"token": _make_token(user.id), "plan": user.plan,
            "plan_info": plan_of(user)}


@app.post("/api/login")
def login(req: RegisterReq, session: Session = Depends(get_session)):
    # 邮箱标准化，与注册保持一致
    email = req.email.strip().lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or user.password_hash != _hash_pw(req.password):
        raise HTTPException(401, "邮箱或密码错误")
    return {"token": _make_token(user.id), "plan": user.plan,
            "plan_info": plan_of(user)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"email": user.email, "plan": user.plan, "plan_info": plan_of(user),
            "trial_ends_at": user.trial_ends_at}


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
    return [{"id": b.id, "name": b.name, "industry": b.industry,
             "website": b.website} for b in brands]


@app.get("/api/brands/{brand_id}/keywords")
async def brand_keywords(brand_id: int, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """提取品牌关键词，让商家确认品牌特征理解是否正确，再生成问题。"""
    brand = _owned_brand(brand_id, user, session)
    result = await extract_brand_keywords(
        brand.name, brand.industry, brand.product, brand.brand_facts
    )
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
async def monitor(brand_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    brand = _owned_brand(brand_id, user, session)
    questions = [q["question"] for q in json.loads(brand.questions_json or "[]")]
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
    )

    # 更新监测次数
    user.monitor_count = user_count + 1
    session.add(user)

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

    session.commit()
    session.refresh(rec)
    return report.__dict__


@app.get("/api/brands/{brand_id}/reports")
def reports(brand_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    _owned_brand(brand_id, user, session)
    recs = session.exec(select(Report).where(Report.brand_id == brand_id)
                        .order_by(Report.generated_at.desc())).all()
    return [{"id": r.id, "generated_at": r.generated_at,
             "mention_rate": r.mention_rate,
             "gaps": json.loads(r.gaps_json),
             "platform_breakdown": json.loads(r.platform_breakdown_json),
             "competitor_share": json.loads(r.competitor_share_json),
             "source_count": r.source_count,
             "sample_note": r.sample_note} for r in recs]


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

    report = json.loads(latest.full_json)
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
    after = json.loads(recs[0].full_json)
    before = json.loads(recs[1].full_json)
    result = compare_reports(before, after)
    result["has_comparison"] = True
    result["before_at"] = recs[1].generated_at
    result["after_at"] = recs[0].generated_at
    return result


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
def track_visit(tid: str, ref: str = "", page: str = "",
                user_agent: str = Header(None),
                session: Session = Depends(get_session)):
    """
    接收追踪数据（公开接口，无需登录）。
    商家官网的追踪代码会调用这个接口上报访客来源。
    只记录来自AI平台的访客。
    """
    if not tid:
        return {"ok": False}
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
        return {
            "has_benchmark": False, "industry": industry_std,
            "sample_count": sample_count, "needed": MIN_SAMPLES, "my_rate": my_rate,
            "message": f"「{industry_std}」行业样本积累中，已收集 {sample_count} 个品牌，达到 {MIN_SAMPLES} 个后即可看到行业大盘和你的排名。",
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
async def gen_content(req: GenContentReq, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    brand = _owned_brand(req.brand_id, user, session)
    result = await generate_content(
        brand.name, req.gap_question, brand.product,
        content_type=req.content_type, brand_facts=brand.brand_facts,
    )
    gc = GeneratedContent(
        brand_id=brand.id, gap_question=req.gap_question,
        content_type=req.content_type, title=result.get("title", ""),
        body=result.get("body", ""), publish_tip=result.get("publish_tip", ""),
    )
    session.add(gc)
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


# ----------------------------- 工具 -----------------------------

def _owned_brand(brand_id: int, user: User, session: Session) -> Brand:
    brand = session.get(Brand, brand_id)
    if not brand or brand.user_id != user.id:
        raise HTTPException(404, "品牌不存在")
    return brand


@app.get("/simulator")
def simulator_page():
    """AI推荐模拟器独立页面，无需登录可直接访问"""
    from fastapi.responses import FileResponse
    import pathlib
    p = pathlib.Path(__file__).parent.parent / "frontend" / "simulator.html"
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
async def simulate(req: SimulateReq):
    """
    AI推荐模拟器：无需登录，输入关键词立刻查
    """
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

    # 候选平台列表（按优先级排，多放几个让客户感觉覆盖广）
    if req.mode == "domestic":
        candidate_keys = ["deepseek", "doubao", "qwen", "kimi", "wenxin"]
        lang_hint = "用中文回答"
    else:
        candidate_keys = ["chatgpt", "deepseek", "qwen", "perplexity", "gemini", "claude"]
        lang_hint = "Answer in English"

    # 只保留有密钥的平台
    available = {
        pid: cfg for pid, cfg in PLATFORMS.items()
        if pid in candidate_keys and os.getenv(cfg["api_key_env"])
    }

    # 按候选顺序排序，最多取4个（保证速度和覆盖感的平衡）
    available = {
        pid: available[pid]
        for pid in candidate_keys
        if pid in available
    }
    # 限制最多4个平台（速度和覆盖感的平衡）
    if len(available) > 4:
        available = dict(list(available.items())[:4])

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

ADMIN_KEY = os.getenv("ADMIN_KEY", "geo-admin-2026")

def _check_admin(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(403, "管理员密钥错误")

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
<option value="starter_trial">¥9.9体验版（1次监测）</option>
<option value="starter">基础版¥299/月</option>
<option value="pro">专业版¥899/月（不限次）</option>
<option value="business">企业版¥2999/月</option>
<option value="trial">退回免费试用</option>
</select>
<button onclick="upgrade()">确认开通</button>
<div class="result" id="upR"></div>
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
  }catch(e){show('loginR','❌'+e.message,false);}
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
const PN={trial:'免费',starter_trial:'¥9.9',starter:'基础',pro:'专业',business:'企业'};
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
    session.commit()
    return {
        "success": True,
        "email": req.email,
        "old_plan": old_plan,
        "new_plan": req.plan,
        "plan_name": PLANS[req.plan]["name"],
        "message": f"✅ {req.email} 已升级为 {PLANS[req.plan]['name']}"
    }


# 托管前端(单文件 SPA)
# 用绝对路径计算 frontend 位置,兼容本地和 Render 等云平台
_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(_HERE, "..", "frontend")
if os.path.isdir(_FRONTEND):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_FRONTEND, "index.html"))
    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")
