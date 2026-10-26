#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_prior.py v0.1 (2026-10-26) — 정량 사전확률 산출 (표준 라이브러리만 사용)

목적  데일리 브리핑과 같은 정보 시점(한국시각 D일 08시 이전)에 코스피·S&P500의 D일 방향 확률을
      기계적으로 산출해 기록한다. 브리핑 예측의 비교 기준이자, 프롬프트 v5.0 적용 이후에는
      브리핑의 출발점이 된다. 모델 명세는 forecast/PREREG.md 2·3절과 같으며 임의로 바꾸지 않는다.

출력  forecast/prior.json      최신 1건(브리핑이 읽음)
      forecast/prior_log.csv   1행 1대상, 추가 전용. (date,target) 중복은 최초 1건만 유지
사용  python engine/forecast_prior.py --forecast forecast [--date YYYY-MM-DD] [--selftest]
      --date 를 주면 그 한국 날짜를 대상으로 산출한다(검증용, 로그에 쓰지 않음).
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

MODEL_VERSION = "prior v0.1 (PREREG 2026-10-26)"
KST = timezone(timedelta(hours=9))
C_REG = 1.0          # L2 정규화 강도(PREREG 3절)
SETTLE_GRACE_SEC = 900
SYMS = ["^KS11", "^GSPC", "^SOX", "^VIX", "069500.KS", "SPY"]
LOG_COLS = ["date", "target", "p_up_cc", "p_up_oc", "info_session", "train_until",
            "generated_at", "model"]


# ── 시세 ─────────────────────────────────────────────────────────────
def fetch(symbol, now_ts):
    """일자 → (시가, 종가). 장중이거나 마감 직후인 당일 봉은 버린다."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?period1=946684800&period2={int(now_ts)}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    else:
        raise RuntimeError(f"{symbol} 시세 취득 실패: {last}")
    res = d["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    reg_end = res["meta"].get("currentTradingPeriod", {}).get("regular", {}).get("end")
    local_today = datetime.fromtimestamp(now_ts + off, tz=timezone.utc).strftime("%Y-%m-%d")
    settled_today = reg_end is not None and now_ts >= reg_end + SETTLE_GRACE_SEC
    q = res["indicators"]["quote"][0]
    out = {}
    for t, o, c in zip(res["timestamp"], q["open"], q["close"]):
        if c is None:
            continue
        day = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        if day > local_today or (day == local_today and not settled_today):
            continue
        out[day] = (o, float(c))
    return out


def load_local(path):
    """검증용: date,open,close CSV."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            o = float(r["open"]) if r["open"] not in ("", "None") else None
            out[r["date"]] = (o, float(r["close"]))
    return out


# ── 지표 계산(판다스와 같은 정의) ─────────────────────────────────────
def reindex_ffill1(series, dates):
    out, prev_val, prev_ok = {}, None, False
    for d in dates:
        if d in series:
            out[d] = series[d]
            prev_val, prev_ok = series[d], True
        elif prev_ok:
            out[d] = prev_val      # 1칸만 이월
            prev_ok = False
        else:
            out[d] = None
    return out


def logdiff(vals):
    out = [None]
    for a, b in zip(vals[:-1], vals[1:]):
        out.append(math.log(b / a) if a and b else None)
    return out


def trend20(vals):
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - 19):i + 1]
        if i < 19 or any(v is None for v in w):
            out.append(None)
        else:
            out.append(math.log(vals[i] / (sum(w) / 20)))
    return out


def zscore252(vals):
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - 251):i + 1]
        if i < 251 or any(v is None for v in w):
            out.append(None)
            continue
        m = sum(w) / 252
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / 251)
        out.append((vals[i] - m) / sd if sd > 0 else None)
    return out


