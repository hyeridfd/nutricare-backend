"""
routers/patients.py — 환자(입소자) CRUD
==========================================
patient_profile_final.py의 PatientProfile 로직(merge_constraints,
disease_type_label, calc_target_energy)을 그대로 활용해, 저장 시점에
nutrition_constraint/target_energy/disease_type_label을 계산해 캐시합니다.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from dataclasses import asdict

from app.services.db_clients import get_supabase
from app.services.patient_logic import (
    PatientProfile, Sex, KidneyType, build_patient_profile,
)

router = APIRouter()


class PatientCreate(BaseModel):
    facility_id: str
    name: str
    sex: str                      # "male" | "female"
    age: int
    height_cm: float
    weight_kg: float
    waist_cm: Optional[float] = None
    diseases: list[str] = []
    kidney_type: Optional[str] = None   # "신장_투석" | "신장질환"
    meal_texture_rice: str = "밥"
    meal_texture_side: str = "일반찬"


class PatientResponse(BaseModel):
    id: str
    name: str
    disease_type_label: str
    target_energy: float
    nutrition_constraint: dict


@router.get("")
def list_patients(facility_id: str, active_only: bool = True):
    sb = get_supabase()
    query = sb.table("patients").select("*").eq("facility_id", facility_id)
    if active_only:
        query = query.eq("active", True)
    result = query.execute()
    return result.data


@router.get("/{patient_id}")
def get_patient(patient_id: str):
    sb = get_supabase()
    result = sb.table("patients").select("*").eq("id", patient_id).single().execute()
    if not result.data:
        raise HTTPException(404, "환자를 찾을 수 없습니다.")
    return result.data


@router.post("", response_model=PatientResponse)
def create_patient(payload: PatientCreate):
    """
    PatientProfile 로직(merge_constraints, calc_target_energy,
    disease_type_label)을 그대로 적용해 계산 필드를 채운 뒤 Supabase에 저장.
    """
    sb = get_supabase()

    try:
        profile = build_patient_profile(
            name=payload.name,
            sex=Sex.MALE if payload.sex == "male" else Sex.FEMALE,
            age=payload.age,
            height_cm=payload.height_cm,
            weight_kg=payload.weight_kg,
            waist_cm=payload.waist_cm,
            diseases=payload.diseases,
            kidney_type=KidneyType(payload.kidney_type) if payload.kidney_type else None,
            meal_texture_rice=payload.meal_texture_rice,
            meal_texture_side=payload.meal_texture_side,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    row = {
        "facility_id":         payload.facility_id,
        "name":                profile.name,
        "sex":                 payload.sex,
        "age":                 profile.age,
        "height_cm":           payload.height_cm,
        "weight_kg":           payload.weight_kg,
        "bmi":                 profile.bmi,
        "waist_cm":            profile.waist_cm,
        "diseases":            profile.diseases,
        "kidney_type":         payload.kidney_type,
        "meal_texture_rice":   profile.meal_texture_rice,
        "meal_texture_side":   profile.meal_texture_side,
        "disease_type_label":  profile.disease_type_label,
        "nutrition_constraint": asdict(profile.constraint),
        "target_energy":       profile.target_energy,
    }

    result = sb.table("patients").insert(row).execute()
    saved = result.data[0]

    return PatientResponse(
        id=saved["id"],
        name=saved["name"],
        disease_type_label=saved["disease_type_label"],
        target_energy=saved["target_energy"],
        nutrition_constraint=saved["nutrition_constraint"],
    )


@router.patch("/{patient_id}")
def update_patient(patient_id: str, payload: PatientCreate):
    """
    환자 정보 수정 시 계산 필드(constraint, target_energy, label)도 재계산.
    질환이나 체중이 바뀌면 영양기준이 달라지므로 단순 필드 업데이트가 아니라
    create_patient와 동일하게 build_patient_profile을 다시 거침.
    """
    sb = get_supabase()

    try:
        profile = build_patient_profile(
            name=payload.name,
            sex=Sex.MALE if payload.sex == "male" else Sex.FEMALE,
            age=payload.age,
            height_cm=payload.height_cm,
            weight_kg=payload.weight_kg,
            waist_cm=payload.waist_cm,
            diseases=payload.diseases,
            kidney_type=KidneyType(payload.kidney_type) if payload.kidney_type else None,
            meal_texture_rice=payload.meal_texture_rice,
            meal_texture_side=payload.meal_texture_side,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    row = {
        "name":                profile.name,
        "sex":                 payload.sex,
        "age":                 profile.age,
        "height_cm":           payload.height_cm,
        "weight_kg":           payload.weight_kg,
        "bmi":                 profile.bmi,
        "waist_cm":            profile.waist_cm,
        "diseases":            profile.diseases,
        "kidney_type":         payload.kidney_type,
        "meal_texture_rice":   profile.meal_texture_rice,
        "meal_texture_side":   profile.meal_texture_side,
        "disease_type_label":  profile.disease_type_label,
        "nutrition_constraint": asdict(profile.constraint),
        "target_energy":       profile.target_energy,
        "updated_at":          "now()",
    }

    result = sb.table("patients").update(row).eq("id", patient_id).execute()
    if not result.data:
        raise HTTPException(404, "환자를 찾을 수 없습니다.")
    return result.data[0]


@router.delete("/{patient_id}")
def deactivate_patient(patient_id: str):
    """물리 삭제 대신 active=false 처리 (과거 식단/잔반 기록 보존)."""
    sb = get_supabase()
    result = sb.table("patients").update({"active": False}).eq("id", patient_id).execute()
    if not result.data:
        raise HTTPException(404, "환자를 찾을 수 없습니다.")
    return {"status": "deactivated", "id": patient_id}
