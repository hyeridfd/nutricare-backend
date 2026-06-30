"""
db_clients.py — Neo4j Aura + Supabase 클라이언트 관리
========================================================
역할 분담:
  Neo4j  = 식품/질환 지식그래프 조회 전용 (CandidateAgent, MealPlanAgent의
           권장재료 매핑 등). 읽기 위주.
  Supabase = 운영 데이터(환자, 산출된 식단, 잔반, 선호도, 알림) CRUD.
"""

import os
from langchain_neo4j import Neo4jGraph
from supabase import create_client, Client

_neo4j_graph: Neo4jGraph | None = None
_supabase: Client | None = None


def init_clients():
    global _neo4j_graph, _supabase

    _neo4j_graph = Neo4jGraph(
        url=os.environ["NEO4J_URI"],
        username=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )

    # SUPABASE_SERVICE_ROLE_KEY 사용 — 백엔드는 RLS를 우회하는 service_role로
    # 동작하고, 행 단위 접근 제어가 필요해지면 그때 anon key + RLS 정책으로 전환.
    _supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def close_clients():
    global _neo4j_graph, _supabase
    # Neo4jGraph/Supabase Client는 별도 close가 필요 없지만,
    # 추후 커넥션 풀을 직접 관리하게 되면 여기서 정리.
    _neo4j_graph = None
    _supabase = None


def get_neo4j() -> Neo4jGraph:
    if _neo4j_graph is None:
        raise RuntimeError("Neo4j client not initialized — init_clients()가 호출되었는지 확인하세요.")
    return _neo4j_graph


def get_supabase() -> Client:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized — init_clients()가 호출되었는지 확인하세요.")
    return _supabase
