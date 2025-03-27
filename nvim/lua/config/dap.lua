-- ~/.config/nvim/lua/config/dap.lua
local M = {}

local if_nil = function(value, val_nil, val_non_nil)
  if value == nil then return val_nil
  else return val_non_nil end
end

M.setup_sign = function()
  vim.fn.sign_define("DapBreakpoint",          { text = "🔴", texthl = "DapBreakpoint" })
  vim.fn.sign_define("DapBreakpointCondition", { text = "🟡", texthl = "DapBreakpointCondition" })
  vim.fn.sign_define("DapBreakpointRejected",  { text = "⭕", texthl = "DapBreakpointRejected" })
  vim.fn.sign_define("DapStopped", {
    text = "▶",
    texthl = "DapBreakpoint",
    linehl = "DapCurrentLine",
    numhl = "DiagnosticSignWarn",
  })

  require("utils.rc_utils").RegisterHighlights(function()
    vim.cmd [[
      hi DapBreakpoint   guifg=#e03131  ctermfg=Red
      hi DapCurrentLine  guibg=#304577
      hi def link DapBreakpointCondition DapBreakpoint
      hi def link DapBreakpointRejected  DapBreakpoint
    ]]
  end)
end

M.setup_ui = function()
  require("dapui").setup{}
end

M.setup_nextjs = function()
  local dap = require('dap')

  -- require("dap-vscode-js").setup({
  --   debugger_path = vim.fn.stdpath('data') .. "/lazy/vscode-js-debug",
  --   adapters = { 'pwa-node' },
  -- })

  dap.configurations.javascript = {
    {
      type = "pwa-node",
      request = "attach",
      name = "Attach Next.js (Docker)",
      address = "127.0.0.1",
      port = 9229,
      localRoot = "${workspaceFolder}",
      remoteRoot = "/app",  -- Docker 내부 프로젝트 경로 확인 필수
      sourceMaps = true,
    },
    {
      type = "pwa-node",
      request = "attach",
      name = "Attach Next.js Router Server (Docker)",
      address = "127.0.0.1",
      port = 9230,
      localRoot = "${workspaceFolder}",
      remoteRoot = "/app",  -- Docker 내부 프로젝트 경로 확인 필수
      sourceMaps = true,
    },
  }

  dap.configurations.typescript = dap.configurations.javascript
  dap.configurations.typescriptreact = dap.configurations.javascript
end

M.setup_lua = function()
  require("osv")
end

M.setup_python = function()
  require('dap-python').setup('python3')
end

M.setup = function()
  M.setup_sign()
  M.setup_ui()
  M.setup_nextjs()
  M.setup_lua()
  M.setup_python()
  -- 👇 이 부분을 추가하세요.
  -- M.setup_custom_commands()
  M.setup_nextjs()
end

if ... == nil then
  M.setup()
end

return M
