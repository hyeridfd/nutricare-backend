"""
registry.py  ─  직렬화 불가 객체 전역 저장소
=============================================
LangGraph MemorySaver는 state를 msgpack으로 직렬화합니다.
커스텀 클래스(FacilityConstraintAdapter, PatientProfile, pymoo Result 등)는
직렬화가 불가능하므로 state에 직접 넣으면 TypeError가 발생합니다.

해결책:
  - 직렬화 불가 객체 → 여기 _STORE에 보관
  - state에는 문자열 키만 저장
  - Agent 내부에서 get()으로 꺼내 사용
"""

from typing import Any

_STORE: dict[str, Any] = {}


def put(key: str, obj: Any) -> str:
    """객체를 저장하고 키를 반환합니다."""
    _STORE[key] = obj
    return key


def get(key: str) -> Any:
    """키로 객체를 가져옵니다."""
    if key not in _STORE:
        raise KeyError(f"[Registry] '{key}' 키를 찾을 수 없습니다.")
    return _STORE[key]


def has(key: str) -> bool:
    return key in _STORE


def clear():
    _STORE.clear()