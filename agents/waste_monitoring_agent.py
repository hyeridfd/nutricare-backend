"""
waste_monitoring_agent.py  ─  잔반 모니터링 파이프라인 (최종)
"""

import os
import json
import registry
from datetime import datetime
from langgraph.graph import StateGraph, END
from state import MealPlanState

ALERT_DAYS    = 3      # 3일 연속 부족 시 알림
DEFICIT_RATIO = 0.8    # 권장량 80% 미만이면 "부족"

# 일일 기준값 (constraint 없을 때 fallback)
DAILY_TARGETS = {
    "energy":  1500.0,   # kcal/일
    "protein":   60.0,   # g/일
    "carb":     300.0,   # g/일
}


# ══════════════════════════════════════════════════════════════
# 1. 잔반 입력 Agent
# ══════════════════════════════════════════════════════════════
def plate_waste_input_agent(state: MealPlanState) -> dict:
    print("\n[PlateWasteInputAgent] 잔반 데이터 처리 시작...")

    # ── 진단 1: df_menu_records ──────────────────────
    df_records = state.get("df_menu_records")
    print(f"  [진단] df_menu_records: {type(df_records)} / "
          f"len={len(df_records) if df_records else 0}")
    
    # ── 진단 2: pool ─────────────────────────────────
    pool = state.get("pool") or {}
    total_pool = sum(len(v) for v in pool.values())
    print(f"  [진단] pool 카테고리: {list(pool.keys())} / 총 {total_pool}건")
    
    # ── 진단 3: serving_map ──────────────────────────
    serving_map = state.get("serving_map") or {}
    print(f"  [진단] serving_map 키 샘플: {list(serving_map.keys())[:3]}")
    
    # ── 진단 4: waste_log 샘플 ───────────────────────
    waste_log = state.get("waste_log") or []
    if waste_log:
        print(f"  [진단] waste_log[0]: {waste_log[0]}")

    import pandas as pd

    waste_log   = state.get("waste_log") or []
    serving_map = state.get("serving_map") or {}
    history     = dict(state.get("nutrition_history") or {})

    SLOTS   = ["밥","국","주찬","부찬1","부찬2","김치"]
    CAT_MAP = {"밥":"밥","국":"국","주찬":"주찬",
               "부찬1":"부찬","부찬2":"부찬","김치":"김치"}

    pool = state.get("pool") or {}
    pool_index = {
        (cat, m["menu_name"]): m
        for cat, menus in pool.items()
        for m in menus
    }

    df = None
    if state.get("df_menu_records"):
        df = pd.DataFrame(state["df_menu_records"],
                          columns=state["df_menu_columns"])

    for entry in waste_log:
        name    = entry["name"]
        day     = entry["일차"]
        meal    = entry["끼니"]
        key_str = f"{name}||{day}||{meal}"
        srv     = serving_map.get(key_str, {})

        menu_row = None
        if df is not None:
            rows = df[(df["일차"] == day) & (df["끼니"] == meal)]
            if not rows.empty:
                menu_row = rows.iloc[0]

        actual = {"energy": 0.0, "protein": 0.0, "sodium": 0.0, "carb": 0.0}

        if menu_row is not None:
            for slot in SLOTS:
                waste_rate  = entry.get(slot, 0.0)
                intake_rate = 1.0 - waste_rate
                cat         = CAT_MAP[slot]
                menu_name   = menu_row.get(slot, "")
                menu_info   = pool_index.get((cat, menu_name), {})
                slot_serve  = srv.get(slot, 0) or 0

                for nut in ["energy", "protein", "sodium", "carb"]:
                    per_100g = menu_info.get(nut, 0) or 0
                    actual[nut] += per_100g * (slot_serve * intake_rate) / 100

        record = {
            "date":    datetime.now().strftime("%Y-%m-%d"),
            "day":     day,
            "meal":    meal,
            # [추가 — 2026-07-01] 원본 waste_logs 행의 id를 그대로 실어 보냄.
            # pipeline_runner.py가 이 값을 nutrition_intake_logs의 고유키로
            # upsert에 사용함 — day_number만으로는 서로 다른 28일 주기(다른
            # 달에 실행된 run)의 "1일차"가 같은 값으로 충돌할 수 있어서,
            # 실제 잔반 기록 원본 행을 키로 삼는 게 안전함.
            "waste_log_id": entry.get("waste_log_id"),
            "energy":  round(actual["energy"],  1),
            "protein": round(actual["protein"], 1),
            "sodium":  round(actual["sodium"],  1),
            "carb":    round(actual["carb"],    1),
            "waste":   {s: entry.get(s, 0.0) for s in SLOTS},
            "menu" : {                                          # ← individual 추가
                        slot: menu_row.get(slot, "") if menu_row is not None else ""
                        for slot in SLOTS
                    },
        }

        if name not in history:
            history[name] = []
        history[name].append(record)

    # 진단 출력
    first_name = list(history.keys())[0] if history else None
    if first_name:
        print(f"  [진단] {first_name} 최근 3건:")
        for rec in history[first_name][:3]:
            print(f"    {rec['day']} {rec['meal']} | "
                  f"energy={rec['energy']} protein={rec['protein']} carb={rec['carb']}")

    print(f"[PlateWasteInputAgent] 완료 — {len(waste_log)}건 / {len(history)}명")
    return {
        "nutrition_history": history,
        "messages": [f"[PlateWasteInputAgent] {len(waste_log)}건 섭취량 기록 완료"],
    }


