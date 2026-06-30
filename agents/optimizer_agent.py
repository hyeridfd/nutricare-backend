"""
optimizer_agent.py  ─  OptimizerAgent 노드 (registry 버전)
"""

import numpy as np
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.optimize import minimize
from pymoo.termination import get_termination
from pymoo.core.problem import Problem

import registry
from state import MealPlanState
import time

t0 = time.monotonic()
result = minimize(problem, algorithm,
                   termination=get_termination("n_gen", n_gen),
                   seed=42, verbose=True)
print(f"[OptimizerAgent] minimize() 소요: {time.monotonic()-t0:.1f}s")

DAILY_SLOTS = [
    ("아침_밥","밥"),  ("아침_국","국"),    ("아침_주찬","주찬"),
    ("아침_부찬1","부찬"),("아침_부찬2","부찬"),("아침_김치","김치"),
    ("점심_밥","밥"),  ("점심_국","국"),    ("점심_주찬","주찬"),
    ("점심_부찬1","부찬"),("점심_부찬2","부찬"),("점심_김치","김치"),
    ("저녁_밥","밥"),  ("저녁_국","국"),    ("저녁_주찬","주찬"),
    ("저녁_부찬1","부찬"),("저녁_부찬2","부찬"),("저녁_김치","김치"),
]
N_DAYS   = 28
N_SLOTS  = len(DAILY_SLOTS)
MEAL_SETS = {
    "아침": [0,1,2,3,4,5],
    "점심": [6,7,8,9,10,11],
    "저녁": [12,13,14,15,16,17],
}
WHITELIST = ["쌀밥", "배추김치"]


