#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_score.py v0.2 (2026-09-14) — 데일리 브리핑 예측 자동 채점

입력  forecast/predictions.csv
시세  Yahoo Finance v8/finance/chart (^KS11, ^GSPC). 취득분은 forecast/prices.csv 에 캐시한다.
출력  forecast/scores.csv       예측 1건당 1행. 매 실행 시 전량 재생성한다
      forecast/SCOREBOARD.md    첫 세 줄 = 표본 수 / 기준선 대비 우위 / 보정 상태

산출 항목은 다섯 가지로 고정한다(지시문 과업 ②). 늘리지 않는다.
  ① 적중 여부·누적 적중률  ② 기준선(실측 상승일 비율)  ③ 기준선 대비 우위
  ④ 브라이어 점수와 개선도  ⑤ 보정표(50~59 / 60~69 / 70 이상)

미취득·미채점은 추정으로 채우지 않는다. 상태값은 채점·미채점·휴장 3종이며,
미채점 건은 다음 실행에서 자동으로 재시도된다(scores.csv 를 매번 재생성하므로).
  - 확정 종가만 쓴다. 해당 거래소의 오늘 날짜 봉은 정규장 마감 후 1시간이 지나기 전이면
    장중 값일 수 있으므로 버린다(05:30 KST 실행 시 미국 겨울철은 15:30 ET로 아직 장중임).
  - 예측일에 봉이 없고 그 뒤 날짜의 봉은 있으면 그 날은 휴장으로 판정해 모수에서 제외한다.
