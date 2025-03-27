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
alias td='tmux detach'
alias tn='tmux new -s'
alias ta='tmux attach -t'
alias tk='tmux kill-session -t'
