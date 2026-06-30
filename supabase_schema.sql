-- ════════════════════════════════════════════════════════════
-- NutriCare Supabase 스키마
-- ════════════════════════════════════════════════════════════
-- 기존 코드 매핑:
--   PatientProfile (patient_profile_final.py)      → patients
--   preference_weights.json                         → preference_scores
--   pool_preference_scores.json                     → pool_preference_scores
--   df_menu_records (meal_plan_agent.py 산출)       → meal_plans, meal_plan_slots
--   personal_menus / personalize_reasons            → personalized_swaps
--   serving_map (serving_agent.py)                  → servings
--   waste_log (waste_monitoring_agent.py)           → waste_logs
--   알림/처방 (AlertAgent, InterventionAgent)        → nutrition_alerts, interventions
--
-- Neo4j Aura와의 역할 분담:
--   Neo4j  = 식품/질환 지식그래프 (Food, Recipe, Disease, 관계) — 그대로 유지
--   Supabase = 운영 데이터 (환자, 산출된 식단, 잔반, 선호도, 알림) — 신규
-- ════════════════════════════════════════════════════════════

-- ── 0. 시설 ──────────────────────────────────────────────────
-- 여러 요양시설을 동시에 운영할 가능성을 대비해 facility_id로 전 테이블을 구분.
-- 단일 시설만 운영한다면 이 테이블은 1행만 있어도 무방.
create table facilities (
    id              uuid primary key default gen_random_uuid(),
    name            text not null,
    budget_per_meal numeric not null default 10000,
    created_at      timestamptz not null default now()
);

