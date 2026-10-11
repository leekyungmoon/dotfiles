# tmux 작업환경 자동 저장·복구

## 목적

PC 재부팅이나 예기치 않은 종료 이후에도 터미널 작업환경을 빠르게 다시
사용할 수 있도록, 작업 구성을 주기적으로 저장하고 데스크톱 로그인 시 자동
복구한다.

아래 "적용 구성"부터 "확인한 내용"까지는 Ubuntu 기준이다. macOS에서는 systemd
사용자 서비스 대신 LaunchAgent 두 개가 같은 일을 한다(맨 아래 "macOS").

## 적용 구성

- **tmux-resurrect:** 세션·창·분할 화면 구성, 작업 디렉터리와 터미널 출력
  기록(scrollback)을 저장·복구한다.
- **tmux-continuum:** tmux 서버가 시작될 때 마지막 저장본을 자동 복구한다.
- **1분 주기 사용자 타이머** (`tmux-resurrect-autosave.timer`): 터미널을 닫아
  tmux에 접속한 화면이 없어도 1분마다 저장한다.
- **systemd 사용자 서비스** (`tmux.service`): Ubuntu 데스크톱 로그인 시 tmux를
  백그라운드로 시작한다.
- 로그인할 때 tmux가 데스크톱 화면·클립보드 정보를 받은 뒤 시작한다.
  로그인마다 그 정보를 다시 맞추므로, 복구된 창의 Codex·Claude에서도
  이미지 붙여넣기(`Ctrl+V`)가 동작한다(아래 "화면·클립보드 환경 변수").
