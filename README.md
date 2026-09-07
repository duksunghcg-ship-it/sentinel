# sentinel
Master Strategist 체계의 Sentinel 8신호 자동 산출 저장소.

- `engine/` 계산 엔진(v1.4, 판정 로직 무변경)·픽스처·실행기
- `state/latest.json` 최신 판정 (주간 브리핑의 유일한 입력) · `state/history.csv` 일자별 판정 누적 · `state/nonquant.json` 비정량 5신호(사람이 갱신)
- `cache/` 시계열 방어선(1차 취득 성공 시 자동 갱신)
- `ssot/SSOT.md` 임계표 정본
- 매일 05:30 KST 자동 실행. 카운트 변동·신규 이탈·판정유보·소스 열화·분기 만료 D-3 이면 Issue 생성(메일 통지)

## 초기 설정 (상세는 SENTINEL_사용자조치안내 문서)
1. 저장소는 Public 으로 만든다 → 주간 브리핑이 `https://raw.githubusercontent.com/<아이디>/sentinel/main/state/latest.json` 을 토큰 없이 읽는다
2. Settings › Secrets and variables › Actions: `FRED_API_KEY` 등록
3. Actions 탭에서 `sentinel-daily` 수동 실행 1회 → state/latest.json 생성 확인
4. (선택) Private 저장소로 두려면 GIST_TOKEN·GIST_ID 를 추가 등록하면 공개 Gist 로 미러된다
5. 엔진·임계표 변경은 PR로만