# ══════════════════════════════════════════════════════════════
# 2. 영양 모니터링 Agent
# ══════════════════════════════════════════════════════════════
def nutrition_monitor_agent(state: MealPlanState) -> dict:
    print("\n[NutritionMonitorAgent] 영양 상태 분석 시작...")

    history     = state.get("nutrition_history") or {}
    alert_queue = list(state.get("alert_queue") or [])
    patients    = registry.get(state["patients_key"]) if state.get("patients_key") else []
    constraint  = registry.get(state["constraint_key"]) if state.get("constraint_key") else None
    patient_map = {p.name: p for p in patients}

    # ── 일일 기준값 결정 ─────────────────────────────────────
    # FacilityConstraintAdapter 실제 속성 기준:
    #   energy  → daily_energy_min  (1500 kcal)
    #   protein → daily_protein_min (protein_min×3, 없으면 40g)
    #   carb    → daily_carb_min 없음 → fallback 150g (KDRIs 최솟값)
    daily_targets = {
        "energy":  getattr(constraint, "daily_energy_min",  DAILY_TARGETS["energy"])  if constraint else DAILY_TARGETS["energy"],
        "protein": getattr(constraint, "daily_protein_min", DAILY_TARGETS["protein"]) if constraint else DAILY_TARGETS["protein"],
        "carb":    DAILY_TARGETS["carb"],   # daily_carb_min 없음 → 고정 fallback
    }

    print(f"  일일 기준: energy={daily_targets['energy']:.0f}kcal | "
          f"protein={daily_targets['protein']:.0f}g | "
          f"carb={daily_targets['carb']:.0f}g")
    print(f"  부족 임계값(80%): energy={daily_targets['energy']*DEFICIT_RATIO:.0f} | "
          f"protein={daily_targets['protein']*DEFICIT_RATIO:.0f} | "
          f"carb={daily_targets['carb']*DEFICIT_RATIO:.0f}")

    new_alerts: list = []

    for name, records in history.items():
        if len(records) < ALERT_DAYS:
            print(f"  {name}: 기록 {len(records)}건 — 최소 {ALERT_DAYS}건 필요, 건너뜀")
            continue

        # 날짜별 일 합계 계산
        daily_totals: dict = {}
        for r in records:
            d = r.get("day", "unknown")
            if d not in daily_totals:
                daily_totals[d] = {"energy": 0.0, "protein": 0.0, "carb": 0.0}
            for nut in ["energy", "protein", "carb"]:
                daily_totals[d][nut] += r.get(nut, 0)

        print(f"\n  [{name}] 날짜별 일 합계:")
        for d, totals in list(daily_totals.items())[:5]:
            print(f"    {d}: energy={totals['energy']:.1f} | "
                  f"protein={totals['protein']:.1f} | "
                  f"carb={totals['carb']:.1f}")

        p = patient_map.get(name)

        for nut in ["energy", "protein", "carb"]:
            target    = daily_targets[nut]
            threshold = target * DEFICIT_RATIO

            # 기준 미달인 날 수
            deficit_days = sum(
                1 for totals in daily_totals.values()
                if totals.get(nut, 0) < threshold
            )

            print(f"    {nut}: 기준미달 {deficit_days}일 / 전체 {len(daily_totals)}일 "
                  f"(임계값 {threshold:.1f})")

            if deficit_days >= ALERT_DAYS:
                nut_label = {
                    "energy":  "열량(kcal)",
                    "protein": "단백질(g)",
                    "carb":    "탄수화물(g)",
                }.get(nut, nut)

                avg_intake = sum(
                    t.get(nut, 0) for t in daily_totals.values()
                ) / max(len(daily_totals), 1)

                alert = {
                    "name":        name,
                    "nutrient":    nut_label,
                    "days":        deficit_days,
                    "avg_intake":  round(avg_intake, 1),
                    "target":      round(target, 1),
                    "deficit_pct": round((1 - avg_intake / target) * 100, 1),
                    "disease":     getattr(p, "disease_type_label", "-") if p else "-",
                    "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "intervention": None,
                }
                new_alerts.append(alert)
                print(f"    ★ 알림 생성: {name} — {nut_label} {deficit_days}일 부족 "
                      f"(평균 {avg_intake:.1f} / 기준 {target:.1f})")

    all_alerts = alert_queue + new_alerts
    print(f"\n[NutritionMonitorAgent] 완료 — 신규 {len(new_alerts)}건 (누적 {len(all_alerts)}건)")
    return {
        "alert_queue": all_alerts,
        "messages":    [f"[NutritionMonitorAgent] 신규 알림 {len(new_alerts)}건 감지"],
    }


