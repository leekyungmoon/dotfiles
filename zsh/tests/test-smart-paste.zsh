#!/usr/bin/env zsh

emulate -LR zsh
setopt errexit nounset pipefail
export LC_ALL=C.UTF-8

typeset -gr SMART_PASTE_TEST_DIR=${0:A:h}
source $SMART_PASTE_TEST_DIR/../zsh.d/zsh_custom_settings.zsh

integer assertions=0 failures=0

assert_success() {
    local name=$1 input=$2 expected=$3
    local REPLY
    (( ++assertions ))

    if ! _smart_paste_unwrap_shell_fence "$input"; then
        print -u2 -r -- "FAIL: $name (unexpected rejection)"
        (( ++failures ))
        return
    fi
    if [[ $REPLY != $expected ]]; then
        print -u2 -r -- "FAIL: $name (content changed)"
        print -u2 -r -- "  expected: ${(qqq)expected}"
        print -u2 -r -- "  actual:   ${(qqq)REPLY}"
        (( ++failures ))
    fi
}

assert_rejected() {
    local name=$1 input=$2
    local REPLY
    (( ++assertions ))

    if _smart_paste_unwrap_shell_fence "$input"; then
        print -u2 -r -- "FAIL: $name (unexpected extraction: ${(qqq)REPLY})"
        (( ++failures ))
        return
    fi
    if [[ $REPLY != $input ]]; then
        print -u2 -r -- "FAIL: $name (rejected input was modified)"
        (( ++failures ))
    fi
}

assert_repair_success() {
    local name=$1 input=$2 expected=$3
    local REPLY
    (( ++assertions ))

    if ! COLUMNS=40 SMART_PASTE_WRAP_MARGIN=12 SMART_PASTE_WRAP_VARIANCE=8 \
        _smart_paste_repair_text "$input"; then
        print -u2 -r -- "FAIL: $name (unexpected repair rejection)"
        (( ++failures ))
        return
    fi
    if [[ $REPLY != $expected ]]; then
        print -u2 -r -- "FAIL: $name (wrong repair)"
        print -u2 -r -- "  expected: ${(qqq)expected}"
        print -u2 -r -- "  actual:   ${(qqq)REPLY}"
        (( ++failures ))
    fi
}

assert_repair_rejected() {
    local name=$1 input=$2
    local REPLY
    (( ++assertions ))

    if COLUMNS=80 SMART_PASTE_WRAP_MARGIN=12 SMART_PASTE_WRAP_VARIANCE=8 \
        _smart_paste_repair_text "$input"; then
        print -u2 -r -- "FAIL: $name (unsafe repair: ${(qqq)REPLY})"
        (( ++failures ))
    fi
}

assert_path_success() {
    local name=$1 input=$2 expected=$3
    local REPLY
    (( ++assertions ))

    if ! _smart_paste_repair_existing_path "$input"; then
        print -u2 -r -- "FAIL: $name (unexpected path-repair rejection)"
        (( ++failures ))
        return
    fi
    if [[ $REPLY != $expected ]]; then
        print -u2 -r -- "FAIL: $name (wrong path repair)"
        print -u2 -r -- "  expected: ${(qqq)expected}"
        print -u2 -r -- "  actual:   ${(qqq)REPLY}"
        (( ++failures ))
    fi
}

assert_path_rejected() {
    local name=$1 input=$2
    local REPLY
    (( ++assertions ))

    if _smart_paste_repair_existing_path "$input"; then
        print -u2 -r -- "FAIL: $name (unsafe path repair: ${(qqq)REPLY})"
        (( ++failures ))
        return
    fi
    if [[ $REPLY != $input ]]; then
        print -u2 -r -- "FAIL: $name (rejected path input was modified)"
        (( ++failures ))
    fi
}

typeset TEST_PASTE BUFFER CONTEXT REPLY POSTDISPLAY
typeset -gi CURSOR YANK_START YANK_END HIGHLIGHT_CALLS
typeset -gi ORIGINAL_PASTE_STATUS=0
typeset -ga ZLE_CALLS

