# 배포 현황 보드 (deploy-board)

웹·API 레포의 **develop → 스테이징(deploy/prod) → 운영(swap)** 상태와, **Jira 작업(이슈 키)별로 지금 어느 단계인지**를 한 화면에서 본다. 토이 프로젝트 — 수집은 각자 PC 에서 하고, 결과 파일만 개발 서버의 보기 전용 보드로 올려 공유할 수 있다. AI 는 쓰지 않는다(git·gh·az CLI 조회와 정규식뿐).

## 실행 방법 — 코드는 하나, 실행 방법은 셋
| 실행 방법 | 명령 | 수집 | 새로고침 | swap |
|---|---|---|---|---|
| 기본 | `python server.py` | 함 | 됨 | 웹 로그인(각자 Microsoft 계정) |
| 공유용(개발 서버) | `python3 server.py --readonly --port 8790` | 안 함(올려 준 결과만 보여 줌) | 안 됨(403) | 웹 로그인(각자 Microsoft 계정) |
| 내 PC 용 | `python server.py --allow-swap --bind 127.0.0.1` | 함 | 됨 | 이 PC 의 az(웹 로그인 꺼짐) |

- 코드에서 막는 것: `--readonly` 를 `--allow-swap`·`--publish` 와 같이 주면 시작하지 않는다. `--allow-swap` 은 `--bind` 가 127.0.0.1·localhost·::1 일 때만 시작한다(기본 0.0.0.0 이면 거부). 화면은 서버가 swap 을 켰다고 알려 주거나 웹 로그인으로 권한을 확인했을 때만 버튼을 그린다.
- 헤더 꼬리표로 지금 실행 방법을 보여 준다: 「공유 보기 전용」(30분 넘게 안 올라오면 「수집 PC 꺼짐?」), 「swap 켜짐」, 「공유 서버로 올림」.
- 수집 결과를 공유 서버에 올리려면 수집하는 PC 에서 `--publish datacrawler:deploy-board/data/state.json` 을 붙인다(수집이 끝날 때마다 ssh 로 임시 파일에 쓴 뒤 바꿔 끼운다. 실패하면 1분 뒤부터 최대 5분 간격으로 다시 올린다). 로컬 경로도 된다. 따로 한 번만: `python publish.py <대상>`.

```
python server.py --no-collect --open --check 30 --swap-check 60   # --no-watch 로 감시 끄기
python collector.py --repo adminsite   # 수집만(서버 없이) → data/state.json
python -m unittest   # 단계 판정·swap 권한 판정·화면 보안 헤더 테스트(test_*.py)
node test_weblogin.js                         # 웹 로그인 권한 판정(perm_cases.json 을 Python 과 같이 씀)
```
전제(수집하는 PC): `gh`(GitHub 로그인)·`az`(Azure 로그인, Reader)·`git` 이 있고, `../projects/<repo>` 에 기준 클론이 있다(수집기는 `git fetch` 만 하고 checkout 하지 않는다). 공유 서버에는 Python 3 만 있으면 된다.

## 주의 — 내 계정으로 나가는 요청
- 수집은 **실행한 사람의 `gh`·`az`·SSH 자격 증명**으로 돈다. 기본·공유용 실행에서 호출은 전부 읽기(git fetch/ls-remote/log, gh pr list·run list·GET pulls, az account show·activity-log list·webapp show·slot list·권한 조회)이고 쓰기·삭제·swap 실행은 없다. **쓰기는 `--allow-swap` 으로 띄우고 화면에서 직접 확인한 swap·되돌리기 하나뿐이다.** 웹 로그인 swap 은 서버가 아니라 그 사람의 브라우저가 자기 Microsoft 계정으로 보낸다(아래 절).
- 공유 서버에는 Azure·GitHub 로그인과 토큰을 두지 않는다. 공유용 실행은 수집하지 않으므로 필요 없다. 변경 감시가 켜져 있으면 실측 시간당 GitHub REST 약 250건, Azure 읽기 약 90건, 레포당 `ls-remote` 약 85회가 계속 나간다(한도의 5% 아래).
- 서버는 **모든 주소(0.0.0.0)에 열리고 인증이 없다.** `POST /api/refresh` 를 같은 망의 누구나 부를 수 있고, 그러면 수집이 내 계정으로 돈다(실행 중이면 409 로 거절하므로 최대 70초에 한 번). 혼자 볼 때는 상관없지만 남에게 주소를 알려 주거나 공용 망에서 띄울 때는 `--bind 127.0.0.1` 로 묶거나 방화벽으로 8765 를 막는다. 외부 접근 주소에는 그대로 올리지 않는다.
- `data/state.json` 에는 브랜치·커밋 메시지·PR 제목·swap 실행자 이메일, 구독 ID·리소스 그룹(`azure`, 웹 로그인 swap 주소용)이 들어 있다. gitignore 되어 있으니 레포에 올리지 말고, 공유 서버로 올릴 때도 같은 망 사람이 보게 된다는 점을 안다.

