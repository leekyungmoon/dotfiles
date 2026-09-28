# tmux 작업환경 자동 저장·복구

## 목적

PC 재부팅이나 예기치 않은 종료 이후에도 터미널 작업환경을 빠르게 다시
사용할 수 있도록, 작업 구성을 주기적으로 저장하고 데스크톱 로그인 시 자동
복구한다.

## 적용 구성

- **tmux-resurrect:** 세션·창·분할 화면 구성, 작업 디렉터리와 터미널 출력
  기록(scrollback)을 저장·복구한다.
- **tmux-continuum:** tmux 서버가 시작될 때 마지막 저장본을 자동 복구한다.
- **1분 주기 사용자 타이머** (`tmux-resurrect-autosave.timer`): 터미널을 닫아
  tmux에 접속한 화면이 없어도 1분마다 저장한다.
- **systemd 사용자 서비스** (`tmux.service`): Ubuntu 데스크톱 로그인 시 tmux를
  백그라운드로 시작한다.
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
| `systemd/user/tmux.service` | 로그인 시 tmux 백그라운드 시작, 종료 시 저장 후 서버 종료 |
| `systemd/user/tmux-resurrect-autosave.{service,timer}` | 1분 주기 저장 |
| `manifests/tmux-plugins.json` | TPM과 plugin의 고정 commit |

표의 경로는 저장소 checkout인 `~/.dotfiles` 기준이다. unit 파일은
`~/.config/systemd/user/`에 복사되며, 예전 로컬 설정이 남긴
`~/.config/systemd/user/tmux.service.d/login.conf` drop-in은 배포된 unit을
덮어쓰므로 설치기가 제거한다(백업 후 제거, [RECOVERY.md](RECOVERY.md) 참고).

plugin은 `${XDG_DATA_HOME:-$HOME/.local/share}/tmux/plugins`
(`TMUX_PLUGIN_MANAGER_PATH`)에, 저장본은
`${XDG_DATA_HOME:-$HOME/.local/share}/tmux/resurrect`에 둔다. 설치기는 plugin을
manifest의 commit 그대로 받는다.

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

설치기로 켠 unit을 직접 끄면 설치기 상태 확인에서 변경된 항목으로 보일 수
있다.

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