- 모든 저장은 `tmux/resurrect-save` 하나를 거친다. lock으로 중복 저장을 막고,
  저장 hook이 실패하면 이전 저장본을 그대로 둔다. 새로 시작한 tmux 서버가
  마지막 저장본을 아직 복구하지 않았으면 저장하지 않는다(아래 "복구 전 저장
  보류").
- Codex·Claude는 실행 중인 프로세스에서 확인한 대화 ID만 저장하여 해당 대화를
  다시 연다(`tmux/resurrect-ai-session.py`, upstream 공식 post-save hook).
  확인되지 않은 ID는 추측하지 않고, 그 pane은 빈 shell로 복구한다.

파일 구성:

| 파일 | 역할 |
| --- | --- |
| `tmux/resurrect.conf` | plugin 선언과 옵션, 복구 전후 hook, prefix + C-s 저장 키, `default-command` |
| `tmux/resurrect-save` | upstream `save.sh`를 lock·hook 검증·복구 전 저장 보류와 함께 실행, 복구 전후 hook(`--pre-restore`, `--post-restore`) |
| `tmux/resurrect-ai-session.py` | 저장본의 AI pane 명령을 확인된 resume 명령으로 교체 |
| `systemd/user/tmux.service` | 로그인 시 `tmux/tmux-service-start`로 tmux 백그라운드 시작, 종료 시 저장 후 서버 종료 |
| `tmux/tmux-service-start` | 데스크톱 변수와 X 쿠키를 받은 뒤 tmux 시작(`--sync`: 로그인마다 갱신) |
| `systemd/user/tmux-gui-env.service` | 로그인마다 살아 있는 tmux의 화면·클립보드 변수 갱신 |
| `systemd/user/tmux-resurrect-autosave.{service,timer}` | 1분 주기 저장 |
| `manifests/tmux-plugins.json` | TPM과 plugin의 고정 commit |

표의 경로는 저장소 checkout인 `~/.dotfiles` 기준이다. unit 파일은
`~/.config/systemd/user/`에 복사된다. 이미 있는
`~/.config/systemd/user/tmux.service.d/` drop-in은 건드리지 않는다.

plugin은 `${XDG_DATA_HOME:-$HOME/.local/share}/tmux/plugins`
(`TMUX_PLUGIN_MANAGER_PATH`)에, 저장본은
`${XDG_DATA_HOME:-$HOME/.local/share}/tmux/resurrect`에 둔다. 설치기는 plugin을
manifest의 commit 그대로 받는다.

### 화면·클립보드 환경 변수

- 문제: 로그인 직후 tmux가 `DISPLAY`만 가진 채 시작되면, GNOME이 조금 뒤에
  내보내는 `WAYLAND_DISPLAY`·`XAUTHORITY`가 없어서 복구된 모든 pane이
  Xwayland 인증 쿠키 없이 뜬다. GNOME에서 X11로 클립보드를 읽는 Codex의
  이미지 붙여넣기가 `X11 server connection timed out`으로 실패한다. Xwayland
  인증 파일은 로그인마다 이름이 바뀌는데 tmux는 로그아웃 뒤에도 살아 있을 수
  있어, 예전 경로를 받은 pane도 같은 문제가 생긴다.
- `tmux/tmux-service-start`(`tmux.service`의 ExecStart): 최대 20초 동안 세션
  변수를 기다린 뒤 그 값으로 tmux를 시작한다. 현재 쿠키만 이 display 번호에
  묶어 전용 파일 `~/.local/state/tmux/Xauthority`를 원자적으로 다시 만들고,
  pane에는 이 고정 경로를 `XAUTHORITY`로 준다. `XAUTHORITY` 없이 시작된
  프로세스를 위해 같은 항목을 `~/.Xauthority`에도 병합한다. 기다리는 동안
  사용자가 tmux를 먼저 띄웠으면 그 서버만 갱신하고 75로 실패 종료한다.
  그래야 systemd가 ExecStop(`kill-server`)으로 사용자 서버를 끄지 않는다.
- `tmux-gui-env.service`: 로그인마다 `tmux-service-start --sync`를 실행해 쿠키
  파일을 새로 만들고, 살아 있는 tmux의 전역 변수와 세션별
  `XAUTHORITY`·`WAYLAND_DISPLAY`·`DISPLAY` 덮어쓰기를 고친다.
- `tmux.conf`의 `update-environment`에는 `XAUTHORITY`가 없다(사라질 경로가
  세션에 고정되지 않도록). `bin/tmux-smart-paste`는 tmux 전역 환경의
  `XAUTHORITY`를 가져온다.
- 보안: `~/.local/state/tmux/Xauthority`와 `~/.Xauthority`에는 현재 X 쿠키가
  들어 있다(0600). 저장소에 넣지 않으며 설치기의 관리·백업 대상도 아니다.
  pane에서 `ssh -X`/`-Y`를 쓰면 실제 쿠키가 전달된다는 점에 주의한다.
- 확인: `systemctl --user show tmux-gui-env.service -p Result --no-pager`,
  `tmux show-environment -g | grep -E 'WAYLAND_DISPLAY|XAUTHORITY'`.
  실제 판정은 Codex pane에서 이미지 `Ctrl+V`.
- 회귀시험: `tests/integration/test_tmux_service_start.py`.

### 설계상 선택

- **continuum 자체 자동 저장은 끈다** (`@continuum-save-interval 0`).
  tmux-resurrect는 plugin을 불러올 때 `@resurrect-save-script-path`를 자기
  `save.sh`로 다시 설정하므로, continuum 저장은 lock과 hook 검증을 우회한다.
  대신 사용자 타이머가 같은 wrapper로 1분마다 저장한다.
- **continuum은 `tmux.service`를 쓰거나 켜고 끄지 않는다.** continuum은
  `@continuum-boot`가 on이면 unit 파일이 없을 때 자체 unit을 쓰고
  `systemctl --user enable`을, off이면 plugin을 불러올 때마다
  `systemctl --user disable`을 실행한다. `resurrect.conf`는 설치된 unit이 있고
  이미 enable된 경우에만 on으로, 그 밖에는 off로 두어 두 동작이 모두 아무것도
  바꾸지 않게 한다. unit은 저장소의 정적 파일을 설치기가 관리한다.
- **그래픽 환경 변수:** unit에 `DISPLAY`를 고정하지 않는다. GNOME은 shell이
  뜬 뒤 `WAYLAND_DISPLAY`, `DISPLAY` 등을 systemd 사용자 관리자에 넣고, 그
  다음에 `graphical-session.target`에 도달한다. `tmux.service`는
  `After=graphical-session.target`으로 그 이후에 시작하므로 tmux 서버와
  로그인 때 복구되는 pane이 이 값을 물려받는다. `graphical-session-pre.target`
  뒤에만 두면 GNOME shell보다 먼저 시작해 복구된 pane에 이 값이 없을 수 있다.
  나중에 터미널에서 접속하면 tmux의 `update-environment`가 그 뒤에 만드는
  pane에 쓸 값을 갱신한다. 실제 22.04/24.04 Wayland 로그인에서 복구된 pane의
  `WAYLAND_DISPLAY`는 아직 확인하지 않았다(아래 상태 확인 참고).
- **서비스 PATH:** 두 unit의 `PATH`는 설치기 도구 디렉터리
  `~/.local/share/personal-dotfiles/bin`(fd shim, `fzf-preview.sh`)을 zshenv와
  같이 맨 앞에 둔다. tmux의 `run-shell`·popup 명령(prefix + @ 등)은 서버
  환경으로 실행되기 때문이다. `XDG_DATA_HOME`을 바꿔 쓰면 unit의 경로도 같이
  바꿔야 한다.
- **`default-command`는 단순 명령 하나다.** pane 내용을 저장하므로 resurrect는
  pane을 `cat <저장 내용>; exec <default-command>`로 다시 연다. `if ...; fi`
  같은 복합 명령은 `exec if ...`가 되어 복구된 pane이 모두 바로 종료된다.
  그래서 설정을 불러올 때 zsh가 있으면 `zsh -il`, 없으면 비워 두어 tmux가
  기본 shell을 login shell로 띄운다. zsh를 나중에 설치했다면 설정을 다시
  불러온다(prefix + r).
- tmux 3.4(Ubuntu 24.04)는 `show-option -v` 출력에서 `$`를 `\$`로 바꾸므로,
  plugin 옵션 값에는 `$`를 넣지 않고 설정을 불러올 때 절대경로로 계산한다.

## 사용자 입장에서의 동작

1. 평소에는 별도 조작 없이 1분마다 현재 작업환경이 저장된다.
2. PC를 재부팅한 뒤 Ubuntu에 로그인한다.
3. tmux가 백그라운드에서 시작되어 마지막 저장본을 자동 복구한다. 로그인용
   임시 세션(`__continuum_startup`)은 다른 세션이 복구된 경우에만 정리된다.
   저장본 자체에 `__continuum_startup` 세션의 창이 있으면(처음 로그인 때 그
   세션에서 작업한 경우) 그 창들은 그대로 복구되고 지워지지 않는다. 이 정리는
   서버 시작 직후의 자동 복구에만 적용되고, 나중에 누른 prefix + C-r은
   `__continuum_startup`을 건드리지 않는다.
4. 복구 명령이나 버튼을 누를 필요는 없다. 화면을 볼 때만 터미널에서 tmux에
   접속한다. 터미널 창을 강제로 열지는 않는다.

이미 tmux 서버에 세션이 있으면 자동 시작을 건너뛰어 기존 작업을 유지한다.
로그아웃이나 그래픽 세션 재시작만으로는 서비스가 멈추지 않는다
(`graphical-session.target`에 묶여 있지 않다). 다만 계정에 linger가 꺼져 있으면
마지막 세션이 끝날 때 systemd 사용자 관리자 자체가 종료된다. 이때 서비스는
먼저 저장한 뒤 tmux를 종료하고, 다음 로그인에서 복구한다.

### 복구 전 저장 보류

새로 시작한 tmux 서버는 복구 전까지 로그인용 임시 세션만 가지고 있다. 이때
저장하면 `last`가 거의 빈 저장본을 가리키게 되므로, `resurrect-save`는 이
서버가 마지막 저장본을 복구하기 전에는 저장하지 않고 이전 저장본을 그대로
둔다(종료 코드 75, 1분 타이머에서는 정상 종료로 처리). 자동 복구를 건너뛴
경우(`~/tmux_no_auto_restore`, 다른 tmux 서버 실행 중, 복구 실패)에도 같다.
`tmux.service` 종료 때의 저장도 같은 규칙을 따른다.

- 복구가 끝나면(자동 복구 또는 prefix + C-r) 서버에
  `@tmux-restore-complete on`이 설정되고 그때부터 저장이 다시 진행된다.
- 복구하지 않고 지금 상태를 새 기준으로 저장하려면 다음을 실행한 뒤
  저장한다.

```sh
# 이전 저장본 대신 현재 tmux 상태를 저장 기준으로 삼는다
tmux set-option -g @tmux-restore-complete on
```

- 이 규칙이 생기기 전부터 떠 있던 서버는 시작 후 스스로 저장한 기록
  (`@continuum-save-last-timestamp`)이 있으므로 계속 저장된다.
- 저장이 보류되면 `journalctl --user -u tmux-resurrect-autosave.service`에
  이유가 남는다.

## 복구 범위와 제한

- 저장하는 것은 **작업환경과 재개 정보**다. 실행 중 프로세스의 메모리 상태나
  완료되지 않은 계산을 그대로 보존하지 않는다.
- 일반 명령이나 SSH 연결은 자동으로 다시 실행하지 않는다. 다시 실행하는 것은
  resurrect 기본 목록(vi, less 등)과 hook이 확인한 `codex resume <ID>`,
  `<claude 경로> --resume <ID>`뿐이다. Claude 경로는 저장 시점 PATH에서 찾고,
  없으면 `~/.local/bin/claude`를 쓴다. 공백이 든 경로는 다시 실행하지 않는다.
- resurrect는 저장된 명령을 pane의 shell에 입력해 다시 실행한다. 그래서 hook은
  AI가 아닌 pane의 명령을 실행 중인 프로세스의 인자에서 shell 인용으로 다시
  만든다. `a;cmd` 같은 파일 이름은 인자 하나로 전달되고 명령으로 실행되지
  않는다. 저장된 문자열과 실제 인자가 정확히 맞지 않거나 제어 문자가 있으면
  그 명령은 비운다(빈 shell로 복구).
- Codex는 실행 중인 프로세스가 연 thread lock·rollout에서 확인한 대화를
  우선한다. TUI 안에서 /new, /resume으로 대화를 바꾸면 명령줄의
  `codex resume <ID>`는 예전 ID로 남기 때문이다. 명령줄 ID는 실행 중 증거가
  없거나 같은 ID일 때만 쓰고, 서로 다르면 확인된 대화를 쓰며 확인할 수 없으면
  빈 shell로 복구한다.
- 예기치 않은 종료 시 마지막 저장 이후 약 1분의 변경은 누락될 수 있다. 저장
  실패나 디스크 문제까지 포함한 절대적인 1분 보장은 아니다.
- 이전 기록에 없는 세션·대화는 복원할 수 없다.
- OS 로그인 자체를 자동화하지 않는다. 기준은 데스크톱 로그인이다.
- 저장본에는 터미널 출력 기록이 들어 있으므로 이 PC에만 둔다. 디렉터리는
  0700, 파일은 0600으로 만들며 저장소나 외부로 올리지 않는다.
- 수동 복구(prefix + C-r)는 upstream 동작상 이미 있는 창의 이름·배치·포커스를
  저장본대로 바꿀 수 있다. 이미 열린 pane의 프로세스는 덮어쓰지 않는다.

## 사용법

- 수동 저장: prefix(`C-a`) 다음 `C-s`. 수동 복구: prefix 다음 `C-r`.
- 저장본 위치: `${XDG_DATA_HOME:-$HOME/.local/share}/tmux/resurrect/last`
  (가장 최근 저장본을 가리키는 링크).
- 상태 확인:

```sh
systemctl --user status tmux.service tmux-resurrect-autosave.timer
```

- 1분 자동 저장 끄기:

```sh
systemctl --user disable --now tmux-resurrect-autosave.timer
```

- 로그인 시 자동 시작 끄기(continuum이 다시 켜지 않는다):

```sh
systemctl --user disable tmux.service
```

- tmux를 시작할 때의 자동 복구만 끄기(upstream continuum 기능). 이때도
  복구 전에는 자동 저장이 보류된다("복구 전 저장 보류" 참고):

```sh
touch ~/tmux_no_auto_restore
```

- 로그인 후 복구된 pane에 그래픽 환경 변수가 있는지 확인(tmux 안에서):

```sh
tmux show-environment -g WAYLAND_DISPLAY DISPLAY
```

위의 `systemctl --user disable`로 끈 것은 다음 설치, `dotfiles update`,
`dotfiles repair`까지만 유지된다. 이 명령이 지우는 `.wants` 링크는 설치기가
관리하는 항목이라 다음 실행이 다시 만들고, 1분 자동 저장 타이머도 바로 다시
시작한다(`tmux.service`는 다음 데스크톱 로그인부터 다시 시작된다). 그때까지는
설치기 상태 확인에서 변경된 항목으로 보인다. Linux에서
`dotfiles services disable`은 이 명령들을 출력하고 이 점을 알릴 뿐 아무것도
바꾸지 않는다.

## 확인한 내용

이 저장소의 시험이 확인하는 내용만 적는다.

- `tests/unit/test_resurrect_ai_session.py`
  - Codex·Claude 명령 인자 해석이 명시적이고 모호하면 실패하는지, Claude
    옵션을 좁게 보존하는지.
  - Claude 실행 경로를 저장 시점 PATH에서 찾고, 없거나 상대경로이면
    `~/.local/bin/claude`로 대체하는지.
  - 여러 AI ID가 보이면 복구하지 않고, Codex는 metadata로 확인된 root 대화
    하나만 고르는지. Codex 명령줄 ID보다 실행 중 thread lock이 우선하고,
    둘이 어긋나면 추측하지 않는지.
  - 일반 명령은 실행 중 인자와 정확히 맞을 때만 shell 인용으로 다시 만들고
    (`a;cmd` 같은 인자가 명령이 되지 않는지), 맞지 않으면 비우는지. 확인되지
    않은 AI 명령은 비우는지. 저장본 쓰기가 원자적이며 0600이고, 성공 표시가
    성공한 경우에만 남는지.
  - 이 기능의 파일(tmux.conf 포함)에 특정 사용자 홈의 절대경로, 고정된 DISPLAY
    값, 특정 PC의 개인 설정 설명이 없는지. unit이 `graphical-session.target`
    뒤에 시작하고 로그아웃에 묶이지 않는지, 두 unit의 PATH에 설치기 도구
    디렉터리가 있는지, 종료 순서와 1분 타이머, 저장 보류(75)가 unit 실패가
    아닌지, tmux.conf와 resurrect.conf가 선언한 plugin이 모두 commit으로
    고정됐는지.
- `tests/integration/test_tmux_resurrect_integration.py` (고정 commit의 upstream
  TPM·plugin을 임시 디렉터리에 받아 별도 socket의 tmux로 실행.
  `TMUX_TEST_BINARY`로 22.04의 tmux 3.2a 같은 다른 tmux를 고를 수 있다)
  - 로그인 재현: 전체 tmux.conf와 TPM을 `tmux.service`와 같은 방식(unit의
    PATH, `new-session -d -s __continuum_startup`)으로 시작하고 저장 → 서버
    종료 → 새 서버 시작 → continuum 자동 복구 후 세션·창 이름·경로와 pane
    내용(저장 전에 출력한 표시 문자열)이 돌아오는지, 로그인 세션에 저장된
    창도 남는지.
  - `~/tmux_no_auto_restore`로 자동 복구를 건너뛴 서버에서 타이머·종료 저장이
    `last`를 바꾸지 않고, 그 뒤 prefix + C-r로 전체 작업환경이 복구되는지.
    복구 전 저장 보류, 명시적 해제, 규칙 이전부터 떠 있던 서버의 저장 유지.
  - unit PATH로 시작한 서버의 `run-shell`에서 도구 디렉터리의 `fd`가 보이는지,
    작은따옴표가 든 디렉터리에서 prefix + @ 명령이 디렉터리 이름을 실행하지
    않는지(tmux 3.3 이상).
  - `tail -f 'a;touch PWNED;#'`를 저장·복구해도 명령이 인자로만 다시 실행되는지.
  - `default-command`가 resurrect의 `exec` 접두어와 함께 문법 오류가 없는지.
  - 설정이 저장 디렉터리·hook 경로를 절대경로로 풀고, plugin을 불러온 뒤에도
    C-s가 wrapper를 가리키는지.
  - continuum 부팅 처리가 unit 파일을 쓰지 않고 enable도 하지 않으며, 사용자가
    끈 unit을 다시 켜지 않는지(가짜 `systemctl`로 호출 기록).
  - 접속한 화면 없이 저장 → 서버 종료 → 복구 후 세션·창 이름·경로·pane
    좌표와 크기가 같은지, 저장 디렉터리 0700과 저장본 0600인지, 확인된 Codex·
    Claude 대화만 다시 실행되고 ID를 모르는 Claude pane은 빈 shell로 남는지,
    다시 복구해도 기존 pane 프로세스가 유지되는지.
  - 저장 hook이 실패하면 이전 저장본이 유지되는지.
  - 로그인용 임시 세션이 복구 전에는 남고, 복구 후에만 정리되는지. 나중의
    수동 복구는 그 세션을 지우지 않는지.

실제 PC 재부팅, 데스크톱 로그인 시 서비스 시작 순서와 그래픽 환경 변수,
systemd 타이머의 실제 동작은 이 시험들이 확인하지 않는다. 각 PC에서 위 상태
확인 명령으로 확인한다.

## macOS

macOS에서는 launchd의 LaunchAgent(사용자 에이전트) 두 개가 Ubuntu의 systemd
사용자 서비스를 대신한다. 저장과 복구 자체(tmux-resurrect, tmux-continuum,
`tmux/resurrect-save`, AI hook, 복구 전 저장 보류)는 위와 같다. 이 절의 내용은
아직 실제 Mac에서 확인하지 않았다(`docs/SUPPORT.md`의 macOS 항목은 모두
pending verification이다).

### 적용 구성 (macOS)

| LaunchAgent | 실행하는 것 | 실행 시점 |
| --- | --- | --- |
| `com.github.leekyungmoon.dotfiles.tmux` | `tmux/tmux-launchd-start` | 데스크톱 로그인 때, 그리고 설치·repair·`dotfiles services enable`이 불러올 때 한 번(RunAtLoad) |
| `com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave` | `tmux/resurrect-autosave` | 60초마다(StartInterval), 접속한 화면이 없어도 |

- plist는 `~/Library/LaunchAgents/<label>.plist`(0644)다. launchd는 `~`와
  `$HOME`을 풀지 않으므로 설치기가 사용자마다 절대경로로 만들고, 다른 관리
  파일처럼 백업·변경 확인·`restore` 대상으로 다룬다.
- 두 agent 모두 데스크톱(Aqua) 로그인 세션에서만 불러온다. SSH로만 접속한
  상태나 FileVault 잠금을 풀기 전에는 실행되지 않는다.
- 환경: `PATH`는 설치기 도구 디렉터리(`~/.local/share/personal-dotfiles/bin`),
  `~/.local/bin`, Homebrew의 `bin`·`sbin`, 시스템 디렉터리 순이다. launchd의
  기본 PATH에는 Homebrew가 없어서 tmux와 hook의 python3를 찾지 못하기
  때문이다. `LANG`은 설치한 셸의 UTF-8 locale(`locale -a`에 있을 때, 없으면
  `en_US.UTF-8`)이다. UTF-8이 아니면 macOS `ps`가 한글 같은 인자를 다른 문자로
  바꿔 보여 주므로 hook이 pane 명령을 확인하지 못하고 비운다. `TMUX`,
  `TMUX_TMPDIR`는 넣지 않는다(터미널에서 실행한 tmux가 찾는 기본 서버여야
  한다).
- tmux agent는 한 번 실행하고 끝나며 다시 띄우지 않는다(KeepAlive 없음:
  사용자가 일부러 끈 서버를 되살리지 않는다). agent가 끝나도 tmux 서버는
  남고(AbandonProcessGroup), pane이 느려지지 않게 대화형 우선순위로
  실행한다(ProcessType Interactive).
- autosave agent는 낮은 우선순위(Background, 낮은 I/O 우선순위, nice 10),
  umask 077로 실행한다.
- `@continuum-boot`는 macOS에서 항상 off다. continuum의 on은 로그인할 때 터미널
  창을 열어 tmux를 입력하는 자체 LaunchAgent(`~/Library/LaunchAgents/Tmux.Start.plist`)를
  쓰기 때문이다. 예전 설정이 남긴 그 파일이 있으면 설치기가 launchd에서 내리고
  `~/.local/state/personal-dotfiles/backups/launchd/<RUN_ID>/`로 옮긴다(지우지
  않는다).

파일 구성:

| 파일 | 역할 |
| --- | --- |
| `tmux/tmux-launchd-start` | tmux agent: 서버가 없을 때만 tmux를 시작하고 복구 결과를 기록 |
| `tmux/resurrect-autosave` | autosave agent: 서버가 있으면 `tmux/resurrect-save`로 저장, 50초 제한, 복구 전 저장 보류(75)는 성공으로 처리 |
| `installer/launchd.py` | plist 생성, `launchctl`로 등록·확인·해제, `dotfiles services` |
| `installer/darwin_proc.py` | `/proc` 대신 libproc과 sysctl로 pane 프로세스의 인자와 열린 파일 확인 |
| `etc/terminfo/tmux-256color.terminfo` | `tmux-256color` 터미널 정보. macOS 14 이상에는 ncurses 6.0의 더 오래된(2015년) 항목이 있어, 설치기는 시스템에 이 항목이 없을 때만 `~/.terminfo`로 컴파일한다 |

macOS 쪽 차이:

- 저장 lock: `flock`(Linux)이 없으므로 `tmux/resurrect-save`는 `/usr/bin/lockf -s -t 0 9`
  (macOS 15 이상), 그것도 안 되면 python3의 `fcntl.flock`으로 같은
  `operation.lock`을 잡는다. 다른 저장이 진행 중이면 조용히 끝내고(0), 잡을
  도구가 하나도 없으면 저장하지 않고 실패로 알린다. 성공한 척하지 않는다.
- macOS `tar`가 pane 내용 압축 파일에 확장 속성(`._*` 항목)을 넣지 않도록
  `COPYFILE_DISABLE=1`로 저장한다.
- AI hook은 `installer/darwin_proc.py`로 실행 중 프로세스의 인자와 열린 파일을
  읽는다. 이것을 쓸 수 없으면 저장은 그대로 하되(배치, 작업 디렉터리, 출력
  기록) pane 명령은 비우고, 저장할 때마다 로그에 경고를 한 줄 남긴다. 설치와
  repair는 `tmux/resurrect-ai-session.py --self-test`를 agent와 같은 PATH로
  실행해 미리 확인하고, 실패하면 "process inspection unavailable: Codex/Claude
  resume commands will not be saved ..." 이유를 보여 준다(설치는 실패로 보지
  않는다).
