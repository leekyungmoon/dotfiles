# custom functions
function _git_status {
    echo 
    git status
    zle reset-prompt
}


# zle handler
zle -N _git_status


# custom binding
bindkey '^E' fzf-cd-widget
bindkey '^F' forward-char
bindkey '^S' _git_status


alias tl='tmux ls'
alias tn='tmux new -s'
alias ta='tmux attach -t'
alias tk='tmux kill-session -t'
alias td='tmux detach'
# `trn NEW` renames the current session; `trn -t OLD NEW` targets another one.
# Deliberately not `tr`: coreutils tr is a real command and must stay reachable.
alias trn='tmux rename-session'


# Normalize safely recognizable command selections after bracketed paste reaches
# the ZLE buffer. Pure zsh: independent of tmux/Wayland clipboard utilities.
typeset -gi SMART_PASTE_WRAP_MARGIN=${SMART_PASTE_WRAP_MARGIN:-12}
typeset -gi SMART_PASTE_WRAP_VARIANCE=${SMART_PASTE_WRAP_VARIANCE:-8}
typeset -gi SMART_PASTE_PATH_MIN_WRAP_ROW=${SMART_PASTE_PATH_MIN_WRAP_ROW:-32}

_smart_paste_unwrap_shell_fence() {
    emulate -L zsh
    setopt extendedglob

    local original=$1 input=$1
    local line stripped info language fence_char rest candidate
    local -a lines block_lines
    integer first=1 last opener_len=0 run_len i saw_nonblank=0
    integer fence_indent=0 remove
    REPLY=$original

    lines=("${(@f)input}")
    last=$#lines

    # Terminal selection can include a blank row immediately outside the code
    # block. Ignore only those outer rows; never discard code-block content.
    while (( first <= last )) && [[ $lines[first] == [[:blank:]]# ]]; do
        (( first++ ))
    done
    while (( last >= first )) && [[ $lines[last] == [[:blank:]]# ]]; do
        (( last-- ))
    done
    (( last - first >= 2 )) || return 1

    stripped=$lines[first]
    i=0
    while (( i < 4 )) && [[ -n $stripped && $stripped[1] == ' ' ]]; do
        stripped=${stripped[2,-1]}
        (( ++i ))
    done
    (( i <= 3 )) || return 1
    fence_indent=$i

    fence_char=${stripped[1]-}
    [[ $fence_char == \` || $fence_char == '~' ]] || return 1
    rest=$stripped
    while [[ ${rest[1]-} == $fence_char ]]; do
        (( opener_len++ ))
        rest=${rest[2,-1]}
    done
    (( opener_len >= 3 )) || return 1
    [[ $fence_char != \` || $rest != *\`* ]] || return 1

    info=${${rest##[[:blank:]]#}%%[[:blank:]]#}
    [[ -n $info ]] || return 1
    language=${info%%[[:blank:]]*}
    [[ $language == (sh|bash|zsh|shell) ]] || return 1

    stripped=$lines[last]
    i=0
    while (( i < 4 )) && [[ -n $stripped && $stripped[1] == ' ' ]]; do
        stripped=${stripped[2,-1]}
        (( ++i ))
    done
    (( i <= 3 )) || return 1

    run_len=0
    rest=$stripped
    while [[ ${rest[1]-} == $fence_char ]]; do
        (( run_len++ ))
        rest=${rest[2,-1]}
    done
    (( run_len >= opener_len )) || return 1
    [[ $rest == [[:blank:]]# ]] || return 1

    # In Markdown the first matching fence closes the block. A matching line
    # before the selected text ends means this is not one isolated code block.
    for (( i=first+1; i < last; ++i )); do
        stripped=$lines[i]
        run_len=0
        while (( run_len < 4 )) && [[ -n $stripped && $stripped[1] == ' ' ]]; do
            stripped=${stripped[2,-1]}
            (( ++run_len ))
        done
        (( run_len <= 3 )) || continue

        run_len=0
        rest=$stripped
        while [[ ${rest[1]-} == $fence_char ]]; do
            (( run_len++ ))
            rest=${rest[2,-1]}
        done
        if (( run_len >= opener_len )) && [[ $rest == [[:blank:]]# ]]; then
            return 1
        fi
    done

    block_lines=("${(@)lines[first+1,last-1]}")
    (( $#block_lines > 0 )) || return 1
    for (( i=1; i <= $#block_lines; ++i )); do
        line=$block_lines[i]
        remove=0
        while (( remove < fence_indent )) &&
              [[ -n $line && $line[1] == ' ' ]]; do
            line=${line[2,-1]}
            (( ++remove ))
        done
        block_lines[i]=$line
        [[ $line == [[:blank:]]# ]] || {
            saw_nonblank=1
        }
    done
    (( saw_nonblank )) || return 1

    candidate=${(pj:\n:)block_lines}
    REPLY=$candidate
    return 0
}

_smart_paste_repair_existing_path() {
    emulate -L zsh
    setopt extendedglob

    local original=$1 input=$1
    local raw fragment candidate
    local -a lines
    integer i len
    REPLY=$original

    # This automatic repair is intentionally narrower than command repair:
    # only indented display-wrap rows that form one existing absolute path.
    [[ $input == *$'\n'* && $input != $'\n'* && $input != *$'\n' ]] || return 1
    lines=("${(@f)input}")
    (( $#lines >= 2 )) || return 1

    candidate=$lines[1]
    len=${#candidate}
    (( len >= SMART_PASTE_PATH_MIN_WRAP_ROW )) || return 1
    [[ $candidate == /* &&
       $candidate == [-[:alnum:]_./+@%:=,]## ]] || return 1
    [[ ! -e $candidate && ! -L $candidate ]] || return 1

    for (( i=2; i <= $#lines; ++i )); do
        raw=$lines[i]
        [[ $raw == [[:blank:]]* ]] || return 1
        fragment=${raw##[[:blank:]]#}
        [[ -n $fragment &&
           $fragment == [-[:alnum:]_./+@%:=,]## ]] || return 1
        candidate+=$fragment

        # An existing intermediate prefix is ambiguous; only the complete
        # reconstructed selection may resolve to a filesystem object.
        if (( i < $#lines )); then
            len=${#raw}
            (( len >= SMART_PASTE_PATH_MIN_WRAP_ROW )) || return 1
            [[ ! -e $candidate && ! -L $candidate ]] || return 1
        fi
    done

    # A dangling symlink is not an existing destination for this purpose.
    [[ -e $candidate ]] || return 1
    REPLY=$candidate
    return 0
}

_smart_paste_line_starts_command() {
    emulate -L zsh
    setopt extendedglob

    local raw=$1 line=${1##[[:space:]]#} token
    integer allow_unknown=${2:-0}
    local MATCH MBEGIN MEND
    local -a tokens match mbegin mend

    [[ -n $line ]] || return 1
    tokens=(${(z)line}) 2>/dev/null || return 1

    for token in "${tokens[@]}"; do
        token=${(Q)token}
        if [[ $token =~ '^[[:alpha:]_][[:alnum:]_]*=' ]]; then
            continue
        fi
        whence -w -- "$token" >/dev/null 2>&1 && return 0

        # Preserve an unindented, command-shaped line even when its executable
        # is not installed on this host. Mistaking it for a wrapped fragment is
        # more dangerous than leaving one paste unrepaired.
        if (( allow_unknown && $#tokens >= 2 )) && [[ $raw == $line &&
             $token == [[:alpha:]_][[:alnum:]_.+-]# ]]; then
            return 0
        fi
        if (( allow_unknown )) && [[ $raw == $line &&
             $token != [-+]* && $token == */* ]]; then
            return 0
        fi
        return 1
    done

    # An assignment-only line is also a complete shell command.
    (( $#tokens > 0 ))
}

_smart_paste_line_has_comment() {
    emulate -L zsh

    local token
    local -a tokens
    tokens=(${(z)1}) 2>/dev/null || return 1

    for token in "${tokens[@]}"; do
        [[ $token == \#* ]] && return 0
    done
    return 1
}

_smart_paste_must_keep_line_break() {
    emulate -L zsh
    setopt extendedglob

    local line=${${1}%%[[:space:]]#}
    [[ $line == *\\ ]]
}

_smart_paste_trailing_operator() {
    emulate -L zsh

    local token
    local -a tokens
    tokens=(${(z)1}) 2>/dev/null || return 1

    for token in "${tokens[@]}"; do
        [[ $token == \#* ]] && return 1
    done
    (( $#tokens > 0 )) || return 1
    [[ $tokens[-1] == ('|'|'||'|'|&'|'&&'|'&'|';'|'{'|'(') ]]
}

_smart_paste_has_lexical_newline() {
    emulate -L zsh

    local input=$1 state=plain char
    integer i escaped=0 dollar_prefix=0
    integer paren_depth=0 brace_depth=0 bracket_depth=0

    [[ $input == *$'\n'* ]] || return 1

    for (( i=1; i <= ${#input}; ++i )); do
        char=${input[i]}

        if [[ $char == $'\n' ]]; then
            if [[ $state != plain ]] ||
               (( escaped || paren_depth || brace_depth || bracket_depth )); then
                return 0
            fi
            dollar_prefix=0
            continue
        fi

        case $state in
            single)
                [[ $char == "'" ]] && state=plain
                ;;
            ansi)
                if (( escaped )); then
                    escaped=0
                elif [[ $char == \\ ]]; then
                    escaped=1
                elif [[ $char == "'" ]]; then
                    state=plain
                fi
                ;;
            double)
                if (( escaped )); then
                    escaped=0
                elif [[ $char == \\ ]]; then
                    escaped=1
                elif [[ $char == '$' ]]; then
                    # Quotes inside a substitution have their own lexical
                    # context. A compact hand-written scanner cannot safely
                    # distinguish them from the surrounding double quote, so
                    # preserve this multiline paste instead of risking a join.
                    case ${input[i+1]-} in
                        '('|'{'|'[') return 0 ;;
                    esac
                elif [[ $char == '`' ]]; then
                    return 0
                elif [[ $char == '"' ]]; then
                    state=plain
                fi
                ;;
            backtick)
                if (( escaped )); then
                    escaped=0
                elif [[ $char == \\ ]]; then
                    escaped=1
                elif [[ $char == '`' ]]; then
                    state=plain
                fi
                ;;
            plain)
                if (( escaped )); then
                    escaped=0
                    dollar_prefix=0
                    continue
                fi

                case $char in
                    \\)
                        escaped=1
                        dollar_prefix=0
                        ;;
                    "'")
                        if (( dollar_prefix )); then
                            state=ansi
                        else
                            state=single
                        fi
                        dollar_prefix=0
                        ;;
                    '"')
                        state=double
                        dollar_prefix=0
                        ;;
                    '`')
                        state=backtick
                        dollar_prefix=0
                        ;;
                    '$')
                        dollar_prefix=1
                        ;;
                    '(')
                        (( paren_depth++ ))
                        dollar_prefix=0
                        ;;
                    ')')
                        (( paren_depth > 0 )) && (( paren_depth-- ))
                        dollar_prefix=0
                        ;;
                    '{')
                        (( brace_depth++ ))
                        dollar_prefix=0
                        ;;
                    '}')
                        (( brace_depth > 0 )) && (( brace_depth-- ))
                        dollar_prefix=0
                        ;;
                    '[')
                        (( bracket_depth++ ))
                        dollar_prefix=0
                        ;;
                    ']')
                        (( bracket_depth > 0 )) && (( bracket_depth-- ))
                        dollar_prefix=0
                        ;;
                    *)
                        dollar_prefix=0
                        ;;
                esac
                ;;
        esac
    done

    return 1
}

