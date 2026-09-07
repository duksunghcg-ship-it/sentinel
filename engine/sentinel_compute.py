#!/usr/bin/env python3
# -*- coding: utf-8 -*-
ENGINE_VERSION = "v1.4"          # 개정 시 반드시 갱신 (head -5 | grep 로 판독)
ENGINE_UPDATED = "2026-08-26"
"""
sentinel_compute.py — SENTINEL S4/S7/S8 정량 신호 산출기
================================================================
SSOT §1-B 준거. 웹/챗 실행 환경(코드 실행 가능) 전용.

역할
  1) 원발행처 우선 시계열 취득 (소스 체인 자동 폴백)
  2) 로컬 슬라이싱으로 롤링 윈도우 확정 (§2-⑥ 개정)
  3) 비대칭 밴드 + 2연속 확인으로 활성/해제 판정 (§2-⑨ 개정)
  4) 직전 확정일 이후 전 영업일 연속 스캔 (§1-B B-2)
  5) 실패 시 판정 유보(carry) 반환 — 이월 컷값으로 확정 선언 금지 (§1-B B-3)
  6) 시계열 스냅샷 캐시 저장 (1차 취득분만)

사용
  python3 sentinel_compute.py                      # 전체 산출, 카드용 MD 출력
  python3 sentinel_compute.py --since 2026-08-21   # 직전 확정일 지정 스캔
  python3 sentinel_compute.py --json               # 기계 판독용 JSON

출력
  ./sentinel_out/snapshot_YYYY-MM-DD.json   전체 결과
  ./sentinel_out/cache_<SERIES>.csv         시계열 캐시(다음 실패 시 사용)
  stdout                                    상태카드 붙여넣기용 Markdown

── 개정 이력 ─────────────────────────────────────────────────────────
v1.1 (2026-08-25) HTTP 전송 계층 분리. urllib/requests가 프록시 경유 시
     응답을 읽지 못하고 정지(실측 25초 read timeout). urllib 1회 시도 후
     curl 폴백으로 교체.

v1.2 (2026-08-26) 취득 성공률·실행시간 개선. 계열 단위 메모이제이션으로
     VIXCLS 중복 취득 제거(호출 4→3), curl -o 임시파일 방식, 재시도
     4회(5/15/30/45초 + 지터), ★회당 취득 예산 300초 상한.

v1.3 (2026-08-26) ★소스 구조 전환 — FRED 웹 호스트 종속 해소.
     [배경] 이 실행 환경에서 fred.stlouisfed.org는 전 경로가 상시 불안정하다.
       graph/fredgraph.csv · data/<S>.txt · research.stlouisfed.org 모두
       curl exit 92(HTTP/2 stream not closed cleanly)로 실패하며,
       --http1.1/--http1.0/--no-keepalive는 exit 28 timeout으로 악화된다.
       60/120/180초 누적 대기로도 미회복. 동일 시점 example.com·CBOE·
       federalreserve.gov는 200을 반환하므로 망 문제가 아니다.
       ★따라서 호출 방식으로 고칠 수 있는 결함이 아니다.
     [해법] 재배포처가 아니라 원발행처를 1차로 삼는다.
       · S4 VIXCLS  → CBOE VIX_History.csv (원발행처).
                      FRED와 9/9 관측 일치 실측(2026-08-26). FRED보다 최신.
       · S8 DTWEXBGS → 연준 H.10 JRXWTFB_N.B (원발행처).
                      FRED와 6/6 관측 일치 실측(2026-08-26).
     [부수] ★1차 취득분만 캐시에 저장한다(2차·캐시가 방어선을 덮지 않음).
       산출물에 tier(1차/2차/캐시)와 primary_ok(계열별 1차 성공 여부)를
       기록해 위클리 [8] 캐시 반출 판단을 자동화한다.
     [실측] 3계열 중 2계열 1차 취득 성공, 총 2분 03초 완주(v1.2는 4분 50초).
     ★판정 로직(SIGNALS·percentile·scan·zscore_events)은 v1.0 이후 무변경.

v1.4 (2026-08-26) ★S7 1차 복구 경로 — FRED API 호스트 편입.
     [발견] 차단된 것은 웹 호스트 `fred.stlouisfed.org` 하나뿐이다.
       API 호스트 `api.stlouisfed.org`는 **정상 도달**하며
       {"error_code":400,"error_message":"Variable api_key is not set"}
       를 반환한다(2026-08-26 실측). 즉 FRED 자체가 죽은 것이 아니라
       그래프/CSV 배포 경로만 막혀 있다.
     [해법] S7(BAMLH0A0HYM2)의 1차를 FRED API로 올린다. 무료 키 1개면
       된다. 키가 없으면 자동으로 기존 경로(fredgraph → 캐시)로 폴백하므로
       ★키 없이도 v1.3과 동일하게 동작한다(하위 호환, 실측 확인).
     [키 공급 경로] 우선순위 — 환경변수 FRED_API_KEY →
       ./sentinel_out/fred_api_key.txt → 미설정 시 이 소스 건너뜀.
     ★S7 신호를 제거하지 않는다. 8신호 체계와 단계 명칭표(원칙 ④),
       부칙 A의 v2기준 카운트는 모두 N/8을 전제로 하므로, 신호 제거는
       SSOT 정식 개정(§2-① 카운트 SSOT) 사항이며 L4 권한 밖이다.
"""