- 설치기는 시스템에 `tmux-256color`가 없으면 `/usr/bin/tic -x -o ~/.terminfo
  ~/.dotfiles/etc/terminfo/tmux-256color.terminfo`로 컴파일한다. 실패하면 이
  명령을 이유로 보여 준다. Apple이 공개한 ncurses 소스로는 macOS 14 이상에
  ncurses 6.0의 2015년 항목이 있으므로(`/usr/bin/infocmp -x tmux-256color`가
  찾는다) 이때는 아무것도 컴파일하지 않는다. 그 항목에는 이 저장소의 항목이
  더한 취소선(`smxx`, `rmxx`), 밑줄 모양(`Smulx`), 밑줄 색(`Setulc`)이 없다.
  저장소의 항목을 쓰려면 직접 설치한다:
  `~/.dotfiles/etc/terminfo.sh tmux-256color`.

### 사용자 입장에서의 동작 (macOS)

1. 평소에는 별도 조작 없이 1분마다 현재 작업환경이 저장된다(접속한 화면이
   없어도).
2. Mac을 재시동한 뒤 데스크톱에 로그인한다.
3. tmux agent가 백그라운드 tmux 서버를 시작하고 continuum이 마지막 저장본을
   복구한다. 터미널을 열고 tmux에 접속하면 된다(`ta`, `tmux attach`).
