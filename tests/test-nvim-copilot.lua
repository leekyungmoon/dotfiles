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

local original_state_home = vim.env.XDG_STATE_HOME
local state = vim.fn.tempname()
vim.fn.mkdir(state .. '/personal-dotfiles', 'p')
vim.env.XDG_STATE_HOME = state

local choices = state .. '/personal-dotfiles/choices.json'
local function write(value)
  vim.fn.writefile({ value }, choices)
end

local copilot = require('config.copilot')
expect(not copilot.enabled(), 'a missing choice must disable Copilot')

write('{broken')
expect(not copilot.enabled(), 'malformed choices must disable Copilot')

write('{"copilot":"yes"}')
expect(not copilot.enabled(), 'a non-boolean choice must disable Copilot')

write('{"copilot":false}')
expect(not copilot.enabled(), 'copilot=false must disable Copilot')

write('{"copilot":true}')
expect(copilot.enabled(), 'copilot=true must enable Copilot')

vim.env.XDG_STATE_HOME = 'relative/state'
expect(copilot.choice_path() == vim.fn.expand('~/.local/state/personal-dotfiles/choices.json'),
  'a relative XDG_STATE_HOME must be ignored')
vim.env.XDG_STATE_HOME = state

local tab_mapping_before = vim.fn.maparg('<Tab>', 'i', false, true)
copilot.setup()
expect(vim.g.copilot_no_tab_map == true, 'the Copilot Tab mapping must be disabled')
local mapping = vim.fn.maparg('<M-y>', 'i', false, true)
expect(mapping.expr == 1, '<M-y> must be an expression mapping')
expect(mapping.replace_keycodes == 0, '<M-y> must preserve Copilot keycodes')
expect(mapping.rhs == 'copilot#Accept("\\<CR>")', '<M-y> must accept Copilot suggestions')

package.loaded['config.keymap'] = nil
require('config.keymap')
mapping = vim.fn.maparg('<M-y>', 'i', false, true)
expect(mapping.rhs == 'copilot#Accept("\\<CR>")',
  '<M-y> must still accept Copilot after config.keymap loads')
local ctrl_j = vim.fn.maparg('<C-j>', 'i', false, true)
expect(type(ctrl_j.callback) == 'function',
  '<C-j> must retain the snippet and pair-jump callback from config.keymap')
expect(vim.deep_equal(vim.fn.maparg('<Tab>', 'i', false, true), tab_mapping_before),
  'Copilot setup must not replace <Tab>')

local specs = dofile(repo .. '/nvim/lua/plugins/copilot.lua')
local spec = assert(specs[1], 'Copilot plugin spec is missing')
expect(spec[1] == 'github/copilot.vim', 'the official Copilot plugin must be used')
expect(spec.enabled(), 'the plugin spec must honor the saved choice')
expect(spec.lazy ~= true, 'Copilot must be available for :Copilot setup at startup')
expect(copilot.auth_status() == 'unknown',
  'an unavailable Copilot runtime must have an unknown auth status')

local autoload = state .. '/runtime/autoload'
vim.fn.mkdir(autoload, 'p')
vim.fn.writefile({
  'function! copilot#Call(method, params) abort',
  "  let g:copilot_test_local_only = a:params.options.localChecksOnly",
  "  return {'status': g:copilot_test_status}",
  'endfunction',
}, autoload .. '/copilot.vim')
vim.opt.runtimepath:prepend(state .. '/runtime')
for _, status in ipairs({ 'OK', 'MaybeOK', 'NotSignedIn', 'NotAuthorized' }) do
  vim.g.copilot_test_status = status
  expect(copilot.auth_status() == status, 'auth status must preserve ' .. status)
end
expect(vim.g.copilot_test_local_only == true, 'auth status must perform local checks only')
vim.g.copilot_test_status = 'SensitiveAccountData'
expect(copilot.auth_status() == 'unknown', 'unexpected auth output must be redacted')

vim.api.nvim_create_user_command('Copilot', function()
  vim.g.copilot_test_command_ran = true
end, {})
package.loaded['config.lsp'] = nil
local result = require('config.lsp')._setup_inline_completion()
expect(result == false, 'the legacy Copilot LSP command must be skipped')
vim.cmd('Copilot')
expect(vim.g.copilot_test_command_ran == true,
  'the official Copilot command must not be overwritten')

vim.api.nvim_del_user_command('Copilot')
write('{"copilot":false}')
vim.cmd([[function! CommandAlias(...) abort
endfunction]])
require('config.lsp')._setup_inline_completion()
expect(vim.fn.exists(':Copilot') == 2,
  'the legacy Copilot LSP command must remain available when the plugin is not selected')

vim.env.XDG_STATE_HOME = original_state_home
print('nvim copilot tests: PASS')
