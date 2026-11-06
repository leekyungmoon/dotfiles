local M = {}

function M.enabled()
  return require('config.copilot').choice('copilot-cli')
end

function M.copilot_command()
  local home = vim.env.HOME or vim.fn.expand('~')
  local managed = vim.fs.joinpath(home, '.local', 'bin', 'copilot')
  if vim.fn.executable(managed) == 1 then
    return { managed, '--banner' }
  end

  local installed = vim.fn.exepath('copilot')
  return { installed ~= '' and installed or 'copilot', '--banner' }
end

function M.opts()
  return {
    nes = { enabled = false },
    cli = {
      watch = true,
      mux = {
        enabled = true,
        backend = 'tmux',
        create = 'terminal',
      },
      tools = {
        copilot = { cmd = M.copilot_command() },
      },
    },
  }
end

function M.toggle()
  require('sidekick.cli').toggle({ name = 'copilot', focus = true })
end

function M.send_file()
  require('sidekick.cli').send({ name = 'copilot', msg = '{file}' })
end

function M.send_selection()
  require('sidekick.cli').send({ name = 'copilot', msg = '{selection}' })
end

function M.send_diagnostics()
  require('sidekick.cli').send({ name = 'copilot', prompt = 'diagnostics' })
end

return M