4. 이미 tmux 서버가 있으면(로그인 직후 터미널에서 먼저 tmux를 실행한 경우 등)
   그 서버를 그대로 두고 끝난다.

`tmux/tmux-launchd-start`의 순서:

- 서버가 있으면 아무것도 하지 않는다("a tmux server is already running; left
  untouched").
- 없으면 `$HOME`에서 `tmux new-session -d -s __continuum_startup`으로 서버를
  시작한다. 설정을 불러오면서 continuum이 복구를 시작한다. tmux는 절대경로로
  실행한다(continuum은 `tmux`로 시작하는 다른 프로세스를 보면 복구를 건너뛴다).
- 복구가 시작되기를 `@continuum-restore-max-delay` + 15초(기본 25초), 끝나기를
  180초까지 기다리며 `@tmux-restore-complete`를 지켜본다.
- continuum은 그 순간 다른 tmux 클라이언트를 보면(다른 plugin의 백그라운드
  작업, 로그인 때 `tmux attach`를 실행한 터미널) 복구를 건너뛴다. 그때 저장본이
  있고 `~/tmux_no_auto_restore`가 없으며 로그인용 임시 세션이 손대지 않은
  상태(`__continuum_startup` 창 1개, pane 1개, 접속 없음, 다른 세션 없음)면,
  스크립트가 continuum과 같은 복구 스크립트를 직접 실행한다. 이미 사용 중인
  서버에는 자동으로 복구하지 않는다.
- 결과는 서버 옵션 `@tmux-restore-status`와 agent 로그에 남긴다.

| `@tmux-restore-status` | 뜻 |
| --- | --- |
| `restored <날짜 시각>` | 복구했다 |
| `restored <날짜 시각> (continuum skipped; restored by tmux-launchd-start)` | continuum이 건너뛰어 스크립트가 직접 복구했다 |
| `no snapshot` | 아직 저장본이 없다 |
| `not restored: ~/tmux_no_auto_restore exists` | 자동 복구를 꺼 두었다 |
| `not restored: tmux-resurrect is not loaded` | plugin을 불러오지 못했다(`dotfiles repair`) |
| `not restored: @continuum-restore is off` | 설정에서 자동 복구가 꺼져 있다 |
| `not restored: continuum did not start it within 25s` | continuum이 시작하지 않았고 스크립트도 복구하지 않았다(이유는 로그) |
| `not restored: still restoring after 180s` | 복구가 180초 안에 끝나지 않았다 |

복구하지 않은 서버도 "복구 전 저장 보류"는 같다. 로그에 이유와 함께 prefix +
C-r, `~/tmux_no_auto_restore` 삭제, `tmux set-option -g @tmux-restore-complete on`
중 할 일을 적는다.

종료 코드(launchd의 `last exit code`):

| agent | 코드 | 뜻 |
| --- | --- | --- |
| tmux | 0 | 복구했다, 복구할 것이 없다(저장본 없음, `~/tmux_no_auto_restore`), 또는 서버가 이미 있었다 |
| tmux | 3 | 저장본이 있지만 복구하지 않았다. 서버는 실행 중이다. 설치기는 실패가 아니라 경고("press prefix + C-r in tmux to restore it")로 알린다 |
| tmux | 1 | tmux가 없다(`brew install tmux && dotfiles repair`), HOME이 잘못되었다, 또는 `tmux new-session`이 실패했다(tmux는 모든 오류에 1로 끝난다). 어느 쪽인지는 로그에 있다 |
| tmux | 126, 127 | tmux를 찾았지만 실행하지 못했다(다른 아키텍처용 바이너리 등). 이유는 로그에 있다. `brew reinstall tmux && dotfiles repair` |
| tmux | 128 초과 | `tmux new-session`이 신호로 종료되었다(128 + 신호 번호) |
| tmux | 78 | launchd가 스크립트를 시작하지 못했다(EX_CONFIG: 로그 디렉터리나 스크립트가 없을 때 등). 로그가 없을 수 있다. `dotfiles repair` |
| autosave | 0 | 저장했다, 서버가 없다, 또는 복구 전 저장 보류(75를 0으로 바꾸고 서버마다 한 번만 로그) |
| autosave | 그 밖 | 저장 실패. `tmux/resurrect-save`의 메시지가 로그에 남는다 |

autosave는 50초가 넘도록 끝나지 않는 저장(응답하지 않는 서버 등)을 멈춘다(TERM,
5초 뒤 KILL). launchd는 작업이 실행 중이면 다음 주기를 건너뛰기 때문이다. 멈춘
저장은 이전 저장본을 그대로 둔다.

로그: `${XDG_STATE_HOME:-~/.local/state}/personal-dotfiles/launchd/<label>.log`
(디렉터리 0700). launchd는 로그를 정리하지 않으므로 두 스크립트가 시작할 때
1 MiB를 넘으면 `<label>.log.1`로 옮긴다(이전 `.1`은 덮어써서 한 세대만
남는다). 평소의 저장은 아무것도 쓰지 않는다.

설치·repair·update 때:

- plist가 바뀌었으면 agent를 내렸다가(bootout) 다시 불러오고(bootstrap), 그대로면
  launchd에 아무것도 하지 않는다. tmux agent는 맨 마지막에 불러온다. 불러오면
  바로 실행되지만 서버가 있으면 그대로 둔다.
- 사용자가 `dotfiles services disable`로 끈 agent는 켜지 않는다. 단계 결과는
  PASS로 두고 `tmux: disabled (enable: dotfiles services enable tmux)` 같은
  이유만 보여 준다.
- 결과는 `launchctl print`로 확인한 것만 PASS다. SSH로만 접속해 GUI 세션이
  없으면 RELOGIN_REQUIRED(`launchd-gui-domain-unavailable`)다. plist는 이미
  있으므로 다음 데스크톱 로그인 때 불러온다. 지금 불러오려면 데스크톱 세션의
  터미널에서, 설치기가 보여 준 `launchctl bootstrap gui/$(id -u) ...` 명령을
  실행한다.

### 로그인 항목: 백그라운드에서 허용

agent를 처음 불러오면 macOS가 백그라운드 항목이 추가되었다는 알림(Background
Items Added)을 보이고, 시스템 설정 > 일반 > 로그인 항목 및 확장 프로그램(Login
Items & Extensions)의 "백그라운드에서 허용"(Allow in the Background) 목록에
올린다(이름은 `sh`나 스크립트 이름으로 보일 수 있다). 여기서 끄면 launchd가
불러오기를 거부하고, 설치·repair는 `launchctl bootstrap failed (5: Input/output
error)`와 이 설정을 확인하라는 안내로 실패한다. 다시 켠 뒤 터미널(SSH가 아닌
데스크톱 세션)에서 `dotfiles repair`를 실행한다. 일부러 끄려면 이 토글보다
`dotfiles services disable`을 쓴다(설치기가 상태를 알고 다시 켜지 않는다).

### 로컬 네트워크와 파일 및 폴더 접근

macOS 15부터 로컬 네트워크(같은 네트워크의 기기, `.local` 이름) 접근은 사용자의
허락이 필요하다. Apple TN3179에 따르면 터미널이나 SSH에서 실행한 명령과 그
자식 프로세스, launchd 데몬, root 프로세스는 자동으로 허용되지만 launchd
에이전트는 그렇지 않다. 그래서 tmux agent가 시작한 tmux 서버의 pane에서
실행한 프로그램은 터미널 앱의 허락을 받지 못한다.

- 증상: pane에서 같은 네트워크의 기기로 `ssh`, `curl`, `git` 등을 실행하면
  `No route to host`나 연결 시간 초과로 실패하거나, tmux(또는 `sh`)가 로컬
  네트워크의 기기에 접근하려 한다는 알림이 뜬다. 알림에 답하기 전의 첫
  시도는 실패할 수 있다. 바로 끝나는 프로세스에는 알림이 뜨지 않을 수도
  있다(TN3179).
- 허용: 시스템 설정 > 개인정보 보호 및 보안 > 로컬 네트워크(Privacy &
  Security, Local Network)에서 해당 항목을 켠다.
- 파일 및 폴더(TCC)도 비슷하다. pane의 프로그램이 데스크탑, 문서, 다운로드,
  iCloud Drive, 외장·네트워크 볼륨에 접근하면 터미널 앱이 아니라 tmux 쪽 이름으로
  권한을 묻거나 `Operation not permitted`로 실패할 수 있다. 시스템 설정 >
  개인정보 보호 및 보안 > 파일 및 폴더(Files & Folders) 또는 전체 디스크 접근
  권한(Full Disk Access)에서 허용한다.
- 피하는 방법: `dotfiles services disable tmux`로 tmux agent만 끈다. 1분 자동
  저장은 계속된다. 로그인 후 터미널에서 처음 tmux를 실행하면 그 서버가
  continuum으로 마지막 저장본을 복구하고, 그 pane들은 터미널 앱의 권한을 따른다.

이 동작은 아직 실제 Mac에서 확인하지 않았다.

### 복구 범위와 제한 (macOS)

- 복구하는 것: 세션·창·분할 화면 배치, 작업 디렉터리, 터미널 출력
  기록(scrollback), 실행 중 프로세스에서 확인한 Codex·Claude 재개 명령
  (`codex resume <ID>`, `<claude 경로> --resume <ID>`), resurrect 기본 목록의
  프로그램(vi, less 등).
- 복구하지 않는 것: 프로세스의 메모리 상태, 그 밖에 실행 중이던 명령(다시
  실행하지 않고 빈 shell로 복구), 살아 있던 네트워크 연결(SSH 접속 등),
  마지막 저장 이후의 변경.
- 저장은 1분마다다. 예기치 않은 종료 때 최대 약 1분의 변경이 빠질 수 있다.
- 로그아웃·재시동·종료할 때 저장하지 않는다(Ubuntu의 `tmux.service` 종료 때
  저장에 해당하는 것이 없다). 마지막 1분 저장이 기준이다.
- 잠자기 중에는 저장하지 않는다. 그동안의 주기는 건너뛰고(launchd.plist의
  StartInterval), 깨어난 뒤 다음 주기부터 다시 저장한다.
- Codex·Claude 명령은 프로세스 확인(`installer/darwin_proc.py`)이 동작할 때만
  저장한다. 다른 사용자의 프로세스(`sudo` 등)는 확인할 수 없어 비운다.
  Codex 프로세스가 열어 둔 파일을 하나라도 읽지 못하면(접근 거부 등) 지금
  쓰는 대화를 확인할 수 없으므로, 명령줄 ID로 대신하지 않고 그 pane의 명령을
  비운다. Ubuntu는 읽지 못한 파일을 건너뛰고, 실행 중 증거가 없으면 명령줄
  ID를 쓴다.

### 사용법 (macOS)

- 상태 확인(두 agent의 상태, 로그 경로, 마지막 저장 후 경과 시간):

```sh
dotfiles services
```

- 끄기·켜기. 이름을 빼면 둘 다다. 끈 상태는 launchd에 남아 이후의 설치,
  `dotfiles update`, `dotfiles repair`도 켜지 않는다. 끄더라도 실행 중인 tmux
  서버는 끝나지 않는다. agent가 실행 중이면(autosave agent가 저장하는 중 등)
  launchd가 내릴 때까지 최대 10초 기다리고, 그래도 남아 있으면 실행할
  `launchctl bootout` 명령과 함께 실패로 알린다(끈 상태는 그대로 기록된다):

```sh
# 로그인 때 tmux 자동 시작만 끈다(1분 자동 저장은 계속)
dotfiles services disable tmux
# 1분 자동 저장을 끈다
dotfiles services disable autosave
# 둘 다 다시 켠다
dotfiles services enable
```

- launchd의 상태와 로그, 복구 결과:

```sh
launchctl print gui/$(id -u)/com.github.leekyungmoon.dotfiles.tmux
tail -n 50 ~/.local/state/personal-dotfiles/launchd/com.github.leekyungmoon.dotfiles.tmux.log
tmux show-option -gqv @tmux-restore-status
```

- 제거: `dotfiles restore --baseline`은 다른 관리 파일과 함께 두 plist를 지우고
  launchd에서 내린다. agent만 지우려면:

```sh
dotfiles restore --baseline --id launchd-tmux --id launchd-tmux-resurrect-autosave
```

- 수동 저장(prefix + C-s), 수동 복구(prefix + C-r), `~/tmux_no_auto_restore`,
  "복구 전 저장 보류"의 해제는 Ubuntu와 같다.

### 확인한 내용 (macOS)

이 저장소의 시험이 확인하는 내용만 적는다. `tests/macos/e2e.sh`를 뺀 시험은
Linux에서도 실행된다(launchd와 macOS 도구는 가짜나 시뮬레이션). 실제 macOS가
필요한 시험은 macOS에서만 실행된다.

- `tests/unit/test_launchd.py` (launchd는 시뮬레이션)
  - plist가 절대경로, 정해진 키, PATH·LANG을 갖고 0644로 써지는지, 같은
    사용자에게 같은 바이트인지.
  - GUI 세션이 없으면 RELOGIN_REQUIRED, 사용자가 끈 agent는 SKIPPED, bootstrap
    거부 코드는 이유와 실행할 명령이 붙은 FAIL인지. `launchctl print`로 확인하지
    못하면 PASS가 아닌지.
  - 바뀌지 않은 plist의 repair는 bootout·bootstrap을 하지 않는지, 바뀐 plist는
    내렸다 다시 불러오는지, tmux agent의 종료 코드 0·3·1을 구분하는지(1이면
    세 가지 원인을 모두 알리는지).
  - 실행 중인 agent의 bootout이 36(진행 중)으로 답하면, 다시 불러올 때와
    내릴 때(`dotfiles services disable`, `restore`) 모두 agent가 내려갈 때까지
    기다리고, 10초 뒤에도 남아 있으면 실행할 명령과 함께 실패로 알리는지.
- `tests/integration/test_tmux_launchd_scripts.py` (가짜 tmux와 별도 socket의
  실제 tmux, bash 3.2와 dash)
  - `tmux-launchd-start`가 있는 서버를 그대로 두는지, 복구 결과를
    `@tmux-restore-status`와 로그에 남기는지, continuum이 건너뛴 복구를 손대지
    않은 로그인 세션에서만 직접 하는지.
  - `resurrect-autosave`의 로그 회전, 50초 제한, 75 처리, 저장 lock의 대체
    경로(flock, lockf, python3)와 도구가 없을 때의 실패, 저장 중 중단 시
    이전 저장본 유지.
- `tests/unit/test_darwin_proc.py`, `tests/unit/test_resurrect_ai_session.py`:
  macOS 프로세스 확인 경로(실제 libproc 시험은 macOS에서만). Codex가 연
  파일을 읽지 못하면 명령줄 ID로 대신하지 않고 비우는지.
- `tests/macos/e2e.sh` (CI의 macOS runner): one-liner 설치, 두 agent와 plist
  확인, 접속 없이 1분 저장, 설정 다시 불러오기 후 저장, kill-server 뒤
  `launchctl kickstart -k`로 세션·작업 디렉터리·출력 기록 복구, 서버가 있을 때의
  kickstart와 bootout·bootstrap에 같은 서버 유지, 바꿀 것이 없는 repair, `services
  disable`·`enable`, `restore --baseline`.

실제 Mac의 데스크톱 로그인과 재시동, 잠자기, 로그인 항목 토글, 로컬 네트워크와
파일 및 폴더 알림은 이 시험들이 확인하지 않는다. CI runner의 결과도 출시 전
점검일 뿐 실제 기기에서의 확인이 아니다(`docs/SUPPORT.md`).
