"""
main.py — NutriCare FastAPI 진입점
====================================
Render 배포 기준. Vercel(frontend)에서의 CORS 요청을 허용하고,
Neo4j Aura / Supabase 클라이언트를 앱 시작 시 1회 초기화합니다.
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()  # 로컬 .env 로드 (Render에서는 환경변수가 이미 설정되어 있어 무해함)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import patients, meal_plans, dashboard, waste, orders, auth
from app.services.db_clients import init_clients, close_clients


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시: Neo4j 드라이버 + Supabase 클라이언트 생성
    init_clients()
    yield
    # 앱 종료 시: 커넥션 정리
    close_clients()


app = FastAPI(
    title="NutriCare API",
    description="LangGraph 멀티에이전트 기반 노인요양시설 식단 설계 시스템",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ────────────────────────────────────────────────────
# Vercel 프리뷰 배포(매번 URL이 달라짐)까지 허용하려면 정규식 패턴을 씀.
# 프로덕션에서는 FRONTEND_ORIGIN 환경변수로 정확한 도메인을 추가.
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_origin_regex=r"https://.*\.vercel\.app",  # Vercel 프리뷰 URL 허용
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우터 등록 ─────────────────────────────────────────────
app.include_router(auth.router,       prefix="/api/auth",       tags=["auth"])
app.include_router(patients.router,   prefix="/api/patients",   tags=["patients"])
app.include_router(meal_plans.router, prefix="/api/meal-plans", tags=["meal-plans"])
app.include_router(dashboard.router,  prefix="/api/dashboard",  tags=["dashboard"])
app.include_router(waste.router,      prefix="/api/waste-logs", tags=["waste"])
app.include_router(orders.router,     prefix="/api/orders",     tags=["orders"])


@app.get("/")
def health_check():
    return {"status": "ok", "service": "nutricare-api"}


@app.get("/api/health")
def api_health_check():
    """Render의 헬스체크 엔드포인트로 사용 (배포 설정에서 지정)."""
    return {"status": "ok"}
