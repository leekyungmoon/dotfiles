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
  저장 hook이 실패하면 이전 저장본을 그대로 둔다.
- Codex·Claude는 실행 중인 프로세스에서 확인한 대화 ID만 저장하여 해당 대화를
  다시 연다(`tmux/resurrect-ai-session.py`, upstream 공식 post-save hook).
  확인되지 않은 ID는 추측하지 않고, 그 pane은 빈 shell로 복구한다.

파일 구성:

| 파일 | 역할 |
| --- | --- |
| `tmux/resurrect.conf` | plugin 선언과 옵션, prefix + C-s 저장 키, `default-command` |
| `tmux/resurrect-save` | upstream `save.sh`를 lock·hook 검증과 함께 실행 |
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
- **그래픽 환경 변수:** unit에 `DISPLAY`를 고정하지 않는다. GNOME 세션은
  `graphical-session.target` 전에 `DISPLAY`, `WAYLAND_DISPLAY` 등을 systemd
  사용자 관리자에 넣어 두고, 서비스는 그 값을 받아 tmux 서버를 시작한다.
  나중에 터미널에서 접속하면 tmux의 `update-environment`가 새 pane에 쓸 값을
  갱신한다.
- tmux 3.4(Ubuntu 24.04)는 `show-option -v` 출력에서 `$`를 `\$`로 바꾸므로,
  plugin 옵션 값에는 `$`를 넣지 않고 설정을 불러올 때 절대경로로 계산한다.

## 사용자 입장에서의 동작

1. 평소에는 별도 조작 없이 1분마다 현재 작업환경이 저장된다.
2. PC를 재부팅한 뒤 Ubuntu에 로그인한다.
3. tmux가 백그라운드에서 시작되어 마지막 저장본을 자동 복구한다. 로그인용
   임시 세션(`__continuum_startup`)은 다른 세션이 복구된 경우에만 정리된다.
4. 복구 명령이나 버튼을 누를 필요는 없다. 화면을 볼 때만 터미널에서 tmux에
   접속한다. 터미널 창을 강제로 열지는 않는다.

이미 tmux 서버에 세션이 있으면 자동 시작을 건너뛰어 기존 작업을 유지한다.
로그아웃이나 그래픽 세션 재시작만으로는 서비스가 멈추지 않는다
(`graphical-session.target`에 묶여 있지 않다). 다만 계정에 linger가 꺼져 있으면
마지막 세션이 끝날 때 systemd 사용자 관리자 자체가 종료된다. 이때 서비스는
먼저 저장한 뒤 tmux를 종료하고, 다음 로그인에서 복구한다.

## 복구 범위와 제한

- 저장하는 것은 **작업환경과 재개 정보**다. 실행 중 프로세스의 메모리 상태나
  완료되지 않은 계산을 그대로 보존하지 않는다.
- 일반 명령이나 SSH 연결은 자동으로 다시 실행하지 않는다. 다시 실행하는 것은
  resurrect 기본 목록(vi, less 등)과 hook이 확인한 `codex resume <ID>`,
  `<claude 경로> --resume <ID>`뿐이다. Claude 경로는 저장 시점 PATH에서 찾고,
  없으면 `~/.local/bin/claude`를 쓴다. 공백이 든 경로는 다시 실행하지 않는다.
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

- tmux를 시작할 때의 자동 복구만 끄기(upstream continuum 기능):

```sh
touch ~/tmux_no_auto_restore
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
    하나만 고르는지.
  - 일반 명령은 그대로 두고, 확인되지 않은 AI 명령은 비우는지. 저장본 쓰기가
    원자적이며 0600이고, 성공 표시가 성공한 경우에만 남는지.
  - 이 기능의 파일에 특정 사용자 홈의 절대경로와 고정된 DISPLAY 값이 없는지,
    unit의 로그인 조건·PATH·종료 순서와 1분 타이머, tmux.conf와
    resurrect.conf가 선언한 plugin이 모두 commit으로 고정됐는지.
- `tests/integration/test_tmux_resurrect_integration.py` (고정 commit의 upstream
  plugin을 임시 디렉터리에 받아 별도 socket의 tmux로 실행)
  - 설정이 저장 디렉터리·hook 경로를 절대경로로 풀고, plugin을 불러온 뒤에도
    C-s가 wrapper를 가리키는지.
  - continuum 부팅 처리가 unit 파일을 쓰지 않고 enable도 하지 않으며, 사용자가
    끈 unit을 다시 켜지 않는지(가짜 `systemctl`로 호출 기록).
  - 접속한 화면 없이 저장 → 서버 종료 → 복구 후 세션·창 이름·경로·pane
    좌표와 크기가 같은지, 저장 디렉터리 0700과 저장본 0600인지, 확인된 Codex·
    Claude 대화만 다시 실행되고 ID를 모르는 Claude pane은 빈 shell로 남는지,
    다시 복구해도 기존 pane 프로세스가 유지되는지.
  - 저장 hook이 실패하면 이전 저장본이 유지되는지.
  - 로그인용 임시 세션이 복구 전에는 남고, 복구 후에만 정리되는지.

실제 PC 재부팅, 데스크톱 로그인 시 서비스 시작, systemd 타이머의 실제 동작은
이 시험들이 확인하지 않는다. 각 PC에서 위 상태 확인 명령으로 확인한다.