def last_before(sorted_dates, d):
    lo, hi = 0, len(sorted_dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_dates[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return sorted_dates[lo - 1] if lo > 0 else None


def build(px):
    ks, sp, sox, vix, ke, spy = (px[s] for s in SYMS)
    us = sorted(sp)
    spc = [sp[d][1] for d in us]
    soxc = reindex_ffill1({d: v[1] for d, v in sox.items()}, us)
    vixc = reindex_ffill1({d: v[1] for d, v in vix.items()}, us)
    soxv = [soxc[d] for d in us]
    vixv = [vixc[d] for d in us]
    U = {}
    spx_r, sox_r, dvix = logdiff(spc), logdiff(soxv), logdiff(vixv)
    spx_tr = trend20(spc)
    vix_z = zscore252([math.log(v) if v else None for v in vixv])
    for i, d in enumerate(us):
        U[d] = {"spx_r": spx_r[i], "sox_r": sox_r[i], "dvix": dvix[i], "spx_tr": spx_tr[i], "vix_z": vix_z[i]}
    kd = sorted(ks)
    ksc = [ks[d][1] for d in kd]
    ks_r, ks_tr = logdiff(ksc), trend20(ksc)
    KF = {d: {"ks_r": ks_r[i], "ks_tr": ks_tr[i]} for i, d in enumerate(kd)}
    return us, U, kd, KF


def kospi_features(d, us, U, kd, KF):
    ud = last_before(us, d)
    kp = last_before(kd, d)
    if ud is None or kp is None:
        return None, ud
    x = [U[ud]["spx_r"], U[ud]["sox_r"], U[ud]["dvix"], KF[kp]["ks_r"], KF[kp]["ks_tr"]]
    return (None if any(v is None for v in x) else x), ud


def spx_features(d, us, U):
    ud = last_before(us, d)
    if ud is None:
        return None, ud
    x = [U[ud]["spx_r"], U[ud]["dvix"], U[ud]["spx_tr"], U[ud]["vix_z"]]
    return (None if any(v is None for v in x) else x), ud


# ── 로지스틱 회귀(L2, 절편 비정규화) — 뉴턴법 ─────────────────────────
def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c] != 0:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_logit(X, y, C=C_REG, iters=50):
    k = len(X[0])
    mu = [sum(r[j] for r in X) / len(X) for j in range(k)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / len(X)) for j in range(k)]
    Z = [[1.0] + [(r[j] - mu[j]) / sd[j] for j in range(k)] for r in X]
    w = [0.0] * (k + 1)
    lam = 1.0 / C
    for _ in range(iters):
        g = [0.0] * (k + 1)
        H = [[0.0] * (k + 1) for _ in range(k + 1)]
        for z, t in zip(Z, y):
            s = sum(a * b for a, b in zip(w, z))
            p = 1.0 / (1.0 + math.exp(-s))
            e = p - t
            v = p * (1 - p)
            for a in range(k + 1):
                g[a] += e * z[a]
                za = v * z[a]
                Ha = H[a]
                for b in range(a, k + 1):
                    Ha[b] += za * z[b]
        for a in range(1, k + 1):
            g[a] += lam * w[a]
            H[a][a] += lam
        for a in range(k + 1):
            for b in range(a):
                H[a][b] = H[b][a]
        step = solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-10:
            break
    return {"mu": mu, "sd": sd, "w": w}


def predict(m, x):
    z = [1.0] + [(xi - mi) / si for xi, mi, si in zip(x, m["mu"], m["sd"])]
    return 1.0 / (1.0 + math.exp(-sum(a * b for a, b in zip(m["w"], z))))


def train_sets(px, us, U, kd, KF, cutoff):
    ks, ke, sp, spy = px["^KS11"], px["069500.KS"], px["^GSPC"], px["SPY"]
    T1, T2, T3, T4 = ([], []), ([], []), ([], []), ([], [])
    for i in range(1, len(kd)):
        d = kd[i]
        if d >= cutoff:
            break
        x, _ = kospi_features(d, us, U, kd, KF)
        if x is None:
            continue
        T1[0].append(x); T1[1].append(1 if ks[d][1] / ks[kd[i - 1]][1] - 1 > 0 else 0)
        if d in ke and ke[d][0] and ke[d][0] > 0:
            T2[0].append(x); T2[1].append(1 if ke[d][1] / ke[d][0] - 1 > 0 else 0)
    for i in range(1, len(us)):
        d = us[i]
        if d >= cutoff:
            break
        x, _ = spx_features(d, us, U)
        if x is None:
            continue
        T4[0].append(x); T4[1].append(1 if sp[d][1] / sp[us[i - 1]][1] - 1 > 0 else 0)
        if d in spy and spy[d][0] and spy[d][0] > 0:
            T3[0].append(x); T3[1].append(1 if spy[d][1] / spy[d][0] - 1 > 0 else 0)
    return T1, T2, T3, T4


