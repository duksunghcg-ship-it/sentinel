#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_sentinel.py v0.2 (2026-09-07) — 비정량 신호를 latest.json에 포함 — GitHub Actions용 실행기
  1) sentinel_compute.py(v1.4, 판정 로직 무변경)를 임포트해 실행한다
  2) state/latest.json 을 갱신하고 state/history.csv 에 그날 판정을 추가한다(추가 전용)
  3) state/nonquant.json(사람이 갱신하는 비정량 5신호)과 합쳐 잠정 카운트·단계를 낸다
  4) 직전 latest.json 과 비교해 알림 사유가 있으면 GITHUB_OUTPUT 에 alert_title/alert_body 를 쓴다
사용: python engine/run_sentinel.py --out state --cache cache [--since YYYY-MM-DD]
"""
import argparse, csv, json, os, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sentinel_compute as E  # noqa: E402

# SSOT §2-④ 단계 고정표
STAGE = [(0, 2, "정상"), (3, 4, "거품 경계"), (5, 6, "거품 후기"), (7, 8, "폭락 임박")]
QUANT_KEY = {"S4": "S4_VIX_LOW", "S7": "S7_HY_OAS", "S8": "S8_DXY_LVL"}


def stage_of(n):
    for lo, hi, name in STAGE:
        if lo <= n <= hi:
            return name
    return "?"


def load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def last_change_dates(hist_path):
    """history.csv 에서 신호별 마지막 상태 변화일. --since 자동 산출과 since 필드에 쓴다."""
    since, prev = {}, {}
    if not os.path.exists(hist_path):
        return since
    with open(hist_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k, st = row["signal"], row["state"]
            if st == "carry":
                continue
            if k in prev and prev[k] != st:
                since[k] = row["date"]
            prev[k] = st
    return since


def headroom(r):
    """활성이면 해제컷까지, 비활성이면 활성컷까지의 거리(%)."""
    v = r["latest"][1]
    tgt = r["cut_off"] if r["active"] else r["cut_on"]
    return round(abs(tgt - v) / abs(v) * 100, 1) if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="state")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--since")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    E.OUT = os.path.abspath(a.cache)  # 엔진 캐시 위치를 저장소 cache/ 로 고정

    prev = load_json(os.path.join(a.out, "latest.json"), {})
    prev_sig = prev.get("signals", {})
    hist_path = os.path.join(a.out, "history.csv")
    changes = last_change_dates(hist_path)
    since = a.since or (min(changes.values()) if changes
                        else (date.today() - timedelta(days=90)).isoformat())

    out = E.run(since)
    today = out["asof"]

    # ── 신호별 정리 ─────────────────────────────────────────────
    signals, quant_active = {}, {}
    for k, r in out["signals"].items():
        pk = prev_sig.get(k, {})
        if r.get("verdict") != "확정":
            # SSOT §1-B B-3 판정유보: 직전 확정 상태 승계
            inherited = pk.get("inherited_state") or pk.get("state")
            signals[k] = dict(state="carry", inherited_state=inherited,
                              reason=str(r.get("error", ""))[:120])
            quant_active[k] = inherited == "active"
            continue
        st = "active" if r["active"] else "inactive"
        quant_active[k] = r["active"]
        since_k = pk.get("since")
        if pk.get("state") not in (None, "carry", st):
            since_k = today                      # 오늘 상태가 바뀜
        if since_k is None:
            since_k = r["events"][-1][0] if r.get("events") else None
        signals[k] = dict(
            value=r["latest"][1], as_of=r["latest"][0], cut_on=r["cut_on"], cut_off=r["cut_off"],
            state=st, since=since_k, headroom_pct=headroom(r),
            tier=r.get("tier"), source=r.get("source"),
        )
        if k == "S8_DXY_LVL":
            evs = [e for e in r.get("change_events", []) if e["valid"]]
            signals["S8_DXY_CHG"] = dict(state="active" if evs else "inactive", events=evs,
                                         expires=max((e["expires"] for e in evs), default=None))

    # ── 카운트: S4 두 방향은 1카운트, S8은 레벨 OR 변화 ───────────
    q = {
        "S4": bool(quant_active.get("S4_VIX_LOW") or quant_active.get("S4_VIX_HIGH")),
        "S7": bool(quant_active.get("S7_HY_OAS")),
        "S8": bool(quant_active.get("S8_DXY_LVL") or signals.get("S8_DXY_CHG", {}).get("state") == "active"),
    }
    live = sum(1 for s, key in QUANT_KEY.items() if out["signals"].get(key, {}).get("verdict") == "확정")
    count = sum(1 for v in q.values() if v)
    nonq = load_json(os.path.join(a.out, "nonquant.json"), {"signals": {}})
    for k, v in nonq.get("signals", {}).items():
        if v.get("state") == "active":
            count += 1
        if v.get("state") in ("active", "inactive"):
            live += 1

    chg = signals.get("S8_DXY_CHG", {})
    latest = dict(
        as_of=today, engine_version=out["engine"], since_used=since,
        count=count, denominator=8, denominator_live=live, stage=stage_of(count),
        stage_note=(f"S8 [변화] 만료 {chg['expires']} — 신규 이탈 없으면 카운트 1 감소" if chg.get("expires") else ""),
        quant_active=q, nonquant_as_of=nonq.get("as_of"), nonquant=nonq.get("signals", {}), signals=signals,
        fetch_log=out["fetch_log"], primary_ok=out["primary_ok"], degraded=out["degraded"],
        pending=nonq.get("pending", []),
    )
    with open(os.path.join(a.out, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # ── history.csv 추가 전용 ────────────────────────────────────
    new_file = not os.path.exists(hist_path)
    with open(hist_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new_file:
            w.writerow(["date", "signal", "value", "as_of", "cut_on", "cut_off", "state", "tier"])
        for k, s in signals.items():
            if k == "S8_DXY_CHG":
                w.writerow([today, k, len(s["events"]), s.get("expires") or "", "", "", s["state"], ""])
            else:
                w.writerow([today, k, s.get("value", ""), s.get("as_of", ""), s.get("cut_on", ""),
                            s.get("cut_off", ""), s["state"], s.get("tier", "")])
        w.writerow([today, "COUNT", count, "", "", "", stage_of(count), f"live={live}"])

    # ── 알림 판정 (현행 [7] 알림 조건의 축약) ─────────────────────
    reasons = []
    if prev and prev.get("count") != count:
        reasons.append(f"카운트 {prev.get('count')}→{count} ({prev.get('stage')}→{stage_of(count)})")
    for k, s in signals.items():
        ps = prev_sig.get(k, {}).get("state")
        if ps and ps != "carry" and ps != s["state"] and s["state"] != "carry":
            reasons.append(f"{k} {ps}→{s['state']}")
        if s["state"] == "carry" and prev_sig.get(k, {}).get("state") != "carry":
            reasons.append(f"{k} 판정유보(직전 승계) 발생")
        if s.get("tier") and s["tier"] != "1차":
            reasons.append(f"{k} 소스 [{s['tier']}] — 1차 취득 실패")
    if chg.get("expires"):
        d = (date.fromisoformat(chg["expires"]) - date.fromisoformat(today)).days
        if 0 <= d <= 3 and not (prev_sig.get("S8_DXY_CHG", {}).get("expires") == chg["expires"] and d < 3):
            reasons.append(f"S8 [변화] 분기 {chg['expires']} 만료 임박 — 달력 효과로 카운트 1 감소 예정")

    gh = os.environ.get("GITHUB_OUTPUT")
    if reasons and gh:
        title = f"[SENTINEL] {today} " + reasons[0]
        body = "\\n".join(f"- {r}" for r in reasons) + f"\\n\\n단계 {count}/8(실측 {live}) {stage_of(count)}"
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"alert_title={title}\nalert_body={body}\n")
    print(json.dumps(dict(count=count, live=live, stage=stage_of(count), reasons=reasons), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