import argparse, csv, io, json, math, os, random, shutil, subprocess, sys, tempfile, time, urllib.request
from datetime import date, datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel_out")
UA = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 40

# ── SSOT §1 / §1-1 파라미터 ────────────────────────────────────────────
# direction: "low"  = 값이 낮을수록 위험 (활성 <p_on, 해제 >p_off)
#            "high" = 값이 높을수록 위험 (활성 >p_on, 해제 <p_off)
SIGNALS = {
    "S4_VIX_LOW":  dict(series="VIXCLS",       window_y=1, direction="low",  p_on=10, p_off=20, k_on=2, k_off=2),
    "S4_VIX_HIGH": dict(series="VIXCLS",       window_y=1, direction="high", p_on=90, p_off=80, k_on=2, k_off=2),
    "S7_HY_OAS":   dict(series="BAMLH0A0HYM2", window_y=3, direction="low",  p_on=10, p_off=20, k_on=2, k_off=2),
    "S8_DXY_LVL":  dict(series="DTWEXBGS",     window_y=3, direction="high", p_on=90, p_off=80, k_on=2, k_off=2),
}
Z_SIGMA_WINDOW = 30      # S8 [변화] 30일 σ
Z_THRESHOLD    = 2.0     # z > ±2σ
Z_VALID_DAYS   = 30      # §2-③ 이탈일 +30 달력일


# ── HTTP 전송 계층 (환경 이식성) ──────────────────────────────────────
# 일부 실행 환경(프록시 경유 클라우드 샌드박스)에서는 urllib/requests가
# 프록시 CONNECT 이후 응답을 읽지 못하고 정지한다. curl은 정상 동작하므로
# urllib 1회 시도 후 curl로 폴백한다. 사용된 전송 방식을 기록한다.
TRANSPORT_LOG = []
FETCH_LOG = []            # 취득 시도 이력 (계열, 시도수, 결과)
_URLLIB_DEAD = False      # urllib이 한 번 실패하면 이후 전역 생략
RETRY_WAITS = [5, 15, 30, 45]       # v1.2: 4회 재시도(총 5시도)
FETCH_BUDGET_SEC = 300              # v1.2: 회당 취득 총 예산. 초과 시 즉시 폴백
_FETCH_T0 = time.time()

def _budget_left():
    return FETCH_BUDGET_SEC - (time.time() - _FETCH_T0)