zle() {
    if [[ ${1-} == _smart_original_bracketed_paste ]]; then
        local paste=${TEST_PASTE//$'\r'/$'\n'}
        local start=$CURSOR
        BUFFER=${BUFFER[1,CURSOR]}$paste${BUFFER[CURSOR+1,-1]}
        CURSOR=$(( start + $#paste ))
        YANK_START=$start
        YANK_END=$CURSOR
        return $ORIGINAL_PASTE_STATUS
    fi
    ZLE_CALLS+=("${(j: :)@}")
}

_zsh_highlight() {
    (( ++HIGHLIGHT_CALLS ))
}

typeset test_root
test_root=$(mktemp -d "${TMPDIR:-/tmp}/smart-paste.XXXXXXXX")
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM
mkdir -p -- "$test_root/reviews"
: >| "$test_root/reviews/ai-code-review.md"
: >| "$test_root/existing-prefix"
: >| "$test_root/existing-prefixsuffix"
: >| "$test_root/reviews/ai code-review.md"
: >| "$test_root/reviews/ai;code-review.md"
: >| "$test_root/reviews/ai-code-review meta.md"
: >| "$test_root/reviews/ai-code-review;meta.md"
ln -s -- "$test_root/reviews/missing-target" \
    "$test_root/reviews/dangling-review.md"
ln -s -- "$test_root/reviews/missing-target" \
    "$test_root/dangling-prefix"
: >| "$test_root/dangling-prefixsuffix"
mkdir -p -- "$test_root/reviews/한글"
: >| "$test_root/reviews/한글/문서-보고서.md"

typeset three_row_middle=abcdefghijklmnopqrstuvwxyz-0123456789-
: >| "$test_root/reviews/three-row-$three_row_middle"'final.md'
typeset intermediate_middle=abcdefghijklmnopqrstuvwxyz-9876543210-
: >| "$test_root/reviews/intermediate-$intermediate_middle"
: >| "$test_root/reviews/intermediate-$intermediate_middle"'final.md'
typeset dangling_middle=abcdefghijklmnopqrstuvwxyz-2468135790-
ln -s -- "$test_root/reviews/missing-target" \
    "$test_root/reviews/dangling-intermediate-$dangling_middle"
: >| "$test_root/reviews/dangling-intermediate-$dangling_middle"'final.md'
typeset short_middle_prefix=abcdefghijklmnopqrstuvwxyz-1357924680-
: >| "$test_root/reviews/short-middle-$short_middle_prefix"'xfinal.md'
typeset relative_middle=abcdefghijklmnopqrstuvwxyz-1122334455-
: >| "$test_root/reviews/relative-$relative_middle"'final.md'

assert_widget_result() {
    local name=$1 initial=$2 paste=$3 expected=$4
    local expected_highlights=$5 expected_calls=$6
    local widget_context=${7:-start}
    local initial_cursor=${8:-${#initial}}
    local normalized_paste=${paste//$'\r'/$'\n'}
    local actual_calls expected_postdisplay=stale
    integer expected_cursor=$(( initial_cursor + $#normalized_paste ))
    integer expected_yank_end=$expected_cursor
    (( ++assertions ))

    BUFFER=$initial
    CURSOR=$initial_cursor
    CONTEXT=$widget_context
    TEST_PASTE=$paste
    REPLY=poison
    YANK_START=-1
    YANK_END=-1
    POSTDISPLAY=stale
    HIGHLIGHT_CALLS=0
    ZLE_CALLS=()

    if ! _smart_bracketed_paste; then
        print -u2 -r -- "FAIL: $name (widget returned nonzero)"
        (( ++failures ))
        return
    fi
    actual_calls=${(j:|:)ZLE_CALLS}
    if (( expected_highlights )); then
        expected_postdisplay=
        expected_cursor=$#expected
        expected_yank_end=$#expected
    fi
    if [[ $BUFFER != $expected || $CURSOR -ne $expected_cursor ||
          $HIGHLIGHT_CALLS -ne $expected_highlights ||
          $actual_calls != $expected_calls ||
          $actual_calls == *accept-line* ||
          $YANK_START -ne $initial_cursor ||
          $YANK_END -ne $expected_yank_end ||
          $POSTDISPLAY != $expected_postdisplay ]]; then
        print -u2 -r -- "FAIL: $name (widget result mismatch)"
        print -u2 -r -- "  expected: ${(qqq)expected}"
        print -u2 -r -- "  actual:   ${(qqq)BUFFER}"
        print -u2 -r -- "  cursor/highlights: $CURSOR/$HIGHLIGHT_CALLS"
        print -u2 -r -- "  yank: $YANK_START/$YANK_END"
        print -u2 -r -- "  postdisplay: ${(qqq)POSTDISPLAY}"
        print -u2 -r -- "  ZLE calls: ${(qqq)actual_calls}"
        (( ++failures ))
    fi
}

assert_delegate_failure_propagates() {
    local name=$1 initial=$2 initial_cursor=$3
    local delegate_context=$4 paste=$5
    local normalized_paste=${paste//$'\r'/$'\n'}
    local expected
    integer expected_cursor=$(( initial_cursor + $#normalized_paste ))
    integer delegate_status
    (( ++assertions ))

    expected=${initial[1,initial_cursor]}$normalized_paste${initial[initial_cursor+1,-1]}
    BUFFER=$initial
    CURSOR=$initial_cursor
    CONTEXT=$delegate_context
    TEST_PASTE=$paste
    ORIGINAL_PASTE_STATUS=42
    YANK_START=-1
    YANK_END=-1
    POSTDISPLAY=stale
    HIGHLIGHT_CALLS=0
    ZLE_CALLS=()

    if _smart_bracketed_paste; then
        delegate_status=0
    else
        delegate_status=$?
    fi
    ORIGINAL_PASTE_STATUS=0

    if (( delegate_status != 42 )) || [[ $BUFFER != $expected ||
          $CURSOR -ne $expected_cursor ||
          $YANK_START -ne $initial_cursor ||
          $YANK_END -ne $expected_cursor || $POSTDISPLAY != stale ||
          $HIGHLIGHT_CALLS -ne 0 || $#ZLE_CALLS -ne 0 ]]; then
        print -u2 -r -- "FAIL: $name (delegate failure propagation)"
        print -u2 -r -- "  status/buffer: $delegate_status/${(qqq)BUFFER}"
        print -u2 -r -- "  ZLE calls: ${(qqq)ZLE_CALLS}"
        (( ++failures ))
    fi
}

assert_repair_widget_success() {
    local name=$1 input=$2 expected=$3
    local actual_calls
    (( ++assertions ))

    BUFFER=$input
    CURSOR=$#BUFFER
    HIGHLIGHT_CALLS=0
    ZLE_CALLS=()

    if ! COLUMNS=40 SMART_PASTE_WRAP_MARGIN=12 \
        SMART_PASTE_WRAP_VARIANCE=8 _smart_paste_repair_buffer; then
        print -u2 -r -- "FAIL: $name (repair widget rejected input)"
        (( ++failures ))
        return
    fi

    actual_calls=${(j:|:)ZLE_CALLS}
    if [[ $BUFFER != $expected || $CURSOR -ne $#expected ||
          $HIGHLIGHT_CALLS -ne 1 ||
          $actual_calls != '.split-undo|redisplay' ||
          $actual_calls == *accept-line* ]]; then
        print -u2 -r -- "FAIL: $name (repair widget result mismatch)"
        print -u2 -r -- "  expected: ${(qqq)expected}"
        print -u2 -r -- "  actual:   ${(qqq)BUFFER}"
        print -u2 -r -- "  ZLE calls: ${(qqq)actual_calls}"
        (( ++failures ))
    fi
}

assert_raw_output_mode_false() {
    local config=$HOME/.codex/config.toml
    (( ++assertions ))

    if ! command python3 -c \
        'import pathlib, sys, tomllib
p = pathlib.Path(sys.argv[1])
d = tomllib.loads(p.read_text())
if d.get("tui", {}).get("raw_output_mode") is not False:
    raise SystemExit(1)' \
        "$config"; then
        print -u2 -r -- 'FAIL: tui.raw_output_mode must parse as false'
        (( ++failures ))
    fi
}

assert_interactive_install() {
    local source_path=$SMART_PASTE_TEST_DIR/../zsh.d/zsh_custom_settings.zsh
    local output
    local probe=$'emulate -LR zsh\nsetopt errexit nounset pipefail\nfunction _test_original_paste() { :; }\nzle -N bracketed-paste _test_original_paste\nsource "$SMART_PASTE_SOURCE"\nfirst_saved=${widgets[_smart_original_bracketed_paste]-}\nfirst_active=${widgets[bracketed-paste]-}\nsource "$SMART_PASTE_SOURCE"\n[[ $first_saved == user:_test_original_paste ]]\n[[ ${widgets[_smart_original_bracketed_paste]-} == $first_saved ]]\n[[ $first_active == user:_smart_bracketed_paste ]]\n[[ ${widgets[bracketed-paste]-} == $first_active ]]\nzle -A bracketed-paste _test_saved_smart_paste\nfunction _test_outer_paste() { zle _test_saved_smart_paste; }\nzle -N bracketed-paste _test_outer_paste\nouter_active=${widgets[bracketed-paste]-}\nsource "$SMART_PASTE_SOURCE"\n[[ $outer_active == user:_test_outer_paste ]]\n[[ ${widgets[bracketed-paste]-} == $outer_active ]]\n[[ ${widgets[_test_saved_smart_paste]-} == user:_smart_bracketed_paste ]]\n[[ ${widgets[_smart_original_bracketed_paste]-} == $first_saved ]]\n[[ ${widgets[smart-paste-repair]-} == user:_smart_paste_repair_buffer ]]\n[[ $(bindkey -M emacs "^[j") == \"\\\"^[j\\\" smart-paste-repair\" ]]\n[[ $(bindkey -M viins "^[j") == \"\\\"^[j\\\" smart-paste-repair\" ]]\n[[ $(bindkey -M vicmd "^[j") == \"\\\"^[j\\\" smart-paste-repair\" ]]\nprint -r -- INTERACTIVE_INSTALL_OK'
    (( ++assertions ))

    if ! output=$(SMART_PASTE_SOURCE=$source_path SMART_PASTE_PROBE=$probe \
        script -qefc 'zsh -dfi -c "$SMART_PASTE_PROBE"' /dev/null) ||
       [[ $output != *INTERACTIVE_INSTALL_OK* ]]; then
        print -u2 -r -- 'FAIL: interactive install and repeated source'
        print -u2 -r -- "  output: ${(qqq)output}"
        (( ++failures ))
    fi
}

for language in sh bash zsh shell; do
    assert_success "$language fence" \
        $'```'"$language"$'\ngsettings set org.gnome.desktop.interface text-scaling-factor 1.0\n```' \
        'gsettings set org.gnome.desktop.interface text-scaling-factor 1.0'
done

assert_success 'outer blank rows' \
    $'\n```bash\nprintf "%s\\n" hello\n```\n' \
    'printf "%s\n" hello'
assert_success 'multiline script is preserved' \
    $'~~~zsh\nfor item in one two; do\n  print -r -- "$item"\ndone\n~~~' \
    $'for item in one two; do\n  print -r -- "$item"\ndone'
assert_success 'longer closing fence' \
    $'````sh\nprintf ok\n`````' \
    'printf ok'
assert_success 'three-space fence indentation' \
    $'   ```bash extra-info\n   printf ok\n   ```' \
    'printf ok'
assert_success 'indented multiline fence preserves relative indentation' \
    $'  ```zsh\n  if true; then\n    printf ok\n  fi\n  ```' \
    $'if true; then\n  printf ok\nfi'
assert_success 'shorter fence text remains code' \
    $'````sh\nprintf before\n```\nprintf after\n````' \
    $'printf before\n```\nprintf after'
assert_success 'intentional trailing blank code row' \
    $'```sh\nprintf ok\n\n```' \
    $'printf ok\n'

assert_rejected 'whole response is not auto-executed' \
    $'Run this:\n\n```sh\nprintf ok\n```'
assert_rejected 'trailing prose is not auto-executed' \
    $'```sh\nprintf ok\n```\nDone.'
assert_rejected 'closing fence trailing text is rejected' \
    $'```sh\nprintf ok\n``` trailing'
assert_rejected 'one-character pseudo fence is rejected' \
    $'`sh\nprintf ok\n`'
assert_rejected 'backtick in fence info is rejected' \
    $'```sh `x`\nprintf ok\n```'
assert_rejected 'four-space closing fence is rejected' \
    $'```sh\nprintf ok\n    ```'
assert_rejected 'multiple blocks are not auto-executed' \
    $'```sh\nprintf one\n```\n```sh\nprintf two\n```'
assert_rejected 'non-shell fence' \
    $'```python\nprint("hello")\n```'
assert_rejected 'unclosed fence' \
    $'```sh\nprintf ok'
assert_rejected 'empty fence' \
    $'```sh\n\n```'
assert_rejected 'four-space-indented fence' \
    $'    ```sh\n    printf ok\n    ```'
assert_rejected 'blockquote fence' \
    $'> ```sh\n> printf ok\n> ```'
assert_rejected 'mismatched fence character' \
    $'```sh\nprintf ok\n~~~'
assert_rejected 'short closing fence' \
    $'````sh\nprintf ok\n```'
assert_rejected 'inline backticks' \
    'Use `printf ok` here.'
assert_rejected 'rejected CRLF input remains byte-for-byte text' \
    $'Run this:\r\n```sh\r\nprintf ok\r\n```\r\n'
assert_rejected 'raw CRLF shell fence is not guessed' \
    $'```sh\r\nprintf ok\r\n```'

assert_repair_success 'terminal-width option wrap' \
    $'printf 1234567890123456789012345\n--version' \
    'printf 1234567890123456789012345 --version'
assert_repair_success 'long option containing a path' \
    $'printf 1234567890123456789012345\n--output=/tmp/result' \
    'printf 1234567890123456789012345 --output=/tmp/result'
assert_repair_rejected 'short arbitrary path split' \
    $'rm -rf /tmp/safe\n/path'
assert_repair_rejected 'quoted newline' \
    $'printf "%s\n" "first\nsecond"'
assert_repair_rejected 'explicit continuation' \
    $'printf "%s" \\\nvalue'
assert_repair_rejected 'ordinary prose' \
    $'This is ordinary prose.\nIt must stay multiline.'
assert_repair_rejected 'repair candidate with invalid shell syntax' \
    $'true; function --version { print -r -- ORIGINAL; }                         \n--version'
assert_repair_rejected 'leading pipe prose remains multiline' \
    $'printf 1234567890123456789012345678901234567890123456789012345678901\n|this is prose, not a pipeline command'

typeset padded_prose='This is ordinary prose'
padded_prose=${(r:68::x:)padded_prose}
typeset padded_comment='printf payload # note'
padded_comment=${(r:68::x:)padded_comment}
typeset padded_quote='printf "'
padded_quote=${(r:68::x:)padded_quote}
typeset variance_first='printf '
variance_first=${(r:68::x:)variance_first}
typeset variance_second='--alpha='
variance_second=${(r:80::x:)variance_second}

assert_repair_rejected 'short known command misses display width' \
    $'printf abc\n--version'
assert_repair_rejected 'in-band ordinary prose is not a command' \
    "$padded_prose"$'\n--version'
assert_repair_rejected 'in-band shell comment stays structured' \
    "$padded_comment"$'\n--version'
assert_repair_rejected 'in-band quoted newline remains lexical' \
    "$padded_quote"$'\n--version"'
assert_repair_rejected 'in-band row variance remains ambiguous' \
    "$variance_first"$'\n'"$variance_second"$'\n--version'

assert_repair_widget_success 'Alt+J invokes the repair widget body' \
    $'printf 1234567890123456789012345\n--version' \
    'printf 1234567890123456789012345 --version'

assert_path_success 'wrapped existing absolute file' \
    "$test_root/reviews/ai-code-"$'\n  review.md' \
    "$test_root/reviews/ai-code-review.md"
assert_path_success 'wrapped existing Korean path' \
    "$test_root/reviews/한글/문서-"$'\n  보고서.md' \
    "$test_root/reviews/한글/문서-보고서.md"
assert_path_success 'three-row existing absolute file' \
    "$test_root/reviews/three-row-"$'\n  '"$three_row_middle"$'\n  final.md' \
    "$test_root/reviews/three-row-$three_row_middle"'final.md'
assert_path_rejected 'raw CRLF path is not guessed' \
    "$test_root/reviews/ai-code-"$'\r\n  review.md'
assert_path_rejected 'missing reconstructed path' \
    "$test_root/reviews/missing-"$'\n  file.md'
assert_path_rejected 'existing first row is ambiguous' \
    "$test_root/existing-prefix"$'\n  suffix'
assert_path_rejected 'dangling first-row entry is ambiguous' \
    "$test_root/dangling-prefix"$'\n  suffix'
assert_path_rejected 'existing intermediate row is ambiguous' \
    "$test_root/reviews/intermediate-"$'\n  '"$intermediate_middle"$'\n  final.md'
assert_path_rejected 'dangling intermediate row is ambiguous' \
    "$test_root/reviews/dangling-intermediate-"$'\n  '"$dangling_middle"$'\n  final.md'
assert_path_rejected 'short intermediate row is not a display wrap' \
    "$test_root/reviews/short-middle-$short_middle_prefix"$'\n  x\n  final.md'
assert_path_rejected 'unindented continuation is ambiguous' \
    "$test_root/reviews/ai-code-"$'\nreview.md'
assert_path_rejected 'command-prefixed path is not a standalone path' \
    "printf %s $test_root/reviews/ai-code-"$'\n  review.md'
assert_path_rejected 'shell metacharacters are not accepted' \
    "$test_root/reviews/ai;code-"$'\n  review.md'
assert_path_rejected 'continuation shell metacharacters are not accepted' \
    "$test_root/reviews/ai-code-"$'\n  review;meta.md'
assert_path_rejected 'spaces inside path fragments are not guessed' \
    "$test_root/reviews/ai code-"$'\n  review.md'
assert_path_rejected 'spaces inside continuation fragments are not guessed' \
    "$test_root/reviews/ai-code-"$'\n  review meta.md'
assert_path_rejected 'whole prose plus path is preserved' \
    "Artifact: $test_root/reviews/ai-code-"$'\n  review.md'
assert_path_rejected 'dangling symlink is not an existing destination' \
    "$test_root/reviews/dangling-"$'\n  review.md'
assert_path_rejected 'rows too short to be a display wrap' \
    $'/tm\n  p'
typeset original_test_pwd=$PWD
builtin cd -- "$test_root"
assert_path_rejected 'relative path is never auto-repaired' \
    "reviews/relative-$relative_middle"$'\n  final.md'
builtin cd -- "$original_test_pwd"

assert_widget_result 'widget unwraps an isolated shell fence' \
    '' $'```sh\nprintf ok\n```' 'printf ok' 1 \
    '.split-undo|-f yank|redisplay'
assert_widget_result 'widget extracts but never accepts a command' \
    '' $'```sh\nexit 77\n```' 'exit 77' 1 \
    '.split-undo|-f yank|redisplay'
assert_widget_result 'widget repairs a wrapped existing absolute path' \
    '' "$test_root/reviews/ai-code-"$'\n  review.md' \
    "$test_root/reviews/ai-code-review.md" 1 \
    '.split-undo|-f yank|redisplay'
assert_widget_result 'widget leaves ZLE-normalized CRLF path untouched' \
    '' "$test_root/reviews/ai-code-"$'\r\n  review.md' \
    "$test_root/reviews/ai-code-"$'\n\n  review.md' 0 '-f yank'
assert_widget_result 'widget preserves ambiguous rich-mode rows' \
    '' $'printf 1234567890123456789012345\n--version' \
    $'printf 1234567890123456789012345\n--version' 0 '-f yank'
assert_widget_result 'widget preserves a whole Markdown response' \
    '' $'Run this:\n```sh\nprintf ok\n```' \
    $'Run this:\n```sh\nprintf ok\n```' 0 '-f yank'
assert_widget_result 'widget does not transform a nonempty prompt' \
    'prefix ' $'```sh\nprintf ok\n```' \
    $'prefix ```sh\nprintf ok\n```' 0 ''
assert_widget_result 'widget preserves nonempty buffer at cursor zero' \
    $'\n' $'```sh\nprintf ok\n```' \
    $'```sh\nprintf ok\n```\n' 0 '' start 0
for context in cont select vared; do
    assert_widget_result "widget preserves an empty $context context" \
        '' "$test_root/reviews/ai-code-"$'\n  review.md' \
        "$test_root/reviews/ai-code-"$'\n  review.md' 0 '' "$context"
done

assert_interactive_install
assert_raw_output_mode_false
assert_delegate_failure_propagates 'delegate failure at empty primary prompt' \
    '' 0 start $'```sh\nprintf should-stay-raw\n```'
assert_delegate_failure_propagates 'delegate failure at nonempty prompt' \
    'prefix ' 7 start $'```sh\nprintf should-stay-raw\n```'
assert_delegate_failure_propagates 'delegate failure in cont context' \
    '' 0 cont $'```sh\nprintf should-stay-raw\n```'
assert_delegate_failure_propagates 'delegate failure in vared context' \
    '' 0 vared "$test_root/reviews/ai-code-"$'\n  review.md'

if (( failures > 0 )); then
    print -u2 -r -- "$failures of $assertions smart-paste assertions failed"
    exit 1
fi

print -r -- "ok: $assertions smart-paste assertions"
