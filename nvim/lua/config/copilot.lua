local M = {}

local function state_home()
  local configured = vim.env.XDG_STATE_HOME
  if configured and configured:sub(1, 1) == '/' then
    return configured
  end
  return vim.fn.expand('~/.local/state')
end

function M.choice_path()
  return vim.fs.joinpath(state_home(), 'personal-dotfiles', 'choices.json')
end

function M.enabled()
  local handle = io.open(M.choice_path(), 'r')
  if not handle then return false end

  local contents = handle:read('*a')
  handle:close()
  local ok, choices = pcall(vim.json.decode, contents)
  return ok and type(choices) == 'table' and choices.copilot == true
end

function M.auth_status()
  local ok, result = pcall(vim.fn['copilot#Call'], 'checkStatus', {
    options = { localChecksOnly = true },
  })
  local status = ok and type(result) == 'table' and result.status or nil
  if vim.tbl_contains({ 'OK', 'MaybeOK', 'NotSignedIn', 'NotAuthorized' }, status) then
    return status
  end
  return 'unknown'
end

function M.setup()
  vim.g.copilot_no_tab_map = true
  vim.keymap.set('i', '<M-y>', 'copilot#Accept("\\<CR>")', {
    expr = true,
    replace_keycodes = false,
    silent = true,
    desc = 'Accept GitHub Copilot suggestion',
  })
end

return M