_smart_paste_structured_text() {
    emulate -L zsh
    setopt extendedglob

    local input=$1 line trim
    local MATCH MBEGIN MEND
    local -a lines=("${(@f)input}") match mbegin mend

    [[ $input != $'\n'* && $input != *$'\n' &&
       $input != *$'\n\n'* && $input != *'<<'* ]] || return 0

    for line in "${lines[@]}"; do
        trim=${${line##[[:space:]]#}%%[[:space:]]#}
        [[ -n $trim ]] || return 0
        [[ $trim != '#'* ]] || return 0
        _smart_paste_line_has_comment "$line" && return 0
        if [[ $trim =~ '^(if|then|elif|else|fi|for|foreach|while|until|repeat|case|esac|select|function|do|done|try|always)([[:space:];]|$)' ]]; then
            return 0
        fi
    done

    return 1
}

_smart_paste_joiner() {
    emulate -L zsh
    setopt extendedglob

    local right=${1##[[:space:]]#}
    _smart_paste_join=

    [[ -n $right ]] || return 1

    # A long option cannot normally be a new command. Leading operators remain
    # ambiguous with pasted prose/Markdown and are intentionally preserved.
    if [[ $right == --* ]]; then
        _smart_paste_join=' '
        return 0
    fi

    return 1
}

_smart_paste_repair_text() {
    emulate -L zsh
    setopt extendedglob

    local input=${1//$'\r\n'/$'\n'}
    local line raw_right right out join _smart_paste_join
    local -a lines joiners
    integer i len width=${COLUMNS:-0}
    integer lower max_len=0 min_len=2147483647
    REPLY=$input

    [[ $input == *$'\n'* ]] || return 1
    _smart_paste_structured_text "$input" && return 1
    _smart_paste_has_lexical_newline "$input" && return 1
    lines=("${(@f)input}")
    (( $#lines >= 2 && width >= 20 )) || return 1

    # The first row must begin with a known command or contain only assignments.
    # Lexical multiline constructs were already rejected above.
    _smart_paste_line_starts_command "$lines[1]" || return 1

    lower=$(( width - SMART_PASTE_WRAP_MARGIN ))
    (( lower < 20 )) && lower=20

    # Every non-final row must look like a rendered terminal row. One doubtful
    # boundary preserves the complete paste; partial repair is never attempted.
    for (( i=1; i < $#lines; ++i )); do
        len=${#lines[i]}
        (( len >= lower && len <= width + 2 )) || return 1
        (( len < min_len )) && min_len=$len
        (( len > max_len )) && max_len=$len

        line=${${lines[i]}%%[[:space:]]#}
        raw_right=$lines[i+1]
        right=${raw_right##[[:space:]]#}
        _smart_paste_must_keep_line_break "$line" && return 1

        # A newline after a shell operator is semantically whitespace. Fold it
        # even before a known command; this also avoids multiline-paste quirks
        # in vi-mode/highlighting wrappers while preserving shell semantics.
        if _smart_paste_trailing_operator "$line"; then
            joiners[i]=' '
            continue
        fi

        _smart_paste_line_starts_command "$raw_right" 1 && return 1
        _smart_paste_joiner "$right" || return 1
        joiners[i]=$_smart_paste_join
    done

    (( max_len - min_len <= SMART_PASTE_WRAP_VARIANCE )) || return 1

    out=${${lines[1]}%%[[:space:]]#}
    for (( i=1; i < $#lines; ++i )); do
        right=${${lines[i+1]##[[:space:]]#}%%[[:space:]]#}
        join=$joiners[i]
        out+=$join$right
    done

    # Even an explicit repair must not turn valid text into malformed shell.
    command zsh -dfn -c "$out" </dev/null >/dev/null 2>&1 || return 1

    REPLY=$out
    return 0
}

_smart_bracketed_paste() {
    # REPLY is a conventional scratch variable in zsh plugins. Keep our result
    # dynamically scoped so the wrapped paste/highlighting chain is untouched.
    local before=$BUFFER before_cursor=$CURSOR paste_context=$CONTEXT
    local REPLY MATCH MBEGIN MEND
    local -a match mbegin mend
    zle _smart_original_bracketed_paste || return $?

    # Restrict automatic transformation to an isolated shell fence or an
    # indented display wrap that resolves to one existing absolute path.
    [[ -z $before && $before_cursor -eq 0 && $paste_context == start ]] || return 0

    if ! _smart_paste_unwrap_shell_fence "$BUFFER" &&
       ! _smart_paste_repair_existing_path "$BUFFER"; then
        # Helper calls can clear the yank classification left by the wrapped
        # widget. Reassert it so untouched multiline pastes keep normal ZLE
        # accept-line behavior.
        zle -f yank
        return 0
    fi

    zle .split-undo
    BUFFER=$REPLY
    CURSOR=$#BUFFER
    YANK_START=0
    YANK_END=$CURSOR
    POSTDISPLAY=
    zle -f yank
    (( $+functions[_zsh_highlight] )) && _zsh_highlight
    zle redisplay
}

_smart_paste_repair_buffer() {
    # Width-based repair is necessarily heuristic. Keep it behind an explicit
    # action instead of changing multiline shell input during ordinary paste.
    local REPLY MATCH MBEGIN MEND
    local -a match mbegin mend

    _smart_paste_repair_text "$BUFFER" || {
        zle -M 'No high-confidence display wrap found'
        return 1
    }

    zle .split-undo
    BUFFER=$REPLY
    CURSOR=$#BUFFER
    (( $+functions[_zsh_highlight] )) && _zsh_highlight
    zle redisplay
}

_smart_paste_install() {
    emulate -L zsh

    zmodload zsh/zleparameter 2>/dev/null
    if [[ -z ${widgets[_smart_original_bracketed_paste]-} ]]; then
        zle -A bracketed-paste _smart_original_bracketed_paste
        zle -N bracketed-paste _smart_bracketed_paste
    fi
    if [[ -z ${widgets[smart-paste-repair]-} ]]; then
        zle -N smart-paste-repair _smart_paste_repair_buffer
    fi
    bindkey -M emacs '^[j' smart-paste-repair
    bindkey -M viins '^[j' smart-paste-repair
    bindkey -M vicmd '^[j' smart-paste-repair
}

if [[ -o interactive ]]; then
    _smart_paste_install
fi
