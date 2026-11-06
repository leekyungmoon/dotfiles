-- Real Sidekick runtime fixture. The Copilot executable is a non-networked
-- recorder supplied by test_sidekick_runtime.py; no login or agent runs here.
local repo = assert(vim.env.SIDEKICK_TEST_ROOT)
local enabled = vim.env.SIDEKICK_TEST_ENABLED == '1'
package.path = table.concat({
  repo .. '/nvim/lua/?.lua',
  repo .. '/nvim/lua/?/init.lua',
  package.path,
}, ';')
vim.opt.runtimepath:prepend(repo .. '/nvim')
vim.opt.runtimepath:prepend(assert(vim.env.SIDEKICK_TEST_LAZY))
vim.g.mapleader = ','
vim.o.loadplugins = true
vim.env.VIMPLUG = vim.fs.joinpath(vim.env.XDG_DATA_HOME, 'vim', 'plugged')

require('lazy').setup({
  { import = 'plugins.sidekick' },
  { 'folke/sidekick.nvim', dir = assert(vim.env.SIDEKICK_TEST_PLUGIN) },
}, {
  root = vim.env.VIMPLUG,
  defaults = { lazy = false },
  install = { missing = false },
  checker = { enabled = false },
  change_detection = { enabled = false, notify = false },
  performance = { rtp = { reset = false } },
})

local function write_result(value)
  vim.fn.writefile({ vim.json.encode(value) }, assert(vim.env.SIDEKICK_TEST_RESULT))
end

if not enabled then
  write_result({
    enabled = require('config.sidekick').enabled(),
    loaded = package.loaded.sidekick ~= nil,
  })
  return
end

require('lazy.core.loader').load('sidekick.nvim', { start = 'runtime-test' })

local function wait_for(predicate, message, timeout)
  assert(vim.wait(timeout or 10000, predicate, 25), message)
end

local function log_text()
  local path = assert(vim.env.SIDEKICK_TEST_LOG)
  return vim.fn.filereadable(path) == 1 and table.concat(vim.fn.readfile(path), '\n') or ''
end

local project = assert(vim.env.SIDEKICK_TEST_PROJECT)
local sample = vim.fs.joinpath(project, 'sample.lua')
vim.cmd.cd(project)
vim.cmd.edit(sample)
local sample_buf = vim.api.nvim_get_current_buf()
local function focus_sample()
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == sample_buf then
      vim.api.nvim_set_current_win(win)
      return
    end
  end
  error('sample window disappeared')
end
wait_for(function() return package.loaded.sidekick ~= nil end,
  'Sidekick did not load: ' .. vim.inspect(vim.tbl_keys(require('lazy.core.config').plugins)))

local config = require('sidekick.config')
assert(config.nes.enabled == false, 'NES must be disabled')
assert(config.cli.watch == true, 'file watching must be enabled')
assert(config.cli.mux.enabled and config.cli.mux.backend == 'tmux', 'tmux mux must be enabled')
assert(config.cli.mux.create == 'terminal', 'mux must create a Neovim terminal')

local function mapped(lhs, mode)
  local map = vim.fn.maparg(lhs, mode or 'n', false, true)
  assert(type(map.callback) == 'function', 'missing mapping: ' .. lhs)
  return map.callback
end

mapped(',ac')()
wait_for(function() return log_text():find('START', 1, true) ~= nil end,
  'fake Copilot CLI did not start')
mapped(',af')()
wait_for(function() return log_text():find('INPUT\t@sample.lua', 1, true) ~= nil end,
  'file context was not sent')
local file_context = true

focus_sample()
vim.cmd('normal! gg0v$')
mapped(',av', 'x')()
wait_for(function() return log_text():find('alpha selected token', 1, true) ~= nil end,
  'visual selection was not sent')
local selection_context = true

focus_sample()
local ns = vim.api.nvim_create_namespace('sidekick-runtime-test')
vim.diagnostic.set(ns, 0, {
  { lnum = 0, col = 0, severity = vim.diagnostic.severity.ERROR,
    message = 'runtime diagnostic token' },
})
mapped(',ad')()
wait_for(function() return log_text():find('runtime diagnostic token', 1, true) ~= nil end,
  'diagnostics were not sent')
local diagnostic_context = true

local cli = require('sidekick.cli')
cli.hide({ name = 'copilot' })
cli.toggle({ name = 'copilot', focus = true })
vim.wait(500)
local _, starts = log_text():gsub('START', '')
assert(starts == 1, 'tmux session was restarted instead of reattached')

wait_for(function()
  local watch = require('sidekick.cli.watch')
  return watch.enabled and watch.watches[project] ~= nil
end, 'Sidekick watcher was not watching the project')
focus_sample()
vim.api.nvim_buf_set_lines(0, 0, 1, false, { 'dirty unsaved token' })
assert(vim.bo.modified, 'test buffer did not become dirty')
vim.fn.writefile({ 'disk replacement token', 'beta' }, sample)
vim.wait(500)
local dirty_preserved = vim.bo.modified
  and vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == 'dirty unsaved token'

write_result({
  enabled = require('config.sidekick').enabled(),
  nes = config.nes.enabled,
  dirty_preserved = dirty_preserved,
  starts = starts,
  file_context = file_context,
  selection_context = selection_context,
  diagnostic_context = diagnostic_context,
})
