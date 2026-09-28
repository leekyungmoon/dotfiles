-- UI-related plugins.
---@diagnostic disable: missing-fields

local Plug = require('utils.plug_utils').Plug
local PlugConfig = require('utils.plug_utils').PlugConfig
local UpdateRemotePlugins = require('utils.plug_utils').UpdateRemotePlugins

local has_py3 = function(p) return require('config.pynvim')() end

return {
  -- Basic UI Components
  Plug 'MunifTanjim/nui.nvim' { lazy = true };  -- see config/ui.lua
  Plug 'folke/snacks.nvim' {
    priority = 1000,
    config = require('config.ui').setup_snacks,
  };

  -- for vim.ui.input() with multiline support
  Plug 'wookayin/multinput.nvim' {
    config = function()
      require('config.ui').setup_input()
    end
  };

  -- FZF & Grep
  -- Only the Vim plugin half of fzf, from the plugin root like any other
  -- plugin (no build step): fzf#exec() uses the pinned, verified fzf binary
  -- that the installer puts on $PATH, so no separate fzf clone is needed.
  Plug 'junegunn/fzf' {
    name = 'fzf',
    cmd = 'FZF', func = 'fzf#*',
  };
  Plug 'ibhagwan/fzf-lua' {
    -- We require >=0.7 (2025.2), but tagged v0.7 doesn't work because it's buggy on nvim-nightly (0.12)
    branch = 'main',  -- use HEAD version
    event = { 'VeryLazy', 'CmdlineEnter' },
    config = require('config.fzf').setup,
  };
  Plug 'rking/ag.vim' { func = 'ag#*', lazy = true };

  -- Telescope (config/telescope.lua)
  Plug 'nvim-telescope/telescope.nvim' {
    enabled = vim.fn.has('nvim-0.9.0') > 0,
    event = 'CmdlineEnter',
    config = function()
      require('config.telescope').setup()
    end,
  };

  -- Terminal
  Plug 'voldikss/vim-floaterm' { event = 'CmdlineEnter' };

  -- Wildmenu
  Plug 'wookayin/wilder.nvim' {
    dependencies = {'romgrk/fzy-lua-native'},
    cond = has_py3,
    build = UpdateRemotePlugins,
    event = 'CmdlineEnter',
    func = 'wilder#*',
  };

  -- Explorer
  Plug 'nvim-neo-tree/neo-tree.nvim' {
    branch = 'main',
    -- version = '>=3.34',
    commit = '19d20a9', -- 3.35+ needed for nvim 0.13 due to BufModifiedSet
    init = function() vim.g.neo_tree_remove_legacy_commands = 1; end,
    config = require('config.neotree').setup_neotree,
  };

  -- Navigation
  Plug 'vim-voom/VOoM' { cmd = { 'Voom', 'VoomToggle' } };
  Plug 'majutsushi/tagbar' { cmd = { 'Tagbar', 'TagbarOpen', 'TagbarToggle' } };

  -- Quickfix
  Plug 'kevinhwang91/nvim-bqf' { ft = 'qf', config = require('config.quickfix').setup_bqf };

  -- Marks and Signs
  Plug 'kshenoy/vim-signature' {
    event = 'VeryLazy',
    config = function()
      -- hlgroups are registered on VimEnter, so need to setup after lazy loading
      pcall(vim.fn['signature#utils#SetupHighlightGroups'])
    end
  };
  Plug 'vim-scripts/errormarker.vim' { event = 'VeryLazy' };
}