## swap (내 PC 용 `--allow-swap`)
- 시작할 때와 1시간마다 앱별로 내 권한을 조회한다(`GET {site}/providers/Microsoft.Authorization/permissions` — 역할마다 `actions` 에 맞고 `notActions` 에 안 걸리면 허용, 역할끼리는 합집합). 권한이 없으면 버튼이 꺼지고 툴팁(Tooltip, 마우스를 올리면 뜨는 짧은 설명)에 이유가 보인다.
- 버튼은 이럴 때 꺼진다: 권한 없음, 다른 swap 진행 중, 스테이징 배포 run 진행 중, 운영 SHA 미확인, 스테이징에만 있는 커밋 없음, 그 레포를 다시 수집하는 중.
- swap 을 누르면 모달 다이얼로그(Modal Dialog, 뒤 화면을 막고 확인을 받는 창)가 열려 운영·스테이징 SHA, 나갈 커밋·이슈 키, 실행 명령을 보여 준다. 앱 이름을 그대로 입력해야 실행 버튼이 켜지고 Esc 로 닫힌다. 실행: `az webapp deployment slot swap -g <리소스 그룹> -n <앱> --slot staging --target-slot production`(15분 제한).
- 끝나면 토스트(Toast, 잠깐 떴다 사라지는 알림)로 결과를 알리고 그 레포만 다시 수집한다. 이 보드에서 한 swap 은 30분 동안 「되돌리기」(같은 명령으로 다시 맞바꿈)가 보인다.
- 모든 실행은 `data/swap-log.jsonl` 에 남는다(누가·언제·무엇을·결과). Azure 활동 기록에도 내 이름으로 남는다. swap 할 때 서버가 `state.json` 에서 그 순간 나가는 이슈·커밋 목록(스냅숏)을 만들어 같이 남긴다(화면이 보낸 값은 참고로만 `client` 에).
- 헤더에 이 PC 의 로그인 상태(`az: 계정 (구독) · gh: 계정`)가 보인다(127.0.0.1 바인딩일 때만, 1시간마다 확인). 로그인이 풀렸으면 「로그인 필요」가 빨갛게 뜨고 툴팁에 `az login`·`gh auth login` 이 보인다. 터미널에서 로그인한 뒤 「권한 다시 확인」 버튼을 누르면 로그인 상태와 앱별 swap 권한을 바로 다시 조회한다(`POST /api/recheck`, swap 과 같은 토큰·주소 확인, 겹쳐 누르면 409).
- 다른 사이트·다른 PC 에서 swap 요청을 못 보내게 막는다: 127.0.0.1 바인딩 + 접속 주소 loopback 확인 + Host·Origin 확인 + 시작할 때 만든 토큰(화면에만 심고 `X-Board-Token` 헤더로 받음).
- 시험은 실제 az 대신 가짜 명령으로 한다: `DEPLOY_BOARD_SWAP_CMD='python -c "import time;time.sleep(3)"' python server.py --allow-swap --bind 127.0.0.1`. 헤더에 「swap 켜짐 (가짜 명령)」이 보인다. 가짜 명령 기록은 `data/swap-log.fake.jsonl` 에 따로 남아 실제 기록·알림 문구와 섞이지 않는다.

