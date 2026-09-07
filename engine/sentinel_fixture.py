#!/usr/bin/env python3
# -*- coding: utf-8 -*-
FIXTURE_VERSION = "v1.0"
FIXTURE_UPDATED = "2026-08-30"
"""
sentinel_fixture.py — sentinel_compute.py 판정 로직 회귀 픽스처
================================================================
목적
  체크섬(sentinel_compute.sha256.txt)이 잡지 못하는 두 가지를 잡는다.
    1) 전사 손상이 숫자 리터럴에 발생하여 py_compile을 통과한 경우
    2) 인터프리터·라이브러리 거동 변화로 같은 코드가 다른 값을 내는 경우
       (SSOT §2-⑧이 못박은 선형보간, §2-⑨의 비대칭 밴드가 실제로
        그대로 작동하는지를 값으로 확인한다)

기대값의 출처
  아래 기대값은 엔진의 현재 출력을 받아 적은 것이 아니라, SSOT
  §2-⑧(선형보간 백분위)·§2-⑨(비대칭 밴드 k=2/2)·§2-③(이탈일 +30일)의
  정의에서 손으로 도출한 값이다. 도출 과정은 각 테스트의 주석에 적었다.
  따라서 본 픽스처는 엔진이 SSOT와 어긋나는 순간 실패한다.

사용
  python3 sentinel_fixture.py          # 전체 검증, 실패 시 exit 1
  python3 sentinel_fixture.py -v       # 각 항목 통과 내역 출력

배치
  sentinel_compute.py 와 같은 디렉터리에 둔다.
"""

import sys
from datetime import date, timedelta

try:
    import sentinel_compute as E
except Exception as exc:  # noqa: BLE001
    print(f"FIXTURE FAIL: 엔진 임포트 실패 — {exc}")
    sys.exit(1)

VERBOSE = "-v" in sys.argv
FAILS = []


def check(name, got, want, note=""):
    if got == want:
        if VERBOSE:
            print(f"  ok   {name}: {got}")
    else:
        FAILS.append(f"{name}: 기대 {want} / 실제 {got}" + (f" ({note})" if note else ""))


def approx(name, got, want, tol=1e-9, note=""):
    if got is not None and abs(got - want) <= tol:
        if VERBOSE:
            print(f"  ok   {name}: {got}")
    else:
        FAILS.append(f"{name}: 기대 {want} / 실제 {got}" + (f" ({note})" if note else ""))


# ── T1. 임계 상수 (SSOT §1-1 / §1 표) ──────────────────────────────
def t1_constants():
    want = {
        "S4_VIX_LOW":  dict(series="VIXCLS",       window_y=1, direction="low",  p_on=10, p_off=20, k_on=2, k_off=2),
        "S4_VIX_HIGH": dict(series="VIXCLS",       window_y=1, direction="high", p_on=90, p_off=80, k_on=2, k_off=2),
        "S7_HY_OAS":   dict(series="BAMLH0A0HYM2", window_y=3, direction="low",  p_on=10, p_off=20, k_on=2, k_off=2),
        "S8_DXY_LVL":  dict(series="DTWEXBGS",     window_y=3, direction="high", p_on=90, p_off=80, k_on=2, k_off=2),
    }
    check("T1 SIGNALS 신호 집합", sorted(E.SIGNALS.keys()), sorted(want.keys()),
          "신호 추가·제거는 SSOT 정식 개정 사항")
    for k, v in want.items():
        check(f"T1 SIGNALS[{k}]", E.SIGNALS.get(k), v)
    check("T1 Z_SIGMA_WINDOW", E.Z_SIGMA_WINDOW, 30)
    check("T1 Z_THRESHOLD", E.Z_THRESHOLD, 2.0)
    check("T1 Z_VALID_DAYS", E.Z_VALID_DAYS, 30)


# ── T2. 백분위 선형보간 (SSOT §2-⑧) ────────────────────────────────
def t2_percentile():
    # vals = 10,20,...,110 (n=11). k = (n-1)*p/100.
    #   p=10 → k=1.0   → s[1]                       = 20
    #   p=20 → k=2.0   → s[2]                       = 30
    #   p=80 → k=8.0   → s[8]                       = 90
    #   p=90 → k=9.0   → s[9]                       = 100
    #   p=15 → k=1.5   → s[1] + (s[2]-s[1])*0.5     = 25   ← 보간이 실제로 일어나는지
    #   p=95 → k=9.5   → s[9] + (s[10]-s[9])*0.5    = 105
    vals = [110, 10, 60, 20, 90, 30, 100, 40, 70, 50, 80]  # 정렬 전 순서 무관 확인
    for p, want in ((10, 20), (20, 30), (80, 90), (90, 100), (15, 25), (95, 105)):
        approx(f"T2 percentile(p={p})", E.percentile(vals, p), want)


