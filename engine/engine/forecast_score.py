#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_score.py v0.3 (2026-10-26) — 데일리 브리핑 예측 자동 채점

입력  forecast/predictions.csv   예측(수집 모듈이 기록)
      forecast/prior_log.csv     정량 사전확률(forecast_prior.py가 기록, 없으면 해당 비교만 생략)
시세  Yahoo Finance v8/finance/chart. ^KS11·^GSPC(종가→종가 채점), 069500.KS·SPY(시가→종가, 매매 가능 기준).
      취득분은 forecast/prices.csv 에 캐시한다(symbol,date,open,close).
출력  forecast/scores.csv        예측 1건당 1행. 매 실행 시 전량 재생성
      forecast/SCOREBOARD.md     첫 세 줄 = 표본 수 / 가장 강한 기준 대비 우위 / 보정 상태

v0.3 변경(판독 규칙 v1.2, PREREG 2026-10-26)
  - 기준을 여럿 산출하고, 판정 기준은 사전에 알 수 있는 기준 중 가장 강한 것으로 한다.
      매일 상승(v0.2 기준, 연속성 표시용) · 사전 다수 방향(직전 250거래일 상승 비율로 정한 방향) ·
      전야 미국 추종(코스피: 직전 미국 세션 S&P500 부호) · 정량 모델(prior_log) · 전일 방향 추종(참고)
  - 신뢰구간(윌슨 95%), 동전 대비 단측 p, 가장 강한 기준 대비 짝 비교(정확 맥니마) 단측 p
  - 매매 가능 기준: 코스피는 KODEX 200, S&P500은 SPY의 시가→종가로 다시 채점하고 가상 손익 산출
  - 등록 무결성: 등록 시각이 대상 시장 개장 시각 이후면 「사후」로 표시하고, 적시 등록분만의 성적을 병기
  - 미기록(개장일인데 예측이 없는 날) 집계