## 웹 로그인 swap (기본·공유용 — 각자 Microsoft 계정)
- 헤더의 「Microsoft 로그인」 버튼을 누르면 회사 Microsoft(Entra) 로그인 화면으로 넘어갔다가 돌아온다(인가 코드 + PKCE, MSAL.js `web/vendor/msal-browser.min.js` v5.26.0 을 파일로 둠, CDN 안 씀). 돌아오면 헤더에 이름과 「로그아웃」이 보인다.
- 로그인하면 브라우저가 앱별로 그 사람의 권한을 조회하고(위와 같은 permissions API·같은 판정 규칙), 권한이 있는 앱만 swap 버튼이 켜진다. 꺼지는 조건·모달 다이얼로그·앱 이름 입력 확인·30분 되돌리기는 위 절과 같다.
- 실행하면 브라우저가 ARM 에 직접 `POST {site}/slots/staging/slotsswap?api-version=2023-12-01 {"targetSlot":"production","preserveVnet":true}` 를 보내고, 202 의 작업 주소(못 읽으면 사이트의 `slotSwapStatus.timestampUtc` 변화)를 5초마다 15분까지 확인한 뒤 토스트로 결과를 알린다. 보드 화면은 수집 PC 가 swap 을 감지해 다시 수집한 뒤 바뀐다. 사이트 swap 상태를 1분마다 봐서 보통 1~2분 걸리고(활동 기록만 보던 11-05 첫 실제 swap 은 5분), 그동안 카드에 「보드 반영 대기 중」이 보이고 swap 버튼은 꺼진다(서버 swap 도 같음, 30분 지나면 풀림).
- **토큰은 그 브라우저의 sessionStorage 에만 있고 보드 서버로 보내지 않는다.** 서버에는 Azure 로그인·토큰이 없다. 기록은 Azure 활동 기록에 그 사람 이름으로 남는다(`data/swap-log.jsonl` 에는 안 남음). 되돌리기 정보는 그 브라우저 localStorage `webUndo:<계정>` 에 30분.
- 웹 swap 이 끝나면 보드가 그 swap 을 반영할 때까지(레포의 마지막 swap 시각이 이번 swap 뒤가 될 때까지) 같은 레포의 swap 버튼을 막고 되돌리기만 허용한다(낡은 「나갈 커밋」으로 다시 눌러 방금 swap 을 뒤집지 않게). 진행 중 swap 은 localStorage 로 같은 브라우저의 다른 탭과 나누고, 새로고침해도 swap 시각으로 이어서 기다린다. 되돌리기 전에는 그 사이 다른 swap 이 없었는지 사이트의 swap 시각으로 확인한다.
- 기다리는 중에 로그인이 만료돼도 화면을 떠나지 않는다(다시 로그인 화면으로 넘어가면 결과·되돌리기 기록을 잃음). 토큰은 ARM 주소(`https://management.azure.com/`)로만 보낸다.
- 설정: `repos.json` 의 `entra{tenant_id, client_id}`(공개값). 구독 ID·리소스 그룹은 수집 결과 `state.json` 의 `azure` 에서 읽는다. `--allow-swap` 실행에서는 서버 swap 을 쓰므로 웹 로그인이 꺼진다.
- 로그인되는 주소는 Entra 앱에 리디렉션 URI 로 등록된 곳뿐이다: `http://localhost:8765/`, `http://localhost:8790/`, 개발 서버 Tailscale 주소. `127.0.0.1` 로 열면 로그인 불가 안내가 나온다.
- 화면에는 CSP(`script-src 'self'` + 인라인 스크립트 해시, `connect-src` 는 login.microsoftonline.com·management.azure.com 만)와 `Referrer-Policy: no-referrer` 를 건다.

## 배포 알림 문구(복사용 — 보드는 보내지 않음)
- swap 앱이 있는 레포 카드 머리의 「알림 문구」 버튼을 누르면 모달 다이얼로그가 열리고 Slack 에 붙여 넣을 문구 두 개가 나온다. 「복사」로 클립보드에 넣고 Esc 로 닫는다. 세 실행 방법 모두에서 보인다.
  - 예고: 지금 스테이징에만 있는 커밋(swap 하면 운영에 나갈 것)을 이슈 키별로 묶는다. 제목은 PR 제목 → 커밋 첫 줄(Jira 는 읽지 않음).
  - 결과: 이 PC 의 마지막 swap 기록의 스냅숏으로 「반영 완료」를 만들고 추가·수정·변경으로 나눈다(conventional 접두어 `feat`·`fix` 와 「추가」「수정」 같은 낱말로 판정). 실패·되돌리기 기록이면 그 문구가, 공유 서버에서는 「swap 한 PC 에서 확인」 안내가 나온다.
