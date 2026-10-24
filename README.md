# 배포 현황 보드 (deploy-board)

웹·API 레포의 **develop → 스테이징(deploy/prod) → 운영(swap)** 상태와, **Jira 작업(이슈 키)별로 지금 어느 단계인지**를 한 화면에서 본다. 토이 프로젝트 — 원격 레포·배포 없음.

## 실행
```
python server.py            # 시작하면서 수집 → http://localhost:8765
python server.py --no-collect --open
python collector.py --repo adminsite   # 수집만(서버 없이) → data/state.json
python -m unittest test_collector      # 단계 판정 테스트
```
전제: 이 PC 에 `gh`(GitHub 로그인)·`az`(Azure 로그인, Reader)·`git` 이 있고, `../projects/<repo>` 에 기준 클론이 있다(수집기는 `git fetch` 만 하고 checkout 하지 않는다).

## 화면
- 레포마다 세 칸: develop HEAD / 스테이징(deploy/prod HEAD) / 운영(추정 SHA, 마지막 swap 시각·실행자).
- 5열 보드: 브랜치 → PR → develop → 스테이징 → 운영. 이슈 키 칩을 누르면 브랜치·PR·커밋·Jira 링크 패널.
- 검색(키·제목)·기간(7/30/90일/전체)·브랜치만 있는 것 숨김. 「새로고침」은 백그라운드 재수집(40~60초), 「10분마다 자동」 옵션.
- 접힌 「스테이징에만 있는 커밋 · develop 에만 있는 커밋 · 운영 SHA 근거」에 원천 목록.

## 수집 원천과 판정
| 항목 | 원천 |
|---|---|
| develop / deploy/prod HEAD, 브랜치 목록 | 로컬 클론 `git fetch --prune` → `log`·`for-each-ref` |
| develop 에만 있는 커밋 | `git log --no-merges --cherry-pick --right-only origin/deploy/prod...origin/develop` (cherry-pick 된 같은 패치 제외) |
| PR(상태·base·머지 커밋) | `gh pr list --state all -L 300` |
| 스테이징 배포 run | `gh run list --workflow deploy.yml -b deploy/prod` |
| swap 기록 | `az monitor activity-log list --resource-id <site>` 의 `slotsswap` Succeeded (리소스 그룹 필터로는 안 나오고, 작업 이름 대소문자가 섞여 있어 소문자로 비교) |
| 운영 SHA(추정) | 마지막 swap 이전에 끝난 마지막 성공 배포 run 의 headSha — swap back·포털 수동 배포가 있으면 틀릴 수 있어 화면에 「추정」과 근거를 같이 보여 준다 |
| 스테이징에만 있는 커밋 | `git log --cherry-pick <운영SHA>...origin/deploy/prod` |

이슈 키별 단계(높은 것이 이김): 브랜치만 → PR 열림 → develop 머지(develop 대상 PR 머지 또는 develop 에만 있는 커밋) → 스테이징(deploy/prod 대상 PR 머지 또는 스테이징에만 있는 커밋) → 운영(deploy/prod 머지 커밋이 운영 SHA 의 조상). 운영인데 같은 키의 커밋이 스테이징에 또 있으면 스테이징으로 내리고 꼬리표를 단다.

## 설정 `repos.json`
`repos[]` 에 `name`(GitHub 레포)·`app`(Azure App Service 이름, 없으면 운영 판정 생략)·`workflow`·`label`·선택 `local`(클론 경로). `issue_key_pattern` 으로 이슈 키 접두어를 제한한다(제목 속 `AC-35` 같은 오인 방지).

## 다음(픽셀아트)
기능이 자리 잡으면 5열 보드를 캔버스 사무실로 바꾼다 — 열 = 방(대기실·리뷰실·개발실·스테이징·운영 서버실), 이슈 = 캐릭터, 단계가 바뀌면 다음 방으로 걸어간다. 에셋 후보·방법은 `../notes/배포-현황/작업-참고.md`.
