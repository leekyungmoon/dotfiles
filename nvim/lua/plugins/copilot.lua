local Plug = require('utils.plug_utils').Plug
local copilot = require('config.copilot')

return {
  Plug 'github/copilot.vim' {
    enabled = copilot.enabled,
    init = copilot.setup,
  };
}
