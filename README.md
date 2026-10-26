# 배포 현황 보드 (deploy-board)

웹·API 레포의 **develop → 스테이징(deploy/prod) → 운영(swap)** 상태와, **Jira 작업(이슈 키)별로 지금 어느 단계인지**를 한 화면에서 본다. 토이 프로젝트 — 로컬 PC 에서 돌리는 용도이고 배포하지 않는다. AI 는 쓰지 않는다(git·gh·az CLI 조회와 정규식뿐).

## 실행
```
python server.py            # 시작하면서 수집, 변경 감시 켜짐 → http://localhost:8765
python server.py --no-collect --open --check 30 --swap-check 120   # --no-watch 로 감시 끄기
python server.py --bind 127.0.0.1      # 이 PC 에서만 접속(아래 「주의」)
python collector.py --repo adminsite   # 수집만(서버 없이) → data/state.json
python -m unittest test_collector      # 단계 판정 테스트
```
전제: 이 PC 에 `gh`(GitHub 로그인)·`az`(Azure 로그인, Reader)·`git` 이 있고, `../projects/<repo>` 에 기준 클론이 있다(수집기는 `git fetch` 만 하고 checkout 하지 않는다).

## 주의 — 내 계정으로 나가는 요청
- 수집은 **실행한 사람의 `gh`·`az`·SSH 자격 증명**으로 돈다. 호출은 전부 읽기(git fetch/ls-remote/log, gh pr list·run list·GET pulls, az account show·activity-log list·webapp show·slot list)이고 쓰기·삭제·swap 실행은 없다. 변경 감시가 켜져 있으면 실측 시간당 GitHub REST 약 250건, Azure 읽기 약 90건, 레포당 `ls-remote` 약 85회가 계속 나간다(한도의 5% 아래).
- 서버는 **모든 주소(0.0.0.0)에 열리고 인증이 없다.** `POST /api/refresh` 를 같은 망의 누구나 부를 수 있고, 그러면 수집이 내 계정으로 돈다(실행 중이면 409 로 거절하므로 최대 70초에 한 번). 혼자 볼 때는 상관없지만 남에게 주소를 알려 주거나 공용 망에서 띄울 때는 `--bind 127.0.0.1` 로 묶거나 방화벽으로 8765 를 막는다. 외부 접근 주소에는 그대로 올리지 않는다.
- `data/state.json` 에는 브랜치·커밋 메시지·PR 제목·swap 실행자 이메일이 들어 있다. gitignore 되어 있으니 레포에 올리지 말고, 공유할 때도 이 점을 안다.
- `repos.json` 에는 조직·리소스 그룹·앱 이름이 있다. 비밀값은 아니지만 공개 레포에서는 드러난다.

## 화면
- 레포마다 세 칸: develop HEAD / 스테이징(deploy/prod HEAD) / 운영(추정 SHA, 마지막 swap 시각·실행자).
- 5열 보드: 브랜치 → PR → develop → 스테이징 → 운영. 이슈 키 칩을 누르면 브랜치·PR·커밋·Jira 링크 패널.
- 검색(키·제목)·기간(7/30/90일/전체)·브랜치만 있는 것 숨김. 「새로고침」은 전체 즉시 재수집(40~60초). 헤더의 「자동 감시 …」가 감시 상태와 최근 변화.
- 접힌 「스테이징에만 있는 커밋 · develop 에만 있는 커밋 · 운영 SHA 근거」에 원천 목록.

## 수집 원천과 판정
| 항목 | 원천 |
|---|---|
| develop / deploy/prod HEAD, 브랜치 목록 | 로컬 클론 `git fetch --prune` → `log`·`for-each-ref` |
| develop 에만 있는 커밋 | `git log --no-merges --cherry-pick --right-only origin/deploy/prod...origin/develop` (cherry-pick 된 같은 패치 제외) |
| PR(상태·base·머지 커밋) | `gh pr list --state all -L 300` |
| 스테이징 배포 run | `gh run list --workflow deploy.yml -b deploy/prod` |
| swap 기록 | `az monitor activity-log list --resource-id <site> --max-events 5000` 의 `slotsswap` Succeeded (리소스 그룹 필터로는 안 나오고, 작업 이름 대소문자가 섞여 있어 소문자로 비교. 기본 `--max-events 50` 이면 배포가 잦은 날 swap 이 잘려 운영 판정이 사라진다) |
| 운영 SHA(추정) | 마지막 swap 이전에 끝난 마지막 성공 배포 run 의 headSha — swap back·포털 수동 배포가 있으면 틀릴 수 있어 화면에 「추정」과 근거를 같이 보여 준다 |
| 스테이징에만 있는 커밋 | `git log --cherry-pick <운영SHA>...origin/deploy/prod` |

