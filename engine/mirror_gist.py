#!/usr/bin/env python3
"""공개 Gist에 파일을 미러링한다(주간 브리핑 작업이 읽는 경로).
환경변수 GIST_TOKEN(gist 권한), GIST_ID 필요. 사용: python engine/mirror_gist.py state/latest.json state/history.csv"""
import json, os, sys, urllib.request
tok, gid = os.environ.get("GIST_TOKEN", ""), os.environ.get("GIST_ID", "")
if not (tok and gid):
    print("GIST_TOKEN/GIST_ID 없음 — 미러 생략"); sys.exit(0)
files = {os.path.basename(p): {"content": open(p, encoding="utf-8").read()} for p in sys.argv[1:]}
req = urllib.request.Request(f"https://api.github.com/gists/{gid}", data=json.dumps({"files": files}).encode(),
                             method="PATCH", headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print("GIST 미러 완료", r.status)