- 사내 글 형식을 따른다: 첫 줄 결론(`*굵게*`), `•` 한 줄 불릿 5개까지(넘으면 「외 N건」), 300자·8줄 안(넘으면 제목을 줄임). 코드 `notice.py`, API `GET /api/notice?repo=<이름>`.

## 공유 서버(개발 서버) 설치·갱신·끄기
개발 서버 `edbdev-ubuntu22-svr`(ssh 별칭 `datacrawler`)에 systemd 사용자 서비스로 띄운다. 8765 는 다른 프로그램이 쓰고 있어 8790 을 쓴다.
```
# 설치(한 번)
ssh datacrawler 'git clone https://github.com/jw-teoul/deploy-board.git ~/deploy-board && mkdir -p ~/.config/systemd/user && cp ~/deploy-board/deploy/deploy-board.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now deploy-board'
# 수집 PC 에서 결과 올리며 띄우기
python server.py --publish datacrawler:deploy-board/data/state.json
# 코드 갱신
ssh datacrawler 'cd ~/deploy-board && git pull --ff-only && systemctl --user restart deploy-board'
# 상태·끄기
ssh datacrawler 'systemctl --user status deploy-board'      # 끄기: systemctl --user disable --now deploy-board
```
보는 주소: `http://192.168.0.61:8790`(사내망). 수집 PC 가 꺼지면 마지막으로 올린 결과가 그대로 보이고 헤더에 「수집 PC 꺼짐?」이 뜬다.
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
| 스테이징 배포 run | `gh run list --workflow <workflow> -b <배포 브랜치>` |
| swap 기록 | `az monitor activity-log list --resource-id <site> --max-events 5000` 의 `slotsswap` Succeeded (리소스 그룹 필터로는 안 나오고, 작업 이름 대소문자가 섞여 있어 소문자로 비교. 기본 `--max-events 50` 이면 배포가 잦은 날 swap 이 잘려 운영 판정이 사라진다) |
| 운영 SHA(추정) | 성공 배포 run(staging 슬롯에 들어감)과 swap(운영·스테이징 맞바꿈)을 시각순으로 따라간 결과 — 배포 없이 swap 을 다시 하는 되돌리기도 맞는다. 추적이 조회 범위(run 30건·swap 조회 기간) 밖을 가리키면 「마지막 swap 이전 마지막 성공 run」 으로 단순 추정하고 근거에 표시한다. 포털 수동 배포는 모르므로 화면에 「추정」과 근거를 같이 보여 준다 |
| 스테이징에만 있는 커밋 | `git log --cherry-pick <운영SHA>...origin/deploy/prod` |

이슈 키별 단계(높은 것이 이김): 브랜치만 → PR 열림 → develop 머지(develop 대상 PR 머지 또는 develop 에만 있는 커밋) → 스테이징(deploy/prod 대상 PR 머지 또는 스테이징에만 있는 커밋) → 운영(deploy/prod 머지 커밋이 운영 SHA 의 조상). 운영인데 같은 키의 커밋이 스테이징에 또 있으면 스테이징으로 내리고 꼬리표를 단다.

## 변경 감시(거의 실시간)
서버가 켜져 있으면 기본으로 돈다(`--no-watch` 로 끔).
- 30초마다(`--check`) 레포별로 가벼운 조회 2건 — `git ls-remote --heads origin`(브랜치 끝 커밋 전체 1회)과 가장 최근 갱신된 PR 시각(API 1회). 지난번과 다르면 그 레포만 다시 수집해 `state.json` 에 합친다(`merge_state`).
- 1분마다(`--swap-check`) 리소스 그룹 사이트의 마지막 swap 시각(slotSwapStatus, az 1회 약 5~9초)을 보고 바뀌면 그 레포를 다시 수집한다. 이 값은 활동 기록(3~5분 늦음)보다 빨라서, 활동 기록에 아직 없는 swap 은 「잠정」으로 운영 SHA 를 추정하고 카드에 표시한다. 활동 기록에 확정이 올라오면 다시 수집해 잠정을 푼다(감시가 앱 활동 기록을 15분 동안은 매 주기, 그 뒤 6시간까지는 10분마다 확인). 활동 기록의 같은 swap 이벤트(사람 호출·처리)는 correlationId 로 묶는다. 감지한 변경의 다시 수집이 실패하면 그 레포를 5분 뒤 다시 수집한다(이어 실패하면 간격을 두 배씩, 최대 1시간).
- 화면은 10초마다 `/api/status` 를 물어 `collected_at` 이 바뀌면 다시 그린다 → 단계가 바뀐 캐릭터가 걸어간다.
- 비용(실측, 레포 3개): tick 이 30초 대기 + 약 13초 조회라 실제 간격은 약 43초. 변화가 없을 때 시간당 GitHub REST 약 250건 + ls-remote 약 250회, Azure 약 90건. 한도(GitHub 5,000/h, Azure 12,000/h)의 5% 아래.

