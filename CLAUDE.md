@common/CLAUDE.md

# MAST_spec — Claude Guidance

Spectrograph control backend. Runs on `mast-wis-spec`. Submodules `MAST_common` as `./common/`.

## Running

```bash
python app.py   # role + identity come from the bootstrap config file
                # (/etc/wis/config.toml; set MAST_CONFIG to override for dev)
```

## Project-wide LLM guidance

Cross-repo LLM guidance for MAST lives in the **`mast-claude-config`** repo (`github.com/The-MAST-project/mast-claude-config`) — the overarching home for project-wide instructions (shared coding standards, team working-style, global environment facts), deployed into `~/.claude/` by its `setup.sh`. Keep repo-specific guidance in this file; put genuinely cross-repo guidance there. See `mast-claude-config/CLAUDE.md` for what belongs where.