class MealPlanProblem(Problem):
    def __init__(self, pool, constraint, budget_per_meal):
        self.pool       = pool
        self.constraint = constraint
        self.budget     = budget_per_meal * N_DAYS * 3
        self.slot_sizes = [len(pool[cat]) for _, cat in DAILY_SLOTS]
        super().__init__(
            n_var=N_DAYS * N_SLOTS, n_obj=4,
            xl=np.zeros(N_DAYS * N_SLOTS, dtype=int),
            xu=np.array([s-1 for s in self.slot_sizes] * N_DAYS, dtype=int),
            vtype=int,
        )

    def _get_menu(self, day, slot, idx):
        _, cat = DAILY_SLOTS[slot]
        return self.pool[cat][int(idx) % len(self.pool[cat])]

    def _evaluate(self, X, out, *args, **kwargs):
        out["F"] = np.array([self._eval_one(c) for c in X])

    def _eval_one(self, chrom):
        all_menus = [self._get_menu(d, s, chrom[d*N_SLOTS+s])
                     for d in range(N_DAYS) for s in range(N_SLOTS)]
        return [
            self._nutrition_violation(all_menus),               # 1. 영양 (f1)
            max(0.0, sum(m["cost"] for m in all_menus) - self.budget) / self.budget, # 2. 예산 (f2)
            -self._preference_weighted_diversity(all_menus),    # 3. 만족도/다양성 (f3)
            self._pattern_violation(chrom, all_menus)           # 4. 통합 패턴 위반 (f4)
        ]

    def _nutrition_violation(self, menus, debug=False):
        c       = self.constraint
        meal_v  = 0.0
        daily_v = 0.0

        # ── [임시 진단] 항목별 위반 기여도 집계 ────────────────
        meal_field_v  = {} if debug else None
        daily_field_v = {} if debug else None
        # ── 진단 끝 ─────────────────────────────────────────────

        for day in range(N_DAYS):
            daily_sum = {k: 0.0 for k in
                ["energy","carb","sugar","fat","sodium",
                "sat_fat","potassium","fiber","vit_d"]}
            base      = day * N_SLOTS
            day_menus = menus[base:base+N_SLOTS]

            for _, slots in MEAL_SETS.items():
                mm = [day_menus[s] for s in slots]
                mn = {
                    "energy":    sum(m["energy"]    for m in mm),
                    "protein":   sum(m["protein"]   for m in mm),
                    "fat":       sum(m["fat"]        for m in mm),
                    "sugar":     sum(m["sugar"]      for m in mm),
                    "sat_fat":   sum(m["sat_fat"]    for m in mm),
                    "sodium":    sum(m["sodium"]     for m in mm),
                    "potassium": sum(m["potassium"]  for m in mm),
                    "fiber":     sum(m["fiber"]      for m in mm),
                    "carb":      sum(m["carb"]        for m in mm),
                }
                for k, val in mn.items():
                    lo = getattr(c, f"{k}_min", None)
                    hi = getattr(c, f"{k}_max", None)
                    if lo and val < lo:
                        contrib = (lo - val) / lo
                        meal_v += contrib
                        if debug:
                            meal_field_v[f"{k}_min"] = meal_field_v.get(f"{k}_min", 0.0) + contrib
                    if hi and val > hi:
                        contrib = (val - hi) / hi
                        meal_v += contrib
                        if debug:
                            meal_field_v[f"{k}_max"] = meal_field_v.get(f"{k}_max", 0.0) + contrib
                    daily_sum[k] = daily_sum.get(k, 0.0) + val

            for k, val in daily_sum.items():
                lo = getattr(c, f"daily_{k}_min", None)
                hi = getattr(c, f"daily_{k}_max", None)
                if lo and val < lo:
                    contrib = (lo - val) / lo
                    daily_v += contrib
                    if debug:
                        daily_field_v[f"daily_{k}_min"] = daily_field_v.get(f"daily_{k}_min", 0.0) + contrib
                if hi and val > hi:
                    contrib = (val - hi) / hi
                    daily_v += contrib
                    if debug:
                        daily_field_v[f"daily_{k}_max"] = daily_field_v.get(f"daily_{k}_max", 0.0) + contrib

        meal_score  = meal_v  / (N_DAYS * 3)
        daily_score = daily_v / N_DAYS

        if debug:
            print(f"\n  [진단] meal_score={meal_score:.4f} (가중치 0.6) | "
                  f"daily_score={daily_score:.4f} (가중치 0.4)")
            print(f"  [진단] 끼니 단위 위반 항목별 누적 기여도 (84끼니 합, 큰 순):")
            for field, v in sorted(meal_field_v.items(), key=lambda x: -x[1]):
                print(f"    {field}: {v:.2f}  (끼니당 평균 {v/(N_DAYS*3):.4f})")
            print(f"  [진단] 일별 위반 항목별 누적 기여도 (28일 합, 큰 순):")
            for field, v in sorted(daily_field_v.items(), key=lambda x: -x[1]):
                print(f"    {field}: {v:.2f}  (일당 평균 {v/N_DAYS:.4f})")

        return meal_score * 0.6 + daily_score * 0.4

    def _simpson_diversity(self, menus):
        names = [m["menu_name"] for m in menus]
        N = len(names)
        counts: dict = {}
        for n in names: counts[n] = counts.get(n, 0) + 1
        M = len(counts)
        if M <= 1: return 0.0
        return (1 - sum((c/N)**2 for c in counts.values())) / (1 - 1/M) * 100

    def _preference_weighted_diversity(self, menus):
        # 기존 simpson diversity + preference_score 혼합
        diversity = self._simpson_diversity(menus)
        pref_avg  = sum(m.get("preference_score", 0.7) for m in menus) / len(menus)
        return diversity * 0.7 + pref_avg * 100 * 0.3  # 가중 합산

    # def _same_side_dish_penalty(self, chrom):
    #     penalty = 0
    #     for day in range(N_DAYS):
    #         base = day * N_SLOTS
    #         for s1, s2 in [(3,4),(9,10),(15,16)]:
    #             if int(chrom[base+s1]) == int(chrom[base+s2]):
    #                 penalty += 1
    #     return penalty / (N_DAYS * 3)

    # def _carry_over_penalty(self, menus, look_back=1):
    #     penalty = total = 0
    #     for day in range(1, N_DAYS):
    #         cur  = [m["menu_name"] for m in menus[day*N_SLOTS:(day+1)*N_SLOTS]]
    #         past = [m["menu_name"] for m in
    #                 menus[max(0,day-look_back)*N_SLOTS:day*N_SLOTS]]
    #         for name in cur:
    #             if name in WHITELIST: continue
    #             if name in past: penalty += 1
    #             total += 1
    #     return penalty / total if total else 0.0

    # ← 여기에 추가
    def _pattern_violation(self, chrom, menus):
        """
        부찬 중복 + 연속 배식 페널티 통합
        """
        # 1. 동일 끼니 내 부찬1 == 부찬2
        side_dish_p = 0
        for day in range(N_DAYS):
            base = day * N_SLOTS
            for s1, s2 in [(3,4),(9,10),(15,16)]:
                if int(chrom[base+s1]) == int(chrom[base+s2]):
                    side_dish_p += 1
        side_score = side_dish_p / (N_DAYS * 3)

        # 2. 2주 간격 연속 배식 중복
        carry_over_p = 0
        total_items  = 0
        look_back    = 13   # 14일 범위

        for day in range(1, N_DAYS):
            cur  = [m["menu_name"] for m in menus[day*N_SLOTS:(day+1)*N_SLOTS]]
            past = [m["menu_name"] for m in
                    menus[max(0, day-look_back)*N_SLOTS:day*N_SLOTS]]
            for name in cur:
                if name in WHITELIST: continue
                if name in past: carry_over_p += 1
                total_items += 1

        carry_score = carry_over_p / total_items if total_items else 0.0

        return (side_score + carry_score) / 2