미취득·미채점은 추정으로 채우지 않는다. 상태값은 채점·미채점·휴장 3종이다.
사용: python engine/forecast_score.py --forecast forecast [--no-fetch]
"""
import argparse
import csv
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

SYMBOL = {"KOSPI": "^KS11", "SP500": "^GSPC"}
TRADE = {"KOSPI": "069500.KS", "SP500": "SPY"}
TRADE_NAME = {"KOSPI": "KODEX 200", "SP500": "SPY"}
COST = {"KOSPI": 0.0005, "SP500": 0.0002}   # 왕복 거래비용(PREREG 5절)
TARGET_NAME = {"KOSPI": "코스피", "SP500": "S&P500"}
FLAT_BAND = 0.10   # 보합권 태그 기준(%)
BUCKETS = [(50, 55), (56, 65), (66, 90)]   # 프롬프트의 확신 수준 구간과 같음(낮음·중간·높음)
MIN_N_CALIB = 30
CLIM_WIN = 250
CHECKPOINTS = [60, 125, 250]
N_DETECT_5PP = 620   # 동전 기준 5%p 우위 판별(단측 0.05, 검정력 0.8) 소요 건수(PREREG 7절)
PRICE_COLS = ["symbol", "date", "open", "close"]
SCORE_COLS = ["date", "target", "close_date", "direction", "prob", "reason_class",
              "prev_close", "close", "ret_pct", "actual", "status", "hit", "flat",
              "registered_at", "reg_status", "edited_at",
              "b_up", "b1", "b2", "mom", "prior_p", "prior_hit", "clim_p",
              "oc_ret_pct", "oc_hit", "paper_pnl_pct"]
SETTLE_GRACE_SEC = 3600
FETCH_RETRY = 3
KST = timezone(timedelta(hours=9))


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


def fetch_series(symbol, rng="2y"):
    """일자 → (시가, 종가). 실패 시 None(추정하지 않음). 정규장 마감 + 유예 전의 당일 봉은 버린다."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = None
    for i in range(FETCH_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            print(f"[score] {symbol} 시세 취득 실패({i+1}/{FETCH_RETRY}): {e}")
    if d is None:
        return None
    try:
        res = d["chart"]["result"][0]
        offset = res["meta"].get("gmtoffset", 0)
        reg_end = res["meta"].get("currentTradingPeriod", {}).get("regular", {}).get("end")
        now_ts = datetime.now(timezone.utc).timestamp()
        local_today = datetime.fromtimestamp(now_ts + offset, tz=timezone.utc).strftime("%Y-%m-%d")
        settled_today = reg_end is not None and now_ts >= reg_end + SETTLE_GRACE_SEC
        q = res["indicators"]["quote"][0]
        out = {}
        for t, o, c in zip(res["timestamp"], q["open"], q["close"]):
            if c is None:
                continue
            day = datetime.fromtimestamp(t + offset, tz=timezone.utc).strftime("%Y-%m-%d")
            if day > local_today or (day == local_today and not settled_today):
                continue
            out[day] = (float(o) if o else None, float(c))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[score] {symbol} 응답 해석 실패: {e}")
        return None


def load_prices(path):
    cache = {}
    for r in read_rows(path):
        o = r.get("open", "")
        cache.setdefault(r["symbol"], {})[r["date"]] = (float(o) if o not in ("", None) else None, float(r["close"]))
    return cache


def save_prices(path, cache):
    rows = [{"symbol": s, "date": d, "open": (f"{v[0]:.4f}" if v[0] else ""), "close": f"{v[1]:.4f}"}
            for s, days in sorted(cache.items()) for d, v in sorted(days.items())]
    write_rows(path, PRICE_COLS, rows)


def prev_day(days, d):
    earlier = [x for x in days if x < d]
    return max(earlier) if earlier else None


def pct(x, digits=1):
    return "—" if x is None else f"{x*100:.{digits}f}%"


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return c - h, c + h


def binom_upper(k, n, p=0.5):
    """P(X >= k), X~Bin(n,p) — 단측 p값."""
    if n == 0:
        return None
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def mcnemar(a, b):
    """a가 b보다 낫다는 단측 정확 맥니마. a, b는 같은 행의 적중(0/1) 목록."""
    n10 = sum(1 for x, y in zip(a, b) if x and not y)
    n01 = sum(1 for x, y in zip(a, b) if y and not x)
    return n10, n01, (binom_upper(n10, n10 + n01) if n10 + n01 else None)


def nw_t(x, lag=5):
    n = len(x)
    if n < 2:
        return None
    m = sum(x) / n
    d = [v - m for v in x]
    s = sum(v * v for v in d) / n
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * sum(d[i] * d[i - l] for i in range(l, n)) / n
    return m / math.sqrt(s / n) if s > 0 else None


def deadline_utc(target, d):
    """사전 등록 마감(UTC). 코스피 = D일 09:00 한국시각, S&P500 = D일 13:30 UTC(미국 서머타임 개장 기준, 보수적)."""
    if target == "KOSPI":
        return f"{d}T00:00:00Z"
    return f"{d}T13:30:00Z"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default="forecast")
    ap.add_argument("--no-fetch", action="store_true", help="시세를 받지 않고 prices.csv 캐시만으로 채점(검증용)")
    a = ap.parse_args()
    F = a.forecast

    preds = read_rows(os.path.join(F, "predictions.csv"))
    priors = {(r["date"], r["target"]): r for r in read_rows(os.path.join(F, "prior_log.csv"))}
    cache = load_prices(os.path.join(F, "prices.csv"))

    needed = set()
    for r in preds:
        if r["target"] in SYMBOL:
            needed |= {SYMBOL[r["target"]], TRADE[r["target"]]}
    if any(r["target"] == "KOSPI" for r in preds):
        needed.add("^GSPC")   # 전야 미국 추종 기준
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

    spx_days = cache.get("^GSPC", {})
    scores = []
    for r in preds:
        t = r["target"]
        days = cache.get(SYMBOL.get(t), {})
        row = dict(r)
        cd = r["close_date"]
        c = days.get(cd, (None, None))[1]
        pd_ = prev_day(days, cd)
        p = days[pd_][1] if pd_ else None
        reg = r.get("registered_at", "") or ""
        row["reg_status"] = "미상" if not reg else ("적시" if reg < deadline_utc(t, r["date"]) else "사후")
        if c is None or p is None:
            later = [x for x in days if x > cd]
            status = "휴장" if (c is None and later and p is not None) else "미채점"
            row.update({"status": status})
            scores.append(row)
            continue
        ret = (c / p - 1.0) * 100.0
        actual = "상승" if ret > 0 else "하락"
        up = actual == "상승"
        row.update({"prev_close": f"{p:.2f}", "close": f"{c:.2f}", "ret_pct": f"{ret:.3f}",
                    "actual": actual, "status": "채점",
                    "hit": "1" if actual == r["direction"] else "0",
                    "flat": "1" if abs(ret) < FLAT_BAND else "0"})
        # 사전 기준(08시 시점에 알 수 있는 정보만)
        row["b_up"] = "1" if up else "0"
        hist = sorted(x for x in days if x < cd)
        if len(hist) > CLIM_WIN:
            seq = hist[-(CLIM_WIN + 1):]
            ups = sum(1 for i in range(1, len(seq)) if days[seq[i]][1] > days[seq[i - 1]][1])
            clim = ups / CLIM_WIN
            row["clim_p"] = f"{clim:.4f}"
            row["b1"] = "1" if ((clim >= 0.5) == up) else "0"
        if pd_ and prev_day(days, pd_):
            mom_up = days[pd_][1] > days[prev_day(days, pd_)][1]
            row["mom"] = "1" if mom_up == up else "0"
        if t == "KOSPI":
            us_d = prev_day(spx_days, cd)
            if us_d and prev_day(spx_days, us_d):
                us_up = spx_days[us_d][1] > spx_days[prev_day(spx_days, us_d)][1]
                row["b2"] = "1" if us_up == up else "0"
        elif row.get("b1"):
            row["b2"] = row["b1"]   # S&P500에는 전야 기준이 없으므로 사전 다수 방향으로 대신함(PREREG 3절)
        pr = priors.get((r["date"], t))
        if pr and pr.get("generated_at", "") < deadline_utc(t, r["date"]):
            pp = float(pr["p_up_cc"])
            row["prior_p"] = f"{pp:.4f}"
            row["prior_hit"] = "1" if (pp > 0.5) == up else "0"
        # 매매 가능 기준(시가→종가)과 가상 손익
        tv = cache.get(TRADE[t], {}).get(cd)
        if tv and tv[0]:
            oc = tv[1] / tv[0] - 1.0
            row["oc_ret_pct"] = f"{oc*100:.3f}"
            row["oc_hit"] = "1" if (oc > 0) == (r["direction"] == "상승") else "0"
            row["paper_pnl_pct"] = f"{((oc - COST[t]) if r['direction'] == '상승' else 0.0)*100:.3f}"
        scores.append(row)

    write_rows(os.path.join(F, "scores.csv"), SCORE_COLS, scores)

    # ── 집계 ──────────────────────────────────────────────────────────
    done = [r for r in scores if r["status"] == "채점"]

    def prob_up(r):
        q = int(r["prob"]) / 100
        return q if r["direction"] == "상승" else 1 - q

    def y(r):
        return 1.0 if r["actual"] == "상승" else 0.0

    def agg(rows):
        n = len(rows)
        if n == 0:
            return None
        hits = [int(r["hit"]) for r in rows]
        k = sum(hits)
        lo, hi = wilson(k, n)
        out = {"n": n, "k": k, "acc": k / n, "lo": lo, "hi": hi, "p_coin": binom_upper(k, n),
               "brier": sum((prob_up(r) - y(r)) ** 2 for r in rows) / n,
               "days": len({r["close_date"] for r in rows})}
        base_up = sum(y(r) for r in rows) / n
        out["brier_realized"] = sum((base_up - y(r)) ** 2 for r in rows) / n   # v0.2 기준(연속성)
        cl = [r for r in rows if r.get("clim_p")]
        out["brier_clim"] = (sum((float(r["clim_p"]) - y(r)) ** 2 for r in cl) / len(cl)) if len(cl) == n else None
        bases = {}
        for key, label in (("b_up", "매일 상승"), ("b1", "사전 다수 방향"), ("b2", "전야 미국 추종"),
                           ("prior_hit", "정량 모델"), ("mom", "전일 방향 추종")):
            if all(r.get(key) not in (None, "") for r in rows):
                bh = [int(r[key]) for r in rows]
                bases[key] = {"label": label, "acc": sum(bh) / n, "mc": mcnemar(hits, bh)}
        out["bases"] = bases
        cand = [k_ for k_ in ("b1", "b2", "prior_hit") if k_ in bases]   # 판정 기준 후보(PREREG 6절)
        if cand:
            best = max(cand, key=lambda k_: bases[k_]["acc"])
            out["best"] = best
            out["edge"] = out["acc"] - bases[best]["acc"]
        return out

    overall = agg(done)
    per = {t: agg([r for r in done if r["target"] == t]) for t in SYMBOL}

    calib = "산출 불가"
    buckets = []
    if overall:
        for lo, hi in BUCKETS:
            sel = [r for r in done if lo <= int(r["prob"]) <= hi]
            acc = (sum(int(r["hit"]) for r in sel) / len(sel)) if sel else None
            mean_p = (sum(int(r["prob"]) for r in sel) / len(sel) / 100) if sel else None
            buckets.append((lo, hi, len(sel), acc, mean_p))
        gaps = [acc - mp for lo, hi, n, acc, mp in buckets if n >= MIN_N_CALIB and acc is not None]
        if not gaps:
            calib = f"산출 불가(구간별 표본 {MIN_N_CALIB}건 미만)"
        elif min(gaps) < -0.10:
            calib = "과신 — 표기 확률보다 실제 적중률이 10%p 넘게 낮은 구간이 있음"
        elif max(gaps) > 0.10:
            calib = "과소 — 실제 적중률이 표기 확률보다 10%p 넘게 높은 구간이 있음"
        else:
            calib = "보정 양호"

    def ptxt(p):
        return "—" if p is None else (f"{p:.3f}" if p >= 0.001 else "<0.001")

    L = []
    if overall is None:
        L += ["표본 0건", "기준선 대비 우위: 산출 불가", "보정 상태: 산출 불가"]
    else:
        b = overall["bases"].get(overall.get("best"))
        line2 = (f"기준선 대비 우위: {overall['edge']*100:+.1f}%포인트 (적중률 {pct(overall['acc'])} / 가장 강한 기준 "
                 f"{b['label']} {pct(b['acc'])}, 짝 비교 단측 p={ptxt(b['mc'][2])})") if b else "기준선 대비 우위: 산출 불가"
        L += [f"표본 {overall['n']}건 (채점 영업일 {overall['days']}일 — 동결 60일·중간 점검 125일·존속 판정 250일은 이 일수 기준)",
              line2, f"보정 상태: {calib}"]
    L += ["", "# 데일리 브리핑 예측 성적표", "",
          f"갱신 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} 한국시각 · 자동 생성 파일이므로 직접 편집하지 않음 · "
          "판독은 `forecast/RULES.md`, 검정 명세는 `forecast/PREREG.md`", ""]
    if overall is None:
        L += ["- 채점된 예측이 아직 없음.", ""]
    else:
        nxt = next((c for c in CHECKPOINTS if c > overall["days"]), None)
        L += ["## 전체", "",
              f"- 적중률 {pct(overall['acc'])}(95% 범위 {pct(overall['lo'])}~{pct(overall['hi'])}), 동전 대비 단측 p={ptxt(overall['p_coin'])}임.",
              f"- 5%p 우위를 가려내는 데 필요한 표본 약 {N_DETECT_5PP}건 중 {overall['n']}건 누적({overall['n']/N_DETECT_5PP*100:.0f}%)."
              + (f" 다음 점검은 채점 영업일 {nxt}일." if nxt else ""),
              f"- 브라이어 점수 {overall['brier']:.4f}(동전 0.2500, 사전 기후값 "
              + (f"{overall['brier_clim']:.4f}" if overall["brier_clim"] is not None else "산출 불가")
              + f", 같은 기간 실측 빈도 {overall['brier_realized']:.4f}).",
              f"- 보합권(등락률 절댓값 {FLAT_BAND}% 미만) {sum(int(r['flat']) for r in done)}건, 미채점 "
              f"{sum(1 for r in scores if r['status']=='미채점')}건, 휴장 제외 {sum(1 for r in scores if r['status']=='휴장')}건.", "",
              "## 기준별 비교", "", "| 기준 | 적중률 | 브리핑 − 기준 | 브리핑만 적중 / 기준만 적중 | 짝 비교 단측 p |", "|---|---|---|---|---|"]
        for key, v in overall["bases"].items():
            mark = " ★" if key == overall.get("best") else ""
            L.append(f"| {v['label']}{mark} | {pct(v['acc'])} | {(overall['acc']-v['acc'])*100:+.1f}%p | "
                     f"{v['mc'][0]} / {v['mc'][1]} | {ptxt(v['mc'][2])} |")
        L += ["", "★ = 판정 기준(사전 다수 방향·전야 미국 추종·정량 모델 중 가장 높은 것). 매일 상승은 v0.2 기준으로 연속성 확인용이며, "
              "전일 방향 추종은 참고용임. 전야 미국 추종은 코스피에만 정의되며 S&P500 행은 사전 다수 방향으로 대신함.", "",
              "## 대상별", "", "| 대상 | 표본 | 적중률 | 95% 범위 | 가장 강한 기준 | 우위 | 짝 비교 p |", "|---|---|---|---|---|---|---|"]
        for t, s in per.items():
            if s is None:
                L.append(f"| {TARGET_NAME[t]} | 0 | — | — | — | — | — |")
                continue
            bb = s["bases"].get(s.get("best"))
            L.append(f"| {TARGET_NAME[t]} | {s['n']} | {pct(s['acc'])} | {pct(s['lo'])}~{pct(s['hi'])} | "
                     + (f"{bb['label']} {pct(bb['acc'])} | {s['edge']*100:+.1f}%p | {ptxt(bb['mc'][2])} |" if bb else "— | — | — |"))
        # 매매 가능 기준
        L += ["", "## 매매 가능 기준(시가→종가)과 가상 손익", "",
              "| 대상 | 상품 | 표본 | 시가→종가 적중률 | 가상 손익 누적(상승 예측일만 보유, 비용 차감) | 매일 보유 누적 | 뉴이-웨스트 t |",
              "|---|---|---|---|---|---|---|"]
        for t in SYMBOL:
            sel = [r for r in done if r["target"] == t and r.get("oc_hit") not in (None, "")]
            if not sel:
                L.append(f"| {TARGET_NAME[t]} | {TRADE_NAME[t]} | 0 | — | — | — | — |")
                continue
            pnl = [float(r["paper_pnl_pct"]) / 100 for r in sel]
            hold = [float(r["oc_ret_pct"]) / 100 for r in sel]
            cum = math.prod(1 + x for x in pnl) - 1
            cumh = math.prod(1 + x for x in hold) - 1
            tt = nw_t(pnl) if len(pnl) >= 60 else None
            L.append(f"| {TARGET_NAME[t]} | {TRADE_NAME[t]} | {len(sel)} | {pct(sum(int(r['oc_hit']) for r in sel)/len(sel))} | "
                     f"{cum*100:+.2f}% | {cumh*100:+.2f}% | {('%.2f' % tt) if tt is not None else '60건 미만'} |")
        L += ["", "종가→종가 적중은 08시 이전에 끝난 미국 장 결과를 이미 반영하므로 그대로 매매로 옮길 수 없음. 투자 활용 판단은 이 표를 기준으로 함.", ""]
        # 정량 모델 대결
        pr_rows = [r for r in done if r.get("prior_hit") not in (None, "")]
        L += ["## 정량 모델과의 대결", ""]
        if not pr_rows:
            L += ["- 같은 날 기록된 정량 사전확률이 아직 없음(`forecast/prior_log.csv`).", ""]
        else:
            bh = [int(r["hit"]) for r in pr_rows]
            mh = [int(r["prior_hit"]) for r in pr_rows]
            mc = mcnemar(bh, mh)
            bb = sum((prob_up(r) - y(r)) ** 2 for r in pr_rows) / len(pr_rows)
            mb = sum((float(r["prior_p"]) - y(r)) ** 2 for r in pr_rows) / len(pr_rows)
            L += [f"- 같은 날 {len(pr_rows)}건: 브리핑 {sum(bh)}건 적중, 정량 모델 {sum(mh)}건 적중. 브리핑만 적중 {mc[0]}건, 모델만 적중 {mc[1]}건, "
                  f"브리핑 우위 단측 p={ptxt(mc[2])}.",
                  f"- 브라이어 점수: 브리핑 {bb:.4f}, 정량 모델 {mb:.4f}(낮을수록 좋음).", ""]
        # 등록 무결성
        cnt = {k_: sum(1 for r in scores if r.get("reg_status") == k_) for k_ in ("적시", "사후", "미상")}
        timely = [r for r in done if r.get("reg_status") == "적시"]
        edited = sum(1 for r in scores if r.get("edited_at"))
        L += ["## 등록 무결성", "",
              f"- 등록 시각 기준 적시 {cnt['적시']}건, 사후 {cnt['사후']}건, 미상 {cnt['미상']}건, 등록 뒤 수정 {edited}건.",
              f"- 적시 등록분만의 적중률: " + (f"{pct(sum(int(r['hit']) for r in timely)/len(timely))}({len(timely)}건)" if timely else "표본 없음") + ".",
              "- 사후 등록은 대상 시장 개장 뒤에 기록된 예측임. 위조를 뜻하지 않으나 사전 예측임을 기록으로 입증할 수 없어 증거력이 낮음.", ""]
        # 보정표
        L += ["## 보정표", "", "| 확률 구간 | 예측 건수 | 평균 표기 확률 | 실제 적중률 |", "|---|---|---|---|"]
        for lo, hi, n, acc, mp in buckets:
            L.append(f"| {lo}~{hi}% | {n} | {pct(mp)} | {pct(acc)} |")
        L += ["", f"구간 표본이 {MIN_N_CALIB}건 미만이면 해석 대상이 아님.", "",
              "## 근거분류별 (부가 집계)", "", "| 근거분류 | 표본 | 적중률 | 해석 |", "|---|---|---|---|"]
        for rc in ["정량", "매크로", "수급", "이벤트", "신호감지"]:
            sel = [r for r in done if r["reason_class"] == rc]
            if not sel:
                L.append(f"| {rc} | 0 | — | 해석 대상 아님 |")
            else:
                acc = sum(int(r["hit"]) for r in sel) / len(sel)
                note = "해석 대상" if len(sel) >= MIN_N_CALIB else f"표본 {MIN_N_CALIB}건 미만 — 해석 대상 아님"
                L.append(f"| {rc} | {len(sel)} | {pct(acc)} | {note} |")
        # 미기록
        miss = []
        for t in SYMBOL:
            ds = sorted(r["date"] for r in preds if r["target"] == t)
            if not ds:
                continue
            last_scored = max((r["close_date"] for r in done if r["target"] == t), default=ds[-1])
            have = set(ds)
            miss += [f"{d}({TARGET_NAME[t]})" for d in sorted(cache.get(SYMBOL[t], {}))
                     if ds[0] <= d <= last_scored and d not in have]
        L += ["", "## 미기록", "",
              f"- 개장일인데 예측이 없는 날 {len(miss)}건" + (f": {', '.join(miss)}" if miss else "") + ".", ""]
    if fetch_failed:
        L += [f"> 이번 실행에서 시세 취득에 실패한 심볼: {', '.join(sorted(fetch_failed))}."
              " 해당 예측은 미채점으로 남으며 다음 실행에서 재시도됨.", ""]

    with open(os.path.join(F, "SCOREBOARD.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[score] 채점 {len(done)}건 / 미채점 {sum(1 for r in scores if r['status']=='미채점')}건 / "
          f"휴장 {sum(1 for r in scores if r['status']=='휴장')}건")


if __name__ == "__main__":
    main()
