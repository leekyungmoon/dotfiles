local Plug = require('utils.plug_utils').Plug
local sidekick = require('config.sidekick')

return {
  Plug 'folke/sidekick.nvim' {
    enabled = sidekick.enabled,
    opts = sidekick.opts,
    keys = {
      { '<leader>ac', sidekick.toggle, desc = 'Toggle Copilot CLI' },
      { '<leader>af', sidekick.send_file, desc = 'Send current file to Copilot CLI' },
      { '<leader>av', sidekick.send_selection, mode = 'x', desc = 'Send selection to Copilot CLI' },
      { '<leader>ad', sidekick.send_diagnostics, desc = 'Send diagnostics to Copilot CLI' },
    },
  };
}