웹훅(GitHub push/PR, Azure Event Grid 의 swap 이벤트)을 쓰면 폴링 없이 즉시 반영되지만, GitHub·Azure 가 호출할 수 있는 **외부 접근 주소가 필요해 지금은 안 된다**(로컬 PC·사내 개발 서버 모두 밖에서 못 들어온다). 나중에 중계(터널·공개 엔드포인트)가 생기면 그 훅이 `POST /api/refresh` 에 `{"repos":["<레포>"]}` 를 보내면 된다 — 엔드포인트는 이미 있다.

## 설정 `repos.json`
`repos[]` 한 항목이 레포 하나. 공통: `name`(GitHub 레포)·`workflow`(배포 워크플로 파일)·`label`·선택 `local`(클론 경로, 기본 `../projects/<name>` — 없으면 `gh repo clone` 으로 받고, 단일 브랜치 클론이면 fetch refspec 을 전체 브랜치로 넓힌다). `issue_key_pattern` 으로 이슈 키 접두어를 제한한다(제목 속 `AC-35` 같은 오인 방지).

배포 방식에 따라 두 모드 중 하나:
| 모드 | 키 | 스테이징 | 운영 | 예 |
|---|---|---|---|---|
| swap | `app`(Azure App Service 이름) | `deploy/prod` HEAD(= staging 슬롯) | 배포·swap 순서 추적으로 구한 SHA(추정) | adminsite·pilldocsite·agentapiv2·adpssite |
| 브랜치 | `staging_branch`·`prod_branch`·선택 `hosts{staging,prod}` | 스테이징 브랜치 HEAD | 운영 브랜치의 마지막 성공 배포 run 의 SHA(머지가 곧 배포, swap 없음) | pillcheckver2frontend(Vercel: `deploy/dev`→staging.pillchk.com, `deploy/prod`→www.pillcheck.co.kr) |

브랜치 모드에서는 변경 감시가 운영 브랜치의 마지막 run 상태도 함께 본다(배포 run 이 끝나야 운영이 바뀌므로). 화면의 레포 순서는 제목 옆 ▲▼ 로 바꾸며 브라우저(localStorage)에 기억된다 — `repos.json` 순서는 기본값일 뿐이다.

## 픽셀 사무실 (`web/pixel.js`)
레포마다 캔버스 하나. 방 5개 = 단계(대기실·리뷰실·개발실·스테이징·운영 서버실), 캐릭터 하나 = 이슈 하나(머리 위 번호). 캐릭터를 누르면 상세 패널. 새로고침으로 단계가 바뀐 이슈는 복도를 따라 다음 방으로 걸어가고, 새 이슈는 왼쪽 입구로 들어오며, 목록에서 빠진 이슈는 오른쪽으로 나간다(지난 단계는 브라우저 localStorage 에 기억). 방 자리는 18명, 넘치면 방 이름에 「+N 자리 없음」. 헤더 「보기」로 픽셀 사무실 / 표(5열 보드) / 둘 다를 고른다(브라우저에 기억, 기본 픽셀). 표 모드에서는 캔버스를 만들지 않아 애니메이션 비용이 없다.
정적 파일 캐시: 서버가 `index.html` 을 내보낼 때 `pixel.js` 주소에 내용 해시(`?v=`)를, 그림 묶음 해시를 `data-assets` 로 붙인다. 버전이 붙은 파일은 1년 캐시, `index.html`·버전 없는 요청은 `no-cache` 라 코드를 고친 뒤 새로고침 한 번이면 새 파일을 받는다(브라우저에 저장한 보기·순서·접힘은 그대로).
외부 라이브러리·빌드 없음(Canvas 2D). 월드는 16px 타일로 그려 화면 폭에 맞춰 정수가 아니어도 확대(`image-rendering: pixelated`). 에셋 출처는 `web/assets/CREDITS.md`.