-- ── 1. 환자(입소자) ──────────────────────────────────────────
-- PatientProfile 클래스의 입력 필드 + 계산 필드(target_energy, constraint)를
-- 함께 저장. constraint는 매번 재계산 가능하지만, 캐시해두면 조회가 빠름.
create table patients (
    id                  uuid primary key default gen_random_uuid(),
    facility_id         uuid not null references facilities(id) on delete cascade,
    name                text not null,
    sex                 text not null check (sex in ('male', 'female')),
    age                 int  not null,
    height_cm           numeric,
    weight_kg           numeric,
    bmi                 numeric not null,
    waist_cm            numeric not null,

    -- 질환 목록 (배열). patient_profile_final.py의 diseases 리스트와 매핑.
    -- 예: ['고혈압', '당뇨병', '신장질환', '치매']
    diseases            text[] not null default '{}',
    kidney_type         text check (kidney_type in ('신장_투석', '신장질환', null)),

    -- 식사 형태
    meal_texture_rice   text not null default '밥',   -- 밥 | 죽
    meal_texture_side   text not null default '일반찬', -- 일반찬 | 다진찬 | 갈찬

    -- disease_type_label 캐시 (예: 'HM형', 'DHK형') — 조회·필터링용
    disease_type_label  text,

    -- 계산된 영양 기준 (merge_constraints 결과를 JSON으로 캐시)
    -- 구조: {"energy_min":500, "energy_max":800, "sodium_max":800,
    --        "protein_min":18, "boost_nutrients":["iron","vit_d",...], ...}
    nutrition_constraint jsonb,
    target_energy        numeric,

    active              boolean not null default true,  -- 퇴소 시 false
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create index idx_patients_facility on patients(facility_id) where active;

-- ── 2. 식단 설계 실행 기록 ────────────────────────────────────
-- 파이프라인을 1회 실행할 때마다 1행. NSGA-II 메타데이터와 HITL 상태 포함.
create table meal_plan_runs (
    id                  uuid primary key default gen_random_uuid(),
    facility_id         uuid not null references facilities(id) on delete cascade,

    -- 실행 상태: optimize 중 / 영양사 승인 대기 / 승인됨 / 반려·재최적화
    status              text not null default 'optimizing'
                        check (status in ('optimizing', 'pending_review', 'approving', 'approved', 'rejected')),

    diseases_targeted   text[] not null,   -- CandidateAgent에 넘긴 교집합 대상 질환
    diseases_excluded   text[] default '{}', -- INTERSECTION_EXCLUDED_DISEASES (예: 신장질환)

    -- NSGA-II 결과 메타데이터
    f1_violation        numeric,   -- 영양 위반도
    f2_budget_overrun    numeric,
    f3_diversity         numeric,
    f4_pattern_violation numeric,
    hypervolume          numeric,
    pareto_count         int,
    reoptimize_count     int default 0,

    -- HITL
    reviewed_by         text,      -- 승인한 영양사 이름/계정
    reviewed_at         timestamptz,
    review_action       text check (review_action in ('approve', 'reoptimize', 'revise', null)),

    created_at          timestamptz not null default now()
);

-- ── 3. 28일 식단표 (공통 1개) ─────────────────────────────────
-- meal_plan_agent.py의 df_menu_records 1행 = 1 meal_plan_slots 행.
-- 시설 전체 공통 식단이므로 patient_id 없음(개인화는 별도 테이블).
create table meal_plan_slots (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid not null references meal_plan_runs(id) on delete cascade,

    day_number      int not null,         -- 1~28
    meal_type       text not null check (meal_type in ('아침', '점심', '저녁')),

    rice            text not null,
    soup            text not null,
    main_dish       text not null,
    side_dish_1     text not null,
    side_dish_2     text not null,
    kimchi          text not null,

    energy_kcal     numeric,
    sodium_mg       numeric,
    protein_g       numeric,
    cost_won        numeric,

    -- 권장재료 매핑 (meal_plan_agent.py의 rec_summary 결과)
    recommended_menu_summary text,   -- "쌀밥(현미, 잡곡) / 된장국(된장)"
    recommended_menu_count   int default 0,

    unique (run_id, day_number, meal_type)
);
create index idx_slots_run on meal_plan_slots(run_id);

-- ── 4. 개인화 대체 (질환 위반 보정 + 선호도 보정) ───────────────
-- personalize_agent.py의 personal_menus + personalize_reasons 매핑.
-- 부찬1/부찬2만 교체 대상이므로 slot은 그 둘로 제한.
create table personalized_swaps (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid not null references meal_plan_runs(id) on delete cascade,
    patient_id      uuid not null references patients(id) on delete cascade,

    day_number      int not null,
    meal_type       text not null check (meal_type in ('아침', '점심', '저녁')),
    slot            text not null check (slot in ('부찬1', '부찬2')),

    original_menu   text not null,
    replaced_menu   text not null,

    -- 'disease' | 'preference' — report_agent.py의 구분 컬럼과 동일
    reason_type     text not null check (reason_type in ('disease', 'preference')),
    reason_detail   text not null,   -- "HM형 나트륨 초과 보정" 등
    serving_ratio   numeric,         -- 질환 보정 시 함께 적용된 ratio (선호도면 null)

    created_at      timestamptz not null default now()
);
create index idx_swaps_run_patient on personalized_swaps(run_id, patient_id);

-- ── 5. 개인별 배식량 ──────────────────────────────────────────
-- serving_agent.py의 serving_map 1건 = 1행.
create table servings (
    id                uuid primary key default gen_random_uuid(),
    run_id            uuid not null references meal_plan_runs(id) on delete cascade,
    patient_id        uuid not null references patients(id) on delete cascade,

    day_number        int not null,
    meal_type         text not null check (meal_type in ('아침', '점심', '저녁')),

    ratio             numeric not null,   -- 최종 ratio (BMI기반 × 위반보정)

    rice_g            numeric,
    soup_ml           numeric,
    main_dish_g       numeric,
    side_dish_1_g     numeric,
    side_dish_2_g     numeric,
    kimchi_g          numeric,

    expected_energy_kcal   numeric,
    expected_protein_g     numeric,
    expected_sodium_mg     numeric,
    expected_carb_g        numeric,

    energy_ok         boolean,
    protein_ok        boolean,
    sodium_ok          boolean,

    unique (run_id, patient_id, day_number, meal_type)
);
create index idx_servings_run_patient on servings(run_id, patient_id);

-- ── 6. 잔반 기록 ──────────────────────────────────────────────
-- waste_monitoring_agent.py의 waste_log 1건 = 1행. 실제 배식 후 입력되는 데이터.
create table waste_logs (
    id              uuid primary key default gen_random_uuid(),
    patient_id      uuid not null references patients(id) on delete cascade,
    run_id          uuid references meal_plan_runs(id) on delete set null,

    day_number      int not null,
    meal_type       text not null check (meal_type in ('아침', '점심', '저녁')),

    -- 슬롯별 잔반율(0.0=다 먹음, 1.0=전부 남김)
    rice_waste_rate         numeric check (rice_waste_rate between 0 and 1),
    soup_waste_rate         numeric check (soup_waste_rate between 0 and 1),
    main_dish_waste_rate    numeric check (main_dish_waste_rate between 0 and 1),
    side_dish_1_waste_rate  numeric check (side_dish_1_waste_rate between 0 and 1),
    side_dish_2_waste_rate  numeric check (side_dish_2_waste_rate between 0 and 1),
    kimchi_waste_rate       numeric check (kimchi_waste_rate between 0 and 1),

    recorded_at     timestamptz not null default now(),
    recorded_by     text  -- 입력한 요양보호사/영양사
);
create index idx_waste_patient on waste_logs(patient_id, recorded_at desc);

-- ── 7. 선호도 점수 ────────────────────────────────────────────
-- preference_weights.json(개인별) → preference_scores
-- pool_preference_scores.json(시설 전체) → pool_preference_scores
create table preference_scores (
    id              uuid primary key default gen_random_uuid(),
    patient_id      uuid not null references patients(id) on delete cascade,
    menu_name       text not null,
    score           numeric not null check (score between 0 and 1) default 0.7,
    updated_at      timestamptz not null default now(),
    unique (patient_id, menu_name)
);
create index idx_pref_patient on preference_scores(patient_id);

create table pool_preference_scores (
    id              uuid primary key default gen_random_uuid(),
    facility_id     uuid not null references facilities(id) on delete cascade,
    menu_name       text not null,
    score           numeric not null check (score between 0 and 1) default 0.7,
    updated_at      timestamptz not null default now(),
    unique (facility_id, menu_name)
);

-- ── 8. 영양 부족 알림 + 처방 ──────────────────────────────────
-- NutritionMonitorAgent / AlertAgent / InterventionAgent 산출물.
create table nutrition_alerts (
    id              uuid primary key default gen_random_uuid(),
    patient_id      uuid not null references patients(id) on delete cascade,

    nutrient        text not null,        -- "열량(kcal)", "단백질(g)" 등
    consecutive_days int not null,
    avg_intake       numeric not null,
    standard_value   numeric not null,
    deficit_rate     numeric not null,    -- 부족률 (%)

    status          text not null default 'open' check (status in ('open', 'sent', 'resolved')),
    detected_at     timestamptz not null default now(),
    sent_at         timestamptz
);

create table interventions (
    id              uuid primary key default gen_random_uuid(),
    alert_id        uuid not null references nutrition_alerts(id) on delete cascade,
    prescription_text text not null,   -- GPT 생성 처방 내용
    created_at      timestamptz not null default now()
);

-- ════════════════════════════════════════════════════════════
-- RLS (Row Level Security) — 기본 정책 예시
-- ════════════════════════════════════════════════════════════
-- 실제 운영 시 시설 직원 인증(Supabase Auth)과 연동해 facility_id 기준으로
-- 행 단위 접근 제어. 아래는 자리만 잡아둔 예시이며, 인증 설계가 끝난 뒤
-- 정책 세부 조건을 채워야 함.

alter table patients enable row level security;
alter table meal_plan_runs enable row level security;
alter table waste_logs enable row level security;
alter table nutrition_alerts enable row level security;

-- 예: 같은 시설 소속 직원만 해당 시설 환자 조회 가능
-- create policy "facility staff can view own patients"
--   on patients for select
--   using (facility_id = (select facility_id from staff where staff.user_id = auth.uid()));