# ── T3. 비대칭 밴드 히스테리시스 (SSOT §2-⑨) ───────────────────────
def t3_scan_low():
    """
    시계열 설계 (n=200, 일자 = today-199 … today, 전량 1Y 윈도 내):
      t=1..198  값 = t          (1,2,3,…,198)
      t=199     값 = 0.5
      t=200     값 = 0.6
    정렬 멀티셋 = 0.5, 0.6, 1, 2, …, 198  → s[k] = k-1 (k>=2)
      p10: k=(200-1)*0.10=19.9 → s[19]=18, s[20]=19 → 18 + 1*0.9 = 18.9
      p20: k=(200-1)*0.20=39.8 → s[39]=38, s[40]=39 → 38 + 1*0.8 = 38.8
    하위측 판정을 시간순으로 따라가면
      t=1 값1 <18.9 → 충족 1회 / t=2 값2 → 충족 2회 → 활성확정 (k_on=2)
      t=19 값19 → 중립대(18.9~38.8) → 연속 리셋
      t=39 값39 >38.8 → 해제 1회 / t=40 값40 → 해제 2회 → 해제확정 (k_off=2)
      t=41..198 해제 상태 유지
      t=199 값0.5 → 충족 1회 / t=200 값0.6 → 충족 2회 → 활성확정
    최종 active=True, 이벤트 3건.
    """
    today = date.today()
    series = []
    for t in range(1, 199):
        series.append(((today - timedelta(days=200 - t)).isoformat(), float(t)))
    series.append(((today - timedelta(days=1)).isoformat(), 0.5))
    series.append((today.isoformat(), 0.6))

    cfg = dict(series="FIXTURE", window_y=1, direction="low", p_on=10, p_off=20, k_on=2, k_off=2)
    r = E.scan(series, cfg)

    approx("T3 cut_on(p10)", r["cut_on"], 18.9, tol=1e-6)
    approx("T3 cut_off(p20)", r["cut_off"], 38.8, tol=1e-6)
    check("T3 n", r["n"], 200)
    check("T3 최종 active", r["active"], True)
    check("T3 latest", tuple(r["latest"]), (today.isoformat(), 0.6))

    want_events = [
        ((today - timedelta(days=198)).isoformat(), "활성확정"),
        ((today - timedelta(days=160)).isoformat(), "해제확정"),
        (today.isoformat(), "활성확정"),
    ]
    check("T3 이벤트 시퀀스", [tuple(e) for e in r["events"]], want_events,
          "활성 2연속/해제 2연속/중립대 유지가 무너지면 여기서 갈린다")


def t3_scan_high():
    """
    상위측(§2-⑨ 상위행) 대칭 확인. 같은 시계열을 direction=high, p_on=90/p_off=80 로 본다.
      p90: k=199*0.90=179.1 → s[179]=178, s[180]=179 → 178.1
      p80: k=199*0.80=159.2 → s[159]=158, s[160]=159 → 158.2
      t=179 값179 >178.1 → 1회 / t=180 값180 → 2회 → 활성확정
      t=181..198 계속 충족 → 활성 유지
      t=199 값0.5 <158.2 → 1회 / t=200 값0.6 → 2회 → 해제확정
    최종 active=False.
    """
    today = date.today()
    series = [((today - timedelta(days=200 - t)).isoformat(), float(t)) for t in range(1, 199)]
    series.append(((today - timedelta(days=1)).isoformat(), 0.5))
    series.append((today.isoformat(), 0.6))

    cfg = dict(series="FIXTURE", window_y=1, direction="high", p_on=90, p_off=80, k_on=2, k_off=2)
    r = E.scan(series, cfg)

    approx("T4 cut_on(p90)", r["cut_on"], 178.1, tol=1e-6)
    approx("T4 cut_off(p80)", r["cut_off"], 158.2, tol=1e-6)
    check("T4 최종 active", r["active"], False)
    want_events = [
        ((today - timedelta(days=20)).isoformat(), "활성확정"),
        (today.isoformat(), "해제확정"),
    ]
    check("T4 이벤트 시퀀스", [tuple(e) for e in r["events"]], want_events)


# ── T5. 변화 분기 z-score 이벤트 (SSOT §2-③ / §1 S8) ───────────────
def t5_zscore():
    """
    설계: 90관측. 일간수익률을 +0.1% / -0.1% 로 교대시키면 30일 표준편차가
    0.001 근방으로 고정되고 모든 |z| ≈ 1 이 되어 이벤트가 생기지 않는다.
    마지막 관측만 +1.0% 로 주면 그 날만 z ≈ +10 으로 임계 2.0을 넘는다.
    따라서 유효 이벤트는 정확히 1건, 일자는 today, 만료는 today+30일,
    valid=True 여야 한다(§2-③).
    """
    today = date.today()
    prices, p = [], 100.0
    n = 90
    for i in range(n - 1):
        prices.append(p)
        p *= 1.001 if i % 2 == 0 else 0.999
    prices.append(prices[-1] * 1.01)   # 마지막 날만 +1%
    series = [((today - timedelta(days=n - 1 - i)).isoformat(), round(v, 6))
              for i, v in enumerate(prices)]

    evs = E.zscore_events(series)
    check("T5 이벤트 건수", len(evs), 1, "교대 수익률 구간에서 오탐이 나면 임계가 흔들린 것")
    if len(evs) == 1:
        e = evs[0]
        check("T5 이벤트 일자", e["date"], today.isoformat())
        check("T5 만료일(+30일)", e["expires"], (today + timedelta(days=30)).isoformat())
        check("T5 유효 여부", e["valid"], True)
        check("T5 z 부호·임계", e["z"] > 2.0, True, f"z={e.get('z')}")


def main():
    for fn in (t1_constants, t2_percentile, t3_scan_low, t3_scan_high, t5_zscore):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{fn.__name__} 실행 중 예외 — {exc}")

    engine_ver = getattr(E, "ENGINE_VERSION", "?")
    if FAILS:
        print(f"FIXTURE FAIL ({len(FAILS)}건) — 엔진 {engine_ver}")
        for f in FAILS:
            print(f"  - {f}")
        print("→ 프롬프트 [3](b) 실패 분기로 이행할 것. 판정을 확정하지 말 것.")
        return 1
    print(f"FIXTURE PASS — 엔진 {engine_ver} / 픽스처 {FIXTURE_VERSION} / 검사 5종")
    return 0


if __name__ == "__main__":
    sys.exit(main())