사용: python engine/forecast_score.py --forecast forecast
"""
import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

SYMBOL = {"KOSPI": "^KS11", "SP500": "^GSPC"}
TARGET_NAME = {"KOSPI": "코스피", "SP500": "S&P500"}
FLAT_BAND = 0.10  # 보합권 태그 기준(%)
BUCKETS = [(50, 59), (60, 69), (70, 90)]
MIN_N_CALIB = 30   # 보정 상태 판정 최소 표본
PRICE_COLS = ["symbol", "date", "close"]
SCORE_COLS = ["date", "target", "close_date", "direction", "prob", "reason_class",
              "prev_close", "close", "ret_pct", "actual", "status", "hit", "flat"]
SETTLE_GRACE_SEC = 3600  # 정규장 마감 후 이 시간이 지나야 그날 봉을 확정치로 본다
FETCH_RETRY = 3


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(path, cols, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def fetch_series(symbol, rng="1y"):
    """일자→종가 사전. 실패 시 None 을 돌려준다(추정하지 않는다).
    거래소 현지 기준 오늘 날짜의 봉은 정규장 마감 + 유예 시간 전이면 장중 값이므로 버린다."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = None
    for i in range(FETCH_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            print(f"[score] {symbol} 시세 취득 실패({i+1}/{FETCH_RETRY}): {e}")
    if d is None:
        return None
    try:
        res = d["chart"]["result"][0]
        offset = res["meta"].get("gmtoffset", 0)
        reg_end = (res["meta"].get("currentTradingPeriod", {}).get("regular", {}).get("end"))
        now_ts = datetime.now(timezone.utc).timestamp()
        local_today = datetime.fromtimestamp(now_ts + offset, tz=timezone.utc).strftime("%Y-%m-%d")
        settled_today = reg_end is not None and now_ts >= reg_end + SETTLE_GRACE_SEC
        out = {}
        for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c is None:
                continue
            day = datetime.fromtimestamp(t + offset, tz=timezone.utc).strftime("%Y-%m-%d")
            if day == local_today and not settled_today:
                print(f"[score] {symbol} {day} 봉은 아직 장중이거나 마감 직후이므로 버림")
                continue
            if day > local_today:
                continue
            out[day] = float(c)
        return out
    except Exception as e:
        print(f"[score] {symbol} 응답 해석 실패: {e}")
        return None


def load_prices(path):
    cache = {}
    for r in read_rows(path):
        cache.setdefault(r["symbol"], {})[r["date"]] = float(r["close"])
    return cache


def save_prices(path, cache):
    rows = [{"symbol": s, "date": d, "close": f"{v:.4f}"}
            for s, days in sorted(cache.items()) for d, v in sorted(days.items())]
    write_rows(path, PRICE_COLS, rows)


def prev_close(days, close_date):
    earlier = [d for d in days if d < close_date]
    if not earlier:
        return None
    return days[max(earlier)]


def pct(x, digits=1):
    return f"{x*100:.{digits}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default="forecast")
    ap.add_argument("--no-fetch", action="store_true", help="시세를 받지 않고 prices.csv 캐시만으로 채점(검증용)")
    a = ap.parse_args()
    F = a.forecast

    preds = read_rows(os.path.join(F, "predictions.csv"))
    cache = load_prices(os.path.join(F, "prices.csv"))

    needed = {SYMBOL[r["target"]] for r in preds if r["target"] in SYMBOL}
    fetch_failed = set()
    for sym in sorted(needed):
        if a.no_fetch:
            continue
        got = fetch_series(sym)
        if got is None:
            fetch_failed.add(sym)
            continue
        cache.setdefault(sym, {}).update(got)
    save_prices(os.path.join(F, "prices.csv"), cache)

    scores = []
    for r in preds:
        sym = SYMBOL.get(r["target"])
        days = cache.get(sym, {})
        row = dict(r)
        cd = r["close_date"]
        c = days.get(cd)
        p = prev_close(days, cd)
        if c is None or p is None:
            # 예측일 봉이 없는데 그 뒤 날짜의 확정 봉이 있으면 그날은 휴장이었다고 판정한다
            later = [x for x in days if x > cd]
            status = "휴장" if (c is None and later and p is not None) else "미채점"
            row.update({"status": status, "hit": "", "flat": "", "actual": "",
                        "prev_close": "", "close": "", "ret_pct": ""})
            scores.append(row)
            continue
        ret = (c / p - 1.0) * 100.0
        actual = "상승" if ret > 0 else "하락"
        row.update({"prev_close": f"{p:.2f}", "close": f"{c:.2f}", "ret_pct": f"{ret:.3f}",
                    "actual": actual, "status": "채점",
                    "hit": "1" if actual == r["direction"] else "0",
                    "flat": "1" if abs(ret) < FLAT_BAND else "0"})
        scores.append(row)

    write_rows(os.path.join(F, "scores.csv"), SCORE_COLS, scores)

    # ── 집계 ──────────────────────────────────────────────────────────
    def agg(rows):
        done = [r for r in rows if r["status"] == "채점"]
        n = len(done)
        if n == 0:
            return None
        hits = sum(int(r["hit"]) for r in done)
        base = sum(1 for r in done if r["actual"] == "상승") / n
        acc = hits / n
        bs = sum((( int(r["prob"])/100 if r["direction"] == "상승" else 1-int(r["prob"])/100)
                  - (1.0 if r["actual"] == "상승" else 0.0)) ** 2 for r in done) / n
        bs_base = sum((base - (1.0 if r["actual"] == "상승" else 0.0)) ** 2 for r in done) / n
        buckets = []
        for lo, hi in BUCKETS:
            sel = [r for r in done if lo <= int(r["prob"]) <= hi]
            buckets.append((lo, hi, len(sel),
                            (sum(int(r["hit"]) for r in sel) / len(sel)) if sel else None))
        return {"n": n, "acc": acc, "base": base, "edge": acc - base,
                "brier": bs, "brier_base": bs_base,
                "improve": (bs_base - bs) / bs_base if bs_base > 0 else None,
                "buckets": buckets,
                "flat": sum(int(r["flat"]) for r in done),
                "days": len({r["close_date"] for r in done}),
                "holiday": sum(1 for r in rows if r["status"] == "휴장"),
                "unscored": sum(1 for r in rows if r["status"] == "미채점")}

    overall = agg(scores)
    per = {t: agg([r for r in scores if r["target"] == t]) for t in SYMBOL}

    # 보정 상태: 표본 30건 이상 구간의 실제 적중률이 구간 중앙값과 10%p 넘게 벌어지면 경고
    if overall is None:
        calib = "산출 불가"
    else:
        gaps = []
        for lo, hi, n, acc in overall["buckets"]:
            if n >= MIN_N_CALIB and acc is not None:
                gaps.append(acc - (lo + hi) / 200)
        if not gaps:
            calib = f"산출 불가(구간별 표본 {MIN_N_CALIB}건 미만)"
        elif min(gaps) < -0.10:
            calib = "과신 — 높은 확률을 부여한 예측의 실제 적중률이 표기보다 낮음"
        elif max(gaps) > 0.10:
            calib = "과소 — 실제 적중률이 표기 확률보다 높음"
        else:
            calib = "보정 양호"

    L = []
    if overall is None:
        L += ["표본 0건", "기준선 대비 우위: 산출 불가", "보정 상태: 산출 불가"]
    else:
        L += [f"표본 {overall['n']}건 (채점 영업일 {overall['days']}일 — 동결 60일·존속 판정 250일은 이 일수 기준)",
              f"기준선 대비 우위: {overall['edge']*100:+.1f}%포인트"
              f" (적중률 {pct(overall['acc'])} / 기준선 {pct(overall['base'])})",
              f"보정 상태: {calib}"]
    L += ["", "# 데일리 브리핑 예측 성적표", "",
          f"갱신 {datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M')} 한국시각"
          " · 자동 생성 파일이므로 직접 편집하지 않음", ""]

    if overall is None:
        L += ["- 채점된 예측이 아직 없음. 예측이 등재되고 해당 일자의 종가가 확정되면 자동으로 채워짐.", ""]
    else:
        L += ["## 전체", "",
              f"- 누적 적중률은 {pct(overall['acc'])}이고 같은 기간 실측 기준선은 {pct(overall['base'])}이므로,"
              f" 기준선 대비 우위는 {overall['edge']*100:+.1f}%포인트임.",
              f"- 브라이어 점수는 {overall['brier']:.4f}이고 기준선 확률만 제시하는 전략은 {overall['brier_base']:.4f}이므로,"
              f" 개선도는 {('%+.1f%%' % (overall['improve']*100)) if overall['improve'] is not None else '산출 불가'}임.",
              f"- 보합권(등락률 절댓값 {FLAT_BAND}% 미만) {overall['flat']}건, 미채점 {overall['unscored']}건,"
              f" 휴장으로 제외 {overall['holiday']}건임.", "",
              "## 보정표", "", "| 확률 구간 | 예측 건수 | 실제 적중률 |", "|---|---|---|"]
        for lo, hi, n, acc in overall["buckets"]:
            L.append(f"| {lo}~{hi}% | {n} | {pct(acc) if acc is not None else '—'} |")
        L += ["", f"구간 표본이 {MIN_N_CALIB}건 미만이면 해석 대상이 아님.", "", "## 대상별", "",
              "| 대상 | 표본 | 적중률 | 기준선 | 우위 |", "|---|---|---|---|---|"]
        for t, s in per.items():
            if s is None:
                L.append(f"| {TARGET_NAME[t]} | 0 | — | — | — |")
            else:
                L.append(f"| {TARGET_NAME[t]} | {s['n']} | {pct(s['acc'])} | {pct(s['base'])}"
                         f" | {s['edge']*100:+.1f}%p |")
        L += ["", "## 근거분류별 (부가 집계)", "",
              "| 근거분류 | 표본 | 적중률 | 해석 |", "|---|---|---|---|"]
        done = [r for r in scores if r["status"] == "채점"]
        for rc in ["매크로", "수급", "이벤트", "신호감지"]:
            sel = [r for r in done if r["reason_class"] == rc]
            if not sel:
                L.append(f"| {rc} | 0 | — | 해석 대상 아님 |")
            else:
                acc = sum(int(r["hit"]) for r in sel) / len(sel)
                note = "해석 대상" if len(sel) >= MIN_N_CALIB else f"표본 {MIN_N_CALIB}건 미만 — 해석 대상 아님"
                L.append(f"| {rc} | {len(sel)} | {pct(acc)} | {note} |")
    if fetch_failed:
        L += ["", f"> 이번 실행에서 시세 취득에 실패한 심볼: {', '.join(sorted(fetch_failed))}."
                  " 해당 예측은 미채점으로 남으며 다음 실행에서 재시도됨."]
    L += ["", "판독·동결 규칙은 `forecast/RULES.md`를 따름.", ""]

    with open(os.path.join(F, "SCOREBOARD.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[score] 채점 {sum(1 for r in scores if r['status']=='채점')}건 / "
          f"미채점 {sum(1 for r in scores if r['status']=='미채점')}건 / "
          f"휴장 {sum(1 for r in scores if r['status']=='휴장')}건")


if __name__ == "__main__":
    main()
