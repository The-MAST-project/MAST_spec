@common/CLAUDE.md

# MAST_spec — Claude Guidance

Spectrograph control backend. Runs on `mast-wis-spec`. Submodules `MAST_common` as `./common/`.

## Running

```bash
MAST_PROJECT=spec python app.py
```

## Project-wide LLM guidance

Cross-repo LLM guidance for MAST lives in the **`mast-claude-config`** repo (`github.com/The-MAST-project/mast-claude-config`) — the overarching home for project-wide instructions (shared coding standards, team working-style, global environment facts), deployed into `~/.claude/` by its `setup.sh`. Keep repo-specific guidance in this file; put genuinely cross-repo guidance there. See `mast-claude-config/CLAUDE.md` for what belongs where.