def _curl(url, timeout):
    """curl -o 임시파일 방식. 파이프 캡처를 쓰지 않는다(v1.2)."""
    fd, path = tempfile.mkstemp(suffix=".dat"); os.close(fd)
    try:
        pr = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", str(timeout),
             "-H", f"User-Agent: {UA['User-Agent']}",
             "-o", path, "-w", "%{http_code}", url],
            capture_output=True, text=True)
        code = (pr.stdout or "").strip()
        if code != "200":
            raise RuntimeError(f"curl rc={pr.returncode} http={code or '000'} {(pr.stderr or '')[:60].strip()}")
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    finally:
        try: os.unlink(path)
        except OSError: pass

def _http_get(url, timeout=TIMEOUT, label=""):
    """urllib 1회(살아 있을 때만) → curl. 실패 시 예외."""
    global _URLLIB_DEAD
    errs = []
    if not _URLLIB_DEAD:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=min(timeout, 15)) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                body = r.read().decode()
            TRANSPORT_LOG.append("urllib")
            return body
        except Exception as e:
            _URLLIB_DEAD = True          # 이 환경에서는 재시도 무의미
            errs.append(f"urllib:{e}")
    if not shutil.which("curl"):
        raise RuntimeError(" | ".join(errs) + " | curl 미설치")
    try:
        body = _curl(url, timeout)
        TRANSPORT_LOG.append("curl")
        return body
    except Exception as e:
        errs.append(str(e))
        raise RuntimeError(" | ".join(errs))

# ── 취득 ──────────────────────────────────────────────────────────────
def _parse_csv(text):
    rows = list(csv.reader(io.StringIO(text)))[1:]
    return [(r[0], float(r[1])) for r in rows if len(r) > 1 and r[1] not in (".", "", "NA")]

def fetch_fred(series):
    """FRED 웹 호스트(fredgraph.csv). 이 환경에서는 상시 불안정 — 2순위 이하로 둔다.
    v1.2: 5시도 + 광폭 간격(지터) + 총 예산 상한."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    last = None
    for i in range(len(RETRY_WAITS) + 1):
        try:
            data = _parse_csv(_http_get(url, label=series))
            if len(data) < 60:
                raise RuntimeError(f"관측치 부족 n={len(data)}")
            return data, "FRED web"
        except Exception as e:
            last = e
            if i >= len(RETRY_WAITS):
                break
            wait = RETRY_WAITS[i] + random.uniform(0, 5)
            if _budget_left() <= wait:      # 예산 소진 → 재시도 포기, 폴백으로
                raise RuntimeError(f"취득 예산 {FETCH_BUDGET_SEC}s 소진 — {str(last)[:50]}")
            time.sleep(wait)
    raise RuntimeError(str(last))

def fetch_cboe_vix():
    """★S4 1차 — VIX 원발행처. FRED VIXCLS와 값 동일함을 실측 검증(2026-08-26, 9/9 일치)."""
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    out = []
    for r in list(csv.reader(io.StringIO(_http_get(url))))[1:]:
        if len(r) < 5 or not r[4]:
            continue
        try:
            out.append((datetime.strptime(r[0], "%m/%d/%Y").date().isoformat(), float(r[4])))
        except ValueError:
            continue
    return out, "CBOE VIX_History"

def _fred_api_key():
    """환경변수 → 키 파일 순. 없으면 None(해당 소스 건너뜀)."""
    k = os.environ.get("FRED_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(OUT, "fred_api_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            k = f.read().strip()
        if k:
            return k
    return None

def fetch_fred_api(series):
    """★FRED API 호스트(api.stlouisfed.org). 웹 호스트가 차단돼도 이쪽은 살아 있다.
    무료 키 필요: https://fred.stlouisfed.org/docs/api/api_key.html"""
    key = _fred_api_key()
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정 — 건너뜀")
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series}&api_key={key}&file_type=json"
           "&observation_start=2018-01-01")
    d = json.loads(_http_get(url))
    if "observations" not in d:
        raise RuntimeError(f"응답 형식 오류: {str(d)[:80]}")
    out = []
    for o in d["observations"]:
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    return out, "FRED API"

