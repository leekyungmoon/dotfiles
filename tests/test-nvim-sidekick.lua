local repo = assert(vim.env.DOTFILES_TEST_ROOT, 'DOTFILES_TEST_ROOT is required')
package.path = table.concat({
  repo .. '/nvim/lua/?.lua',
  repo .. '/nvim/lua/?/init.lua',
  package.path,
}, ';')
vim.opt.runtimepath:prepend(repo .. '/nvim')

local function expect(condition, message)
  if not condition then error(message, 2) end
end

local protected_mappings = {}
for _, lhs in ipairs({ '<Tab>', '<C-j>', '<M-y>' }) do
  protected_mappings[lhs] = vim.fn.maparg(lhs, 'i', false, true)
end

local original_state_home = vim.env.XDG_STATE_HOME
local state = vim.fn.tempname()
vim.fn.mkdir(state .. '/personal-dotfiles', 'p')
vim.env.XDG_STATE_HOME = state
local choices = state .. '/personal-dotfiles/choices.json'
local function write(value)
  vim.fn.writefile({ value }, choices)
end

local copilot = require('config.copilot')
local sidekick = require('config.sidekick')
expect(not sidekick.enabled(), 'a missing choice must disable Sidekick')
write('{broken')
expect(not sidekick.enabled(), 'malformed choices must disable Sidekick')
write('{"copilot-cli":"yes"}')
expect(not sidekick.enabled(), 'a non-boolean choice must disable Sidekick')

write('{"copilot":true,"copilot-cli":false}')
expect(copilot.enabled(), 'inline Copilot must remain independently enabled')
expect(not sidekick.enabled(), 'copilot-cli=false must disable Sidekick')
write('{"copilot":false,"copilot-cli":true}')
expect(not copilot.enabled(), 'inline Copilot must remain independently disabled')
expect(sidekick.enabled(), 'copilot-cli=true must enable Sidekick')

local opts = sidekick.opts()
expect(opts.nes.enabled == false, 'Sidekick NES must stay disabled')
expect(opts.cli.watch == true, 'Sidekick must watch CLI edits for checktime')
expect(opts.cli.mux.enabled == true, 'Sidekick tmux persistence must be enabled')
expect(opts.cli.mux.backend == 'tmux', 'Sidekick must use tmux')
expect(opts.cli.mux.create == 'terminal', 'Sidekick must create a Neovim terminal')
expect(vim.tbl_count(opts.cli.tools) == 1 and opts.cli.tools.copilot ~= nil,
  'only the Copilot CLI backend may be customized')
local original_home = vim.env.HOME
local original_path = vim.env.PATH
local command_root = vim.fn.tempname()
local managed = command_root .. '/home/.local/bin/copilot'
local path_bin = command_root .. '/path-bin'
vim.fn.mkdir(vim.fs.dirname(managed), 'p')
vim.fn.mkdir(path_bin, 'p')
vim.fn.writefile({ '#!/bin/sh' }, managed)
vim.fn.writefile({ '#!/bin/sh' }, path_bin .. '/copilot')
vim.fn.setfperm(managed, 'rwx------')
vim.fn.setfperm(path_bin .. '/copilot', 'rwx------')
vim.env.HOME = command_root .. '/home'
vim.env.PATH = path_bin
expect(vim.deep_equal(sidekick.copilot_command(), { managed, '--banner' }),
  'Sidekick must prefer the installer-owned Copilot CLI')
vim.fn.delete(managed)
expect(vim.deep_equal(sidekick.copilot_command(), { path_bin .. '/copilot', '--banner' }),
  'Sidekick must use a Copilot CLI already installed on PATH')
vim.fn.delete(path_bin .. '/copilot')
expect(vim.deep_equal(sidekick.copilot_command(), { 'copilot', '--banner' }),
  'Sidekick must keep a clear fallback command when Copilot is unavailable')
vim.env.HOME = original_home
vim.env.PATH = original_path

local specs = dofile(repo .. '/nvim/lua/plugins/sidekick.lua')
local spec = assert(specs[1], 'Sidekick plugin spec is missing')
expect(spec[1] == 'folke/sidekick.nvim', 'the official Sidekick plugin must be used')
expect(spec.enabled(), 'the plugin spec must honor copilot-cli=true')
expect(spec.opts().nes.enabled == false, 'the plugin spec must keep NES disabled')

local expected_keys = {
  ['<leader>ac'] = 'n',
  ['<leader>af'] = 'n',
  ['<leader>av'] = 'x',
  ['<leader>ad'] = 'n',
}
for _, key in ipairs(spec.keys) do
  local expected_mode = assert(expected_keys[key[1]], 'unexpected Sidekick key: ' .. key[1])
  expect((key.mode or 'n') == expected_mode, 'wrong mode for ' .. key[1])
  expected_keys[key[1]] = nil
end
expect(vim.tbl_isempty(expected_keys), 'a requested Sidekick mapping is missing')

local calls = {}
package.loaded['sidekick.cli'] = {
  toggle = function(arg) calls[#calls + 1] = { method = 'toggle', arg = arg } end,
  send = function(arg) calls[#calls + 1] = { method = 'send', arg = arg } end,
}
sidekick.toggle()
sidekick.send_file()
sidekick.send_selection()
sidekick.send_diagnostics()
expect(vim.deep_equal(calls, {
  { method = 'toggle', arg = { name = 'copilot', focus = true } },
  { method = 'send', arg = { name = 'copilot', msg = '{file}' } },
  { method = 'send', arg = { name = 'copilot', msg = '{selection}' } },
  { method = 'send', arg = { name = 'copilot', prompt = 'diagnostics' } },
}), 'Sidekick actions must target only the Copilot CLI with the requested context')

for _, lhs in ipairs({ '<Tab>', '<C-j>', '<M-y>' }) do
  expect(vim.deep_equal(vim.fn.maparg(lhs, 'i', false, true), protected_mappings[lhs]),
    'Sidekick must not override ' .. lhs)
end

vim.env.XDG_STATE_HOME = original_state_home
print('nvim sidekick tests: PASS')