def _evaluate_pareto(F: np.ndarray) -> dict:
    from pymoo.indicators.hv import HV

    n_obj = F.shape[1]  # ← 실제 목적함수 수 자동 감지

    # n_obj에 맞게 ref_point 동적 생성
    ref_defaults = [2.0, 1.1, 0.0, 1.1, 1.1]  # 최대 5개 대비
    ref_point = np.array(ref_defaults[:n_obj])

    print(f"  [평가] F shape: {F.shape} | ref_point: {ref_point}")

    dominated = np.all(F <= ref_point, axis=1)
    F_valid   = F[dominated]

    if len(F_valid) == 0:
        print("  [경고] ref_point보다 나쁜 해만 존재 — HV=0")
        hv_val = 0.0
    else:
        hv_val = HV(ref_point=ref_point)(F_valid)

    metrics = {
        "hypervolume":  round(hv_val, 6),
        "pareto_count": len(F),
        "f1_min":  round(float(F[:, 0].min()), 4),
        "f2_min":  round(float(F[:, 1].min()), 4),
        "f3_max":  round(float(-F[:, 2].max()), 2),
        "f4_min":  round(float(F[:, 3].min()), 4),
        "f1_pass_rate": round(float((F[:, 0] <= 0.3).sum() / len(F) * 100), 1),
    }

    print(f"\n[Pareto Front 품질 평가]")
    print(f"  Hypervolume:    {metrics['hypervolume']:.6f}  ← 클수록 좋음")
    print(f"  Pareto 해 수:   {metrics['pareto_count']}개")
    print(f"  f1 영양위반:    {metrics['f1_min']:.4f}  (목표: ≤ 0.3)")
    print(f"  f2 예산초과:    {metrics['f2_min']:.4f}  (목표: = 0.0)")
    print(f"  f3 다양성:      {metrics['f3_max']:.2f}   (목표: ≥ 80.0)")
    print(f"  f4 패턴위반:    {metrics['f4_min']:.4f}  (목표: ≤ 0.1)")
    print(f"  f1 통과율:      {metrics['f1_pass_rate']}%")

    return metrics


def optimizer_agent(state: MealPlanState) -> dict:
    # ── registry에서 직렬화 불가 객체 꺼내기 ─────────────────
    constraint = registry.get(state["constraint_key"])

    count    = state.get("violation_count", 0)
    pop_size = min(80 + count * 20, 150)
    n_gen    = min(60 + count * 15, 90)

    print(f"\n[OptimizerAgent] 최적화 시작 (시도 #{count+1} | pop={pop_size} | gen={n_gen})")

    problem   = MealPlanProblem(state["pool"], constraint, state["budget_per_meal"])
    algorithm = NSGA2(pop_size=pop_size, eliminate_duplicates=True)
    result    = minimize(problem, algorithm,
                         termination=get_termination("n_gen", n_gen),
                         seed=42, verbose=True)

    f1_min = result.F[:, 0].min()
    print(f"[OptimizerAgent] 완료 — Pareto {len(result.X)}개 | f1={f1_min:.4f}")

    # ── [임시 진단] f1이 가장 낮은 해(best)로 항목별 위반 기여도 분석 ──
    best_idx   = result.F[:, 0].argmin()
    best_chrom = result.X[best_idx]
    best_menus = [
        problem._get_menu(d, s, best_chrom[d*N_SLOTS+s])
        for d in range(N_DAYS) for s in range(N_SLOTS)
    ]
    # 항목별 위반 기여도 진단이 필요하면 debug=True로 변경
    problem._nutrition_violation(best_menus, debug=False)
    # ── 진단 끝 ────────────────────────────────────────────────

    # ── Pareto Front 품질 평가 ────────────────────────────────
    pareto_metrics = _evaluate_pareto(result.F)

    # ── pymoo Result도 registry에 저장 ───────────────────────
    result_key = f"nsga_result_{count}"
    registry.put(result_key, result)

    return {
        "nsga_result_key": result_key,
        "violation_count": count,
        "messages": [
            f"[OptimizerAgent] 시도 #{count+1} | f1={f1_min:.4f} "
            f"| HV={pareto_metrics['hypervolume']:.4f} "
            f"| Pareto={pareto_metrics['pareto_count']}개 "
            f"| f1통과={pareto_metrics['f1_pass_rate']}%"
        ],
    }


def get_menu(pool: dict, slot_idx: int, chrom_val: int) -> dict:
    _, cat = DAILY_SLOTS[slot_idx]
    return pool[cat][int(chrom_val) % len(pool[cat])]