FED_H10_DXY_KEY = "122e3bcb627e8e53f1bf72a1a09cfb81"   # H10/H10/JRXWTFB_N.B

def fetch_fed_h10_dxy():
    """★S8 1차 — Nominal Broad Dollar Index 원발행처(연준 H.10).
    FRED DTWEXBGS와 동일 계열이며 값 일치를 실측 검증(2026-08-26, 6/6 일치).
    주의: from/to 파라미터는 빈 응답을 반환한다. lastobs=<N> 만 사용할 것."""
    url = ("https://www.federalreserve.gov/datadownload/Output.aspx?rel=H10"
           f"&series={FED_H10_DXY_KEY}&lastobs=1300&from=&to=&filetype=csv"
           "&label=include&layout=seriescolumn")
    out = []
    for r in csv.reader(io.StringIO(_http_get(url))):
        if len(r) < 2 or r[0][:2] != "20" or r[1] in ("", "ND"):
            continue
        try:
            out.append((r[0], float(r[1])))
        except ValueError:
            continue
    return out, "Fed H.10 JRXWTFB_N.B"

def fetch_yahoo_vix():
    """2차 소스 — S4 전용. CBOE·FRED 모두 실패 시에만 사용."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=2y&interval=1d"
    d = json.loads(_http_get(url))
    res = d["chart"]["result"][0]
    ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), round(c, 2)) for t, c in zip(ts, cl) if c]
    return out, "Yahoo ^VIX (2차)"

def load_cache(series):
    p = os.path.join(OUT, f"cache_{series}.csv")
    if not os.path.exists(p):
        raise RuntimeError("캐시 없음")
    with open(p, encoding="utf-8") as f:
        return _parse_csv(f.read()), "캐시(스냅샷)"

def save_cache(series, data):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"cache_{series}.csv"), "w", encoding="utf-8") as f:
        f.write("observation_date,value\n")
        f.writelines(f"{d},{v}\n" for d, v in data)

_ACQUIRE_MEMO = {}

def _sources_for(series):
    """(호출함수, 소스명, tier) 순서 목록. tier '1차'는 원발행처 또는 FRED.
    v1.3: FRED 웹 호스트가 상시 불안정하므로 원발행처를 1순위로 올렸다.
    v1.4: FRED API 호스트를 별도 소스로 편입(웹 호스트와 별개로 살아 있음)."""
    if series == "VIXCLS":
        return [(fetch_cboe_vix,                   "CBOE",     "1차"),
                (lambda: fetch_fred_api(series),   "FRED API", "1차"),
                (lambda: fetch_fred(series),       "FRED web", "1차"),
                (fetch_yahoo_vix,                  "Yahoo",    "2차")]
    if series == "DTWEXBGS":
        return [(fetch_fed_h10_dxy,                "Fed H.10", "1차"),
                (lambda: fetch_fred_api(series),   "FRED API", "1차"),
                (lambda: fetch_fred(series),       "FRED web", "1차")]
    # BAMLH0A0HYM2 (ICE BofA HY OAS) — ICE 라이선스 계열로 재배포처가 FRED뿐이다.
    # v1.4: 웹 호스트가 막혀도 API 호스트는 살아 있으므로 API를 1순위로 둔다.
    return [(lambda: fetch_fred_api(series),       "FRED API", "1차"),
            (lambda: fetch_fred(series),           "FRED web", "1차")]

def acquire(series):
    """소스 체인 → 캐시. 반환: (data, source, tier, degraded)
    v1.3: ★1차 취득분만 캐시에 저장한다. 2차·캐시 데이터로 방어선을 덮지 않는다."""
    if series in _ACQUIRE_MEMO:
        return _ACQUIRE_MEMO[series]
    errs = []
    for fn, name, tier in _sources_for(series):
        try:
            d, label = fn()
            if len(d) < 60:
                raise RuntimeError(f"관측치 부족 n={len(d)}")
            if tier == "1차":
                save_cache(series, d)          # 1차만 방어선 갱신
            FETCH_LOG.append(f"{series}: {label} 취득 성공 [{tier}] 최신 {d[-1][0]}")
            _ACQUIRE_MEMO[series] = (d, label, tier, tier != "1차")
            return _ACQUIRE_MEMO[series]
        except Exception as e:
            errs.append(f"{name}:{str(e)[:70]}")
            FETCH_LOG.append(f"{series}: {name} 실패 — {str(e)[:70]}")
    try:
        d, label = load_cache(series)
        FETCH_LOG.append(f"{series}: 캐시 폴백 [캐시] 최신 {d[-1][0]}")
        _ACQUIRE_MEMO[series] = (d, label, "캐시", True)
        return _ACQUIRE_MEMO[series]
    except Exception as e:
        errs.append(f"cache:{e}")
    raise RuntimeError(" | ".join(errs))

# ── 산출 ──────────────────────────────────────────────────────────────
def slice_window(data, years):
    """§2-⑥ 개정: 엔드포인트 파라미터가 아니라 수신 후 로컬 슬라이싱으로 확정."""
    cut = (date.today() - timedelta(days=int(365.25 * years))).isoformat()
    return [x for x in data if x[0] >= cut]

def percentile(vals, p):
    """§2-⑧: 정렬 시계열의 하위 p% 위치 값 (선형보간)."""
    s = sorted(vals); k = (len(s) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

def scan(data, cfg, since=None):
    """§1-B B-2 + §2-⑨: 전 영업일 연속 스캔, 비대칭 밴드 + k연속 확인."""
    win = slice_window(data, cfg["window_y"])
    vals = [x[1] for x in win]
    c_on  = percentile(vals, cfg["p_on"])
    c_off = percentile(vals, cfg["p_off"])
    low = cfg["direction"] == "low"

    seq = [x for x in win if (since is None or x[0] >= since)]
    # 상태 확정을 위해 스캔 시작 전 이력도 필요 → 윈도우 전체로 상태를 만들고, 표시만 seq로 자른다
    state, so, sn, events, trace = False, 0, 0, [], []
    for d, v in win:
        ok  = (v < c_on)  if low else (v > c_on)
        off = (v > c_off) if low else (v < c_off)
        if ok:    so += 1; sn = 0; zone = "충족"
        elif off: sn += 1; so = 0; zone = "해제조건"
        else:     so = sn = 0;     zone = "중립대"
        ev = ""
        if not state and so >= cfg["k_on"]:
            state, ev = True, "활성확정"; events.append((d, "활성확정"))
        elif state and sn >= cfg["k_off"]:
            state, ev = False, "해제확정"; events.append((d, "해제확정"))
        if since is None or d >= since:
            trace.append(dict(date=d, value=v, zone=zone, state="활성" if state else "비활성", event=ev))
    return dict(active=state, cut_on=round(c_on, 4), cut_off=round(c_off, 4),
                n=len(vals), window=f"{win[0][0]}~{win[-1][0]}",
                latest=win[-1], events=events[-6:], trace=trace[-15:])

def zscore_events(data):
    """S8 [변화]: 일간수익률 z>±2σ(30일 σ), 이탈일 +30 달력일 유효."""
    win = slice_window(data, 1)
    v = [x[1] for x in win]
    rets = [(win[i][0], v[i] / v[i - 1] - 1) for i in range(1, len(v))]
    out, today = [], date.today()
    for i in range(Z_SIGMA_WINDOW, len(rets)):
        w = [r for _, r in rets[i - Z_SIGMA_WINDOW:i]]
        mu = sum(w) / len(w)
        sd = math.sqrt(sum((x - mu) ** 2 for x in w) / len(w))
        if sd == 0: continue
        d, r = rets[i]
        z = (r - mu) / sd
        if abs(z) > Z_THRESHOLD:
            exp = (date.fromisoformat(d) + timedelta(days=Z_VALID_DAYS))
            out.append(dict(date=d, ret_pct=round(r * 100, 3), z=round(z, 2),
                            expires=exp.isoformat(), valid=exp >= today))
    return out

# ── 실행 ──────────────────────────────────────────────────────────────
def run(since=None):
    res, degraded = {}, []
    for key, cfg in SIGNALS.items():
        try:
            data, src, tier, deg = acquire(cfg["series"])
            r = scan(data, cfg, since)
            r.update(source=src, tier=tier, degraded=deg, verdict="확정")
            if deg: degraded.append(f"{key}: {src} [{tier}]")
            if key == "S8_DXY_LVL":
                r["change_events"] = zscore_events(data)
            res[key] = r
        except Exception as e:
            # §1-B B-3: 실패 시 판정 유보. '비활성 확정'을 선언하지 않는다.
            res[key] = dict(verdict="판정유보(carry)", error=str(e), active=None)
            degraded.append(f"{key}: 취득 실패 — {e}")
    tl = sorted(set(TRANSPORT_LOG))
    primary = {k: (v.get("tier") == "1차") for k, v in res.items() if v.get("verdict") == "확정"}
    series_primary = {}
    for k, cfg in SIGNALS.items():
        if primary.get(k):
            series_primary[cfg["series"]] = True
        else:
            series_primary.setdefault(cfg["series"], False)
    return dict(asof=date.today().isoformat(), engine=ENGINE_VERSION, signals=res,
                degraded=degraded, transport=tl, fetch_log=FETCH_LOG,
                primary_ok=series_primary)

def to_markdown(out):
    L = [f"### Sentinel 정량 신호 산출 — {out['asof']} "
         f"(sentinel_compute.py {out.get('engine','?')}, SSOT §1-B)", ""]
    L += ["| 신호 | 최신값(기준일) | 활성컷 | 해제컷 | 판정 | 최근 이벤트 | 소스 |",
          "|:--|--:|--:|--:|:--|:--|:--|"]
    for k, r in out["signals"].items():
        if r["verdict"] != "확정":
            L.append(f"| {k} | — | — | — | ⚠ **판정유보** | {r['error'][:40]} | — |"); continue
        d, v = r["latest"]
        ev = r["events"][-1] if r["events"] else ("—", "—")
        mark = "🔴 **활성**" if r["active"] else "⚪ 비활성"
        L.append(f"| {k} | {v} ({d}) | {r['cut_on']} | {r['cut_off']} | {mark} | {ev[1]} {ev[0]} | {r['source']} [{r.get('tier','?')}] |")
    s8 = out["signals"].get("S8_DXY_LVL", {})
    for e in s8.get("change_events", []):
        if e["valid"]:
            L.append("")
            L.append(f"- **S8 [변화] 유효 이벤트**: {e['date']} z={e['z']} ({e['ret_pct']}%), 만료 {e['expires']}")
    if out["degraded"]:
        L += ["", "**⚠ 열화 보고 (§1-B B-3)**"] + [f"- {x}" for x in out["degraded"]]
    if out.get("transport"):
        L += ["", f"_전송: {', '.join(out['transport'])}_"]
    if out.get("fetch_log"):
        L += ["", "**취득 시도 이력**"] + [f"- {x}" for x in out["fetch_log"]]
    if out.get("primary_ok"):
        ok = [x for x, v in out["primary_ok"].items() if v]
        ng = [x for x, v in out["primary_ok"].items() if not v]
        L += ["", f"**1차 취득 성공 — 캐시 반출 대상**: {', '.join(ok) if ok else '없음'}"]
        if ng:
            L += [f"**1차 실패 — 반출 금지**: {', '.join(ng)}"]
    return "\n".join(L)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="직전 확정일 YYYY-MM-DD (이후 전 영업일 스캔 표시)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = run(a.since)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"snapshot_{out['asof']}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2) if a.json else to_markdown(out))
    sys.exit(1 if out["degraded"] else 0)