def route_after_monitor(state: MealPlanState) -> str:
    pending = [a for a in (state.get("alert_queue") or [])
               if a.get("intervention") is None]
    return "alert" if pending else END


# ══════════════════════════════════════════════════════════════
# 3. 알림 Agent
# ══════════════════════════════════════════════════════════════
def alert_agent(state: MealPlanState) -> dict:
    print("\n[AlertAgent] 알림 발송 시작...")
    alert_queue = state.get("alert_queue") or []
    pending     = [a for a in alert_queue if a.get("intervention") is None]

    sent = 0
    for alert in pending:
        if _send_kakao(_format_kakao_message(alert)):
            sent += 1
            print(f"  발송: {alert['name']} — {alert['nutrient']}")

    print(f"[AlertAgent] 완료 — {sent}/{len(pending)}건")
    return {"messages": [f"[AlertAgent] {sent}건 알림 발송 완료"]}


def _format_kakao_message(alert: dict) -> str:
    return (
        f"[영양 부족 알림]\n"
        f"입소자: {alert['name']} ({alert['disease']})\n"
        f"부족 영양소: {alert['nutrient']}\n"
        f"{alert['days']}일 연속 부족\n"
        f"일 평균 섭취: {alert['avg_intake']} / 기준: {alert['target']}\n"
        f"부족률: {alert['deficit_pct']}%\n"
        f"감지 시각: {alert['detected_at']}\n"
        f"처방 확인 후 배식량을 조정해 주세요."
    )


def _send_kakao(message: str) -> bool:
    webhook = os.getenv("KAKAO_WEBHOOK_URL", "")
    if webhook:
        import urllib.request
        try:
            payload = json.dumps({"text": message}).encode("utf-8")
            urllib.request.urlopen(
                urllib.request.Request(webhook, data=payload,
                                       headers={"Content-Type": "application/json"}),
                timeout=5
            )
            return True
        except Exception as e:
            print(f"  [카카오 발송 실패] {e}")
            return False
    print(f"\n{'='*50}\n[카카오톡 메시지 미리보기]\n{message}\n{'='*50}\n")
    return True


# ══════════════════════════════════════════════════════════════
# 4. 처방 생성 Agent
# ══════════════════════════════════════════════════════════════
def intervention_agent(state: MealPlanState) -> dict:
    from openai import OpenAI
    print("\n[InterventionAgent] 처방 생성 시작...")

    alert_queue = state.get("alert_queue") or []
    pending     = [a for a in alert_queue if a.get("intervention") is None]
    if not pending:
        return {"messages": ["[InterventionAgent] 처리할 알림 없음"]}

    client      = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    patients    = registry.get(state["patients_key"]) if state.get("patients_key") else []
    patient_map = {p.name: p for p in patients}
    updated     = list(alert_queue)

    for i, alert in enumerate(updated):
        if alert.get("intervention") is not None:
            continue
        p       = patient_map.get(alert["name"])
        disease = getattr(p, "disease_type_label", "-") if p else "-"
        prompt  = f"""
당신은 노인요양시설 전문 영양사입니다.
[입소자] {alert['name']} ({disease})
[부족 영양소] {alert['nutrient']} — {alert['days']}일 연속 부족
[평균 섭취] {alert['avg_intake']} / 기준 {alert['target']} (부족률 {alert['deficit_pct']}%)

아래를 간결하게 3~5문장으로 답해 주세요:
1. 부족 원인 추정
2. 즉시 식이 조정 방안 (대체 메뉴, 배식량)
3. 영양 보충 방법
4. 다음 끼니 배식 ratio 권고 (0.8~1.2)
"""
        try:
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300, temperature=0.3,
            )
            text = resp.choices[0].message.content.strip()
        except Exception as e:
            text = f"[GPT-4o 오류: {e}]"

        updated[i] = {**alert, "intervention": text}
        print(f"  처방: {alert['name']} — {alert['nutrient']}")
        print(f"  {text[:80]}...")

    print(f"[InterventionAgent] 완료 — {len(pending)}건")
    return {
        "alert_queue": updated,
        "messages":    [f"[InterventionAgent] {len(pending)}건 처방 생성 완료"],
    }


# ══════════════════════════════════════════════════════════════
# SubGraph 조립
# ══════════════════════════════════════════════════════════════
def build_waste_monitoring_subgraph():
    builder = StateGraph(MealPlanState)
    builder.add_node("plate_waste_input", plate_waste_input_agent)
    builder.add_node("nutrition_monitor", nutrition_monitor_agent)
    builder.add_node("alert",             alert_agent)
    builder.add_node("intervention",      intervention_agent)

    builder.set_entry_point("plate_waste_input")
    builder.add_edge("plate_waste_input", "nutrition_monitor")
    builder.add_conditional_edges(
        "nutrition_monitor", route_after_monitor,
        {"alert": "alert", END: END}
    )
    builder.add_edge("alert",        "intervention")
    builder.add_edge("intervention", END)
    return builder.compile()


waste_monitoring_subgraph = build_waste_monitoring_subgraph()