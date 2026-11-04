"""Opt-in network smoke test using real Neovim, lazy.nvim and copilot.vim.

Run with DOTFILES_COPILOT_NETWORK_TEST=1. Only a disposable HOME is used;
no account login or code-completion request is made. Neovim and Node.js must
already be available. The test downloads lazy.nvim, the official plugin and
its language server, just as the selected plugin does on first startup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from installer import phases
from installer.platform import Target
from installer.runner import Runner


@unittest.skipUnless(os.environ.get("DOTFILES_COPILOT_NETWORK_TEST") == "1",
                     "opt in to the real Copilot download/runtime smoke test")
class CopilotRuntimeTests(unittest.TestCase):
    def test_selection_plugin_command_mapping_and_unsigned_auth(self):
        nvim = shutil.which("nvim")
        self.assertIsNotNone(nvim, "Neovim must be installed")
        self.assertIsNotNone(shutil.which("node"), "Node.js must be installed")
        with tempfile.TemporaryDirectory(prefix="dotfiles-copilot-runtime-") as tmp:
            home = Path(tmp)
            target = Target(os.getuid(), os.getgid(), "fixture", home,
                            home / "data", home / "state", home / "config", home / "cache")
            env = phases.child_env(target, {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8", "DOTFILES_TEST_ROOT": str(ROOT),
                "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            })
            lazy = target.data_home / "vim/plugged/lazy.nvim"
            subprocess.run([
                "git", "clone", "--depth=1", "--branch=v9.25.1", "--single-branch",
                "https://github.com/folke/lazy.nvim.git", str(lazy),
            ], env=env, check=True, capture_output=True, timeout=120)
            config = target.config_home / "nvim"
            config.mkdir(parents=True)
            (config / "init.lua").write_text(r'''
local repo = vim.env.DOTFILES_TEST_ROOT
vim.opt.rtp:prepend(repo .. '/nvim')
vim.env.VIMPLUG = vim.env.XDG_DATA_HOME .. '/vim/plugged'
vim.opt.rtp:prepend(vim.env.VIMPLUG .. '/lazy.nvim')
require('lazy').setup({ { import = 'plugins.copilot' } }, {
  root = vim.env.VIMPLUG,
  lockfile = vim.env.XDG_DATA_HOME .. '/vim/lazy-lock.json',
  defaults = { lazy = false },
  checker = { enabled = false },
})
''')
            target.state_root.mkdir(parents=True)
            choices = target.state_root / "choices.json"

            def launch(*commands):
                argv = [nvim, "--headless", "-i", "NONE"]
                for command in commands:
                    argv += ["-c", command]
                argv += ["-c", "qall"]
                result = subprocess.run(argv, env=env, capture_output=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

            choices.write_text(json.dumps({"copilot": False}))
            launch("lua assert(vim.fn.exists(':Copilot') == 0)")
            plugin = target.data_home / "vim/plugged/copilot.vim"
            self.assertFalse(plugin.exists(), "declining must not download the plugin")

            choices.write_text(json.dumps({"copilot": True}))
            launch("lua require('lazy').install { wait = true }")
            self.assertTrue((plugin / "plugin/copilot.vim").is_file())
            launch(
                "lua assert(vim.fn.exists(':Copilot') == 2)",
                "lua require('config.keymap')",
                "lua assert(vim.fn.maparg('<M-y>', 'i'):find('copilot#Accept', 1, true))",
                "lua local before = vim.api.nvim_get_commands({}).Copilot.definition; "
                "require('config.lsp')._setup_inline_completion(); "
                "assert(vim.api.nvim_get_commands({}).Copilot.definition == before)",
            )
            result = phases.copilot_auth_phase(target, Runner(), env)
            self.assertEqual(result.status, phases.AUTH_REQUIRED, result.to_dict())
            self.assertEqual(result.details, {"copilot": "not-signed-in"})

            choices.write_text(json.dumps({"copilot": False}))
            launch("lua assert(vim.fn.exists(':Copilot') == 0)")
            self.assertTrue(plugin.exists(), "declining later must not delete an installed plugin")


if __name__ == "__main__":
    unittest.main()