def compute(px, target_date):
    us, U, kd, KF = build(px)
    cutoff = target_date[:4] + "-01-01"      # 매년 1월 첫 거래일 재학습(PREREG 3절)
    T1, T2, T3, T4 = train_sets(px, us, U, kd, KF, cutoff)
    models = {k: fit_logit(*v) for k, v in (("t1", T1), ("t2", T2), ("t3", T3), ("t4", T4))}
    kx, k_us = kospi_features(target_date, us, U, kd, KF)
    sx, s_us = spx_features(target_date, us, U)
    out = {"kospi": None, "sp500": None, "train_until": max(d for d in kd if d < cutoff)}
    if kx is not None:
        out["kospi"] = {"p_up_cc": round(predict(models["t1"], kx), 4),
                        "p_up_oc": round(predict(models["t2"], kx), 4),
                        "info_session": k_us,
                        "features": dict(zip(["spx_r", "sox_r", "dvix", "ks_r", "ks_tr"], [round(v, 6) for v in kx]))}
    if sx is not None:
        out["sp500"] = {"p_up_cc": round(predict(models["t4"], sx), 4),
                        "p_up_oc": round(predict(models["t3"], sx), 4),
                        "info_session": s_us,
                        "features": dict(zip(["spx_r", "dvix", "spx_tr", "vix_z"], [round(v, 6) for v in sx]))}
    return out, models


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast", default="forecast")
    ap.add_argument("--date", default="", help="한국 날짜 YYYY-MM-DD(검증용, 로그 미기록)")
    ap.add_argument("--local", default="", help="검증용 시세 폴더(심볼별 CSV)")
    a = ap.parse_args()
    now_ts = time.time()
    now_kst = datetime.fromtimestamp(now_ts, KST)
    target = a.date or now_kst.strftime("%Y-%m-%d")
    if not a.date and now_kst.isoweekday() >= 6:
        print("[prior] 주말 — 산출하지 않음")
        return
    if a.local:
        px = {s: load_local(os.path.join(a.local, s.replace("^", "").replace(".", "_") + ".csv")) for s in SYMS}
        px = {s: {d: v for d, v in px[s].items() if d < target} for s in SYMS}
    else:
        px = {s: fetch(s, now_ts) for s in SYMS}
    res, _ = compute(px, target)
    gen = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {"date": target, "generated_at": gen, "model": MODEL_VERSION, "train_until": res["train_until"],
           "kospi": res["kospi"], "sp500": res["sp500"],
           "_설명": "p_up_cc = 직전 종가 대비 D일 종가 상승 확률(브리핑 예측 대상과 같음), "
                   "p_up_oc = D일 시가→종가 상승 확률(매매 가능 기준). info_session = 사용한 미국 직전 세션 날짜."}
    print(json.dumps(doc, ensure_ascii=False, indent=1))
    if a.date:
        return
    os.makedirs(a.forecast, exist_ok=True)
    with open(os.path.join(a.forecast, "prior.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    lp = os.path.join(a.forecast, "prior_log.csv")
    rows = read_rows(lp)
    seen = {(r["date"], r["target"]) for r in rows}
    for tgt, key in (("KOSPI", "kospi"), ("SP500", "sp500")):
        v = res[key]
        if v is None or (target, tgt) in seen:
            continue
        rows.append({"date": target, "target": tgt, "p_up_cc": v["p_up_cc"], "p_up_oc": v["p_up_oc"],
                     "info_session": v["info_session"], "train_until": res["train_until"],
                     "generated_at": gen, "model": MODEL_VERSION})
    rows.sort(key=lambda r: (r["date"], r["target"]))
    with open(lp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in LOG_COLS})
    print(f"[prior] {target} 기록 완료")


if __name__ == "__main__":
    sys.exit(main())
