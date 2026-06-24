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
                      User, Brand, Report, GeneratedContent)
from services.monitor import run_monitoring
from services.generator import generate_questions, generate_content, extract_brand_keywords
from services.knowledge import build_knowledge_base
from services.optimizer import diagnose_score, build_action_plan, compare_reports

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
    existing = session.exec(select(User).where(User.email == req.email)).first()
    if existing:
        raise HTTPException(400, "该邮箱已注册")
    user = User(
        email=req.email, password_hash=_hash_pw(req.password),
        plan="trial", trial_ends_at=datetime.utcnow() + timedelta(days=7),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"token": _make_token(user.id), "plan": user.plan,
            "plan_info": plan_of(user)}


@app.post("/api/login")
def login(req: RegisterReq, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.email == req.email)).first()
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


# ----------------------------- 工具 -----------------------------

def _owned_brand(brand_id: int, user: User, session: Session) -> Brand:
    brand = session.get(Brand, brand_id)
    if not brand or brand.user_id != user.id:
        raise HTTPException(404, "品牌不存在")
    return brand


@app.get("/api/health")
def health():
    configured = [p for p in ("OPENAI_API_KEY", "GEMINI_API_KEY",
                              "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY",
                              "DEEPSEEK_API_KEY")
                  if os.getenv(p)]
    return {"status": "ok", "ai_platforms_configured": len(configured)}


# ----------------------------- 管理员接口 -----------------------------
# 用法：在浏览器访问 /admin?key=你设置的ADMIN_KEY
# 或用 POST /api/admin/upgrade 升级用户套餐

ADMIN_KEY = os.getenv("ADMIN_KEY", "geo-admin-2026")

def _check_admin(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(403, "管理员密钥错误")

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
    user = session.exec(select(User).where(User.email == req.email)).first()
    if not user:
        raise HTTPException(404, f"用户 {req.email} 不存在")
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
