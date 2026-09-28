"-------------
" plugins.vim
" DEPRECATED: This is no longer used in neovim, only for vanilla vim.
" vim: set ts=2 sts=2 sw=2 foldenable foldmethod=marker:
"-------------

if has('nvim')
  lua vim.notify("plugins.vim is no longer is used in neovim.", "error", { title = "vim/plugins.vim" })
  finish
endif

" Plug buffers appear in a new tab
let g:plug_window = '-tabnew'

"==============================================
" Plugins are runtime state and live outside the dotfiles checkout (installer-owned
" source): ${XDG_DATA_HOME:-~/.local/share}/vim/plugged, shared with lazy.nvim (nvim).
let $VIMPLUG = ($XDG_DATA_HOME =~# '^/' ? $XDG_DATA_HOME : expand('~/.local/share')) . '/vim/plugged'
" vim-plug itself is the pinned submodule; no autoload/plug.vim link inside the checkout needed.
if empty(globpath(&runtimepath, 'autoload/plug.vim'))
  execute 'source' fnameescape(expand('<sfile>:p:h') . '/bundle/vim-plug/plug.vim')
endif
call plug#begin($VIMPLUG)
"==============================================

Plug 'flazz/vim-colorschemes'
Plug 'tweekmonster/helpful.vim', { 'on' : ['HelpfulVersion'] }
Plug 'dstein64/vim-startuptime', { 'on': ['StartupTime'] }

Plug 'vim-airline/vim-airline'
Plug 'vim-airline/vim-airline-themes'

" Only the Vim plugin half of fzf: the fzf binary itself is the pinned,
" verified one the installer puts on PATH, so nothing is cloned into ~/.fzf.
Plug 'junegunn/fzf'
Plug 'junegunn/fzf.vim'
if v:version >= 800
  Plug 'mg979/vim-xtabline'
endif

Plug 'scrooloose/nerdtree'
Plug 'jistr/vim-nerdtree-tabs'
Plug 'christoomey/vim-tmux-navigator'
Plug 'tmux-plugins/vim-tmux-focus-events'
Plug 'tpope/vim-fugitive'

Plug 'tpope/vim-surround'
Plug 'tpope/vim-repeat'
Plug 'haya14busa/vim-asterisk'
Plug 'tpope/vim-commentary'
Plug 'sheerun/vim-polyglot', {'tag': 'v4.2.1'}
Plug 'tmux-plugins/vim-tmux'
Plug 'fladson/vim-kitty', { 'for': ['kitty'] }

" =======================================================
" Additional, optional local plugins
" =======================================================
if filereadable(expand("\~/.vim/plugins.local.vim"))
  source \~/.vim/plugins.local.vim
endif

call plug#end()

" :PlugUpgrade would rewrite plug.vim, i.e. the pinned submodule in the checkout.
command! -nargs=0 -bar PlugUpgrade echoerr 'PlugUpgrade is disabled: vim-plug is pinned by the dotfiles installer (use: dotfiles update)'

" Automatically install missing plugins on startup
function! s:plug_missing(plug)
  return !isdirectory(a:plug.dir) && !empty(get(a:plug, "uri"))
endfunction
let g:plugs_missing_on_startup = filter(values(g:plugs), 's:plug_missing(v:val)')
if len(g:plugs_missing_on_startup) > 0
  PlugInstall --sync | q
endif