이슈 키별 단계(높은 것이 이김): 브랜치만 → PR 열림 → develop 머지(develop 대상 PR 머지 또는 develop 에만 있는 커밋) → 스테이징(deploy/prod 대상 PR 머지 또는 스테이징에만 있는 커밋) → 운영(deploy/prod 머지 커밋이 운영 SHA 의 조상). 운영인데 같은 키의 커밋이 스테이징에 또 있으면 스테이징으로 내리고 꼬리표를 단다.

## 변경 감시(거의 실시간)
서버가 켜져 있으면 기본으로 돈다(`--no-watch` 로 끔).
- 30초마다(`--check`) 레포별로 가벼운 조회 2건 — `git ls-remote --heads origin`(브랜치 끝 커밋 전체 1회)과 가장 최근 갱신된 PR 시각(API 1회). 지난번과 다르면 그 레포만 다시 수집해 `state.json` 에 합친다(`merge_state`).
- 2분마다(`--swap-check`) Azure 활동 기록(최근 7일, 앱당 약 8초)에서 마지막 swap 시각을 보고 바뀌면 그 레포를 다시 수집한다.
- 화면은 10초마다 `/api/status` 를 물어 `collected_at` 이 바뀌면 다시 그린다 → 단계가 바뀐 캐릭터가 걸어간다.
- 비용(실측, 레포 3개): tick 이 30초 대기 + 약 13초 조회라 실제 간격은 약 43초. 변화가 없을 때 시간당 GitHub REST 약 250건 + ls-remote 약 250회, Azure 약 90건. 한도(GitHub 5,000/h, Azure 12,000/h)의 5% 아래.

웹훅(GitHub push/PR, Azure Event Grid 의 swap 이벤트)을 쓰면 폴링 없이 즉시 반영되지만, GitHub·Azure 가 호출할 수 있는 **외부 접근 주소가 필요해 지금은 안 된다**(로컬 PC·사내 개발 서버 모두 밖에서 못 들어온다). 나중에 중계(터널·공개 엔드포인트)가 생기면 그 훅이 `POST /api/refresh` 에 `{"repos":["<레포>"]}` 를 보내면 된다 — 엔드포인트는 이미 있다.

## 설정 `repos.json`
`repos[]` 에 `name`(GitHub 레포)·`app`(Azure App Service 이름, 없으면 운영 판정 생략)·`workflow`·`label`·선택 `local`(클론 경로). `issue_key_pattern` 으로 이슈 키 접두어를 제한한다(제목 속 `AC-35` 같은 오인 방지).

## 픽셀 사무실 (`web/pixel.js`)
레포마다 캔버스 하나. 방 5개 = 단계(대기실·리뷰실·개발실·스테이징·운영 서버실), 캐릭터 하나 = 이슈 하나(머리 위 번호). 캐릭터를 누르면 상세 패널. 새로고침으로 단계가 바뀐 이슈는 복도를 따라 다음 방으로 걸어가고, 새 이슈는 왼쪽 입구로 들어오며, 목록에서 빠진 이슈는 오른쪽으로 나간다(지난 단계는 브라우저 localStorage 에 기억). 방 자리는 18명, 넘치면 방 이름에 「+N 자리 없음」. 헤더 「보기」로 픽셀 사무실 / 표(5열 보드) / 둘 다를 고른다(브라우저에 기억, 기본 픽셀). 표 모드에서는 캔버스를 만들지 않아 애니메이션 비용이 없다.
외부 라이브러리·빌드 없음(Canvas 2D). 월드는 16px 타일로 그려 화면 폭에 맞춰 정수가 아니어도 확대(`image-rendering: pixelated`). 에셋 출처는 `web/assets/CREDITS.md`.
