#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_ingest.py v0.2 (2026-09-14) — 데일리 브리핑 [예측] 블록 수집·파싱

입력 경로 2종을 모두 지원한다.
  (b) 이슈 댓글  : --issue N  (기본값. GH_TOKEN 또는 GITHUB_TOKEN 사용)
  (a) 커밋 파일  : --inbox forecast/inbox  (*.txt 를 읽는다)

출력
  forecast/predictions.csv   1행 1예측, 추가 전용. (date,target) 중복은 최초 1건만 채택
  forecast/parse_errors.csv  형식 불일치 원문과 사유

판정 규칙
  - 「휴장」이 포함된 줄은 예측 없음으로 보고 건너뛴다(오류 아님).
  - 방향은 상승/하락 2종만 허용한다. 중립·보합·횡보는 오류로 남긴다.
  - 확률은 50~90 정수만 허용한다.
  - 근거분류는 매크로/수급/이벤트/신호감지 4종만 허용한다.
  - 댓글 하나에 [예측] 블록이 여러 개 있으면(며칠치를 몰아 붙인 경우) 전부 읽는다.
  - 같은 일자·대상이 두 번 들어오면 최초 1건만 채택하고 나머지는 오류 표에 남긴다.
사용: python engine/forecast_ingest.py --out forecast --issue 1
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request

TARGETS = {"코스피": "KOSPI", "S&P500": "SP500", "S&P 500": "SP500"}
DIRECTIONS = {"상승", "하락"}
REASONS = {"매크로", "수급", "이벤트", "신호감지"}

HEAD_RE = re.compile(r"\[예측\]\s*(\d{4}-\d{2}-\d{2})")
LINE_RE = re.compile(
    r"^\s*(?P<target>S&P\s?500|코스피)\s*종가[^:：]*[:：]\s*(?P<body>.+?)\s*$"
)
PRED_COLS = ["date", "target", "close_date", "direction", "prob", "reason_class", "source"]
ERR_COLS = ["captured_at", "source", "raw", "reason"]


def http_json(url, token=None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "forecast-ingest"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def read_rows(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)]


def write_rows(path, cols, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def parse_block(text, source):
    """댓글·파일 1건 안의 [예측] 블록 전부를 (예측행 리스트, 오류행 리스트)로 변환한다."""
    preds, errs = [], []
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return preds, errs  # 예측 블록이 아닌 댓글은 조용히 무시한다
    for i, m in enumerate(heads):
        d = m.group(1)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        tail = text[m.end():end]
        for raw in tail.splitlines():
            line = raw.strip().replace("％", "%").replace("：", ":")
            if not line:
                continue
            if "휴장" in line:
                continue
            lm = LINE_RE.match(line)
            if not lm:
                if "종가" in line:
                    errs.append({"source": source, "raw": raw, "reason": "줄 형식 불일치"})
                continue
            target = TARGETS[lm.group("target").replace(" ", "")]
            parts = [p.strip() for p in lm.group("body").split("/")]
            if len(parts) != 3:
                errs.append({"source": source, "raw": raw, "reason": "항목 3개(방향·확률·근거분류) 아님"})
                continue
            direction, prob_s, reason_s = parts
            if direction not in DIRECTIONS:
                errs.append({"source": source, "raw": raw, "reason": f"방향 허용값 아님({direction})"})
                continue
            pm = re.fullmatch(r"(\d{2})\s*%", prob_s)
            if not pm or not (50 <= int(pm.group(1)) <= 90):
                errs.append({"source": source, "raw": raw, "reason": f"확률 50~90 정수 아님({prob_s})"})
                continue
            reason = reason_s.replace("근거분류", "").strip()
            if reason not in REASONS:
                errs.append({"source": source, "raw": raw, "reason": f"근거분류 허용값 아님({reason})"})
                continue
            preds.append({"date": d, "target": target, "close_date": d,
                          "direction": direction, "prob": pm.group(1),
                          "reason_class": reason, "source": source})
    return preds, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="forecast")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "duksunghcg-ship-it/sentinel"))
    ap.add_argument("--issue", type=int, default=0, help="예측 기록함 이슈 번호. 0이면 이슈를 읽지 않는다")
    ap.add_argument("--inbox", default="", help="커밋 파일 경로. 비우면 읽지 않는다")
    a = ap.parse_args()

    ppath = os.path.join(a.out, "predictions.csv")
    epath = os.path.join(a.out, "parse_errors.csv")
    preds = read_rows(ppath, PRED_COLS)
    errs = read_rows(epath, ERR_COLS)
    seen = {(r["date"], r["target"]) for r in preds}
    seen_src = {r["source"] for r in preds} | {r["source"] for r in errs}

    new_p, new_e = [], []

    if a.issue:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        comments = []
        page = 1
        while True:  # 댓글이 100건을 넘어도 전부 읽는다(1년이면 250건)
            url = (f"https://api.github.com/repos/{a.repo}/issues/{a.issue}/comments"
                   f"?per_page=100&page={page}")
            try:
                batch = http_json(url, token)
            except Exception as e:
                print(f"[ingest] 이슈 댓글 조회 실패(page {page}): {e}", file=sys.stderr)
                break
            comments += batch
            if len(batch) < 100:
                break
            page += 1
        for c in comments:
            src = f"issue_comment:{c['id']}"
            if src in seen_src:
                continue
            p, er = parse_block(c.get("body", ""), src)
            new_p += p
            new_e += er

    if a.inbox and os.path.isdir(a.inbox):
        for fn in sorted(os.listdir(a.inbox)):
            if not fn.endswith(".txt"):
                continue
            src = f"commit:{fn}"
            if src in seen_src:
                continue
            with open(os.path.join(a.inbox, fn), encoding="utf-8") as f:
                p, er = parse_block(f.read(), src)
            new_p += p
            new_e += er

    added = 0
    for r in new_p:
        key = (r["date"], r["target"])
        if key in seen:
            new_e.append({"source": r["source"], "raw": json.dumps(r, ensure_ascii=False),
                          "reason": "동일 일자·대상 예측이 이미 등재됨 — 최초 1건만 채택"})
            continue
        seen.add(key)
        preds.append(r)
        added += 1

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in new_e:
        r["captured_at"] = now
    errs += new_e

    preds.sort(key=lambda r: (r["date"], r["target"]))
    write_rows(ppath, PRED_COLS, preds)
    write_rows(epath, ERR_COLS, errs)
    print(f"[ingest] 신규 예측 {added}건, 신규 오류 {len(new_e)}건, 누적 예측 {len(preds)}건")


if __name__ == "__main__":
    main()
