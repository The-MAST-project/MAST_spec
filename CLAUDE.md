@../common/CLAUDE.md

# MAST_spec — Claude Guidance

Spectrograph control backend. Runs on `mast-ns-spec` (the active site is `ns`). Imports `MAST_common` as `common`, which is cloned as a **sibling** of this repo in the flat layout (`<top>/common/`, `<top>/spec/`) and put on `sys.path` by the `mast.pth` the provisioning writes into the venv. It is no longer a submodule.

## Running

```bash
python app.py   # role + identity come from the bootstrap config file
                # (/etc/wis/config.toml; set MAST_CONFIG to override for dev)
```

## Adding an endpoint

Routes are registered through `common.endpoints.add_api_route`, which refuses a handler that declares no tier — see `MAST_unit/docs/adding-an-endpoint.md` for the decorator and its arguments. Two things are specific to this repo:

- **A new area needs an entry in `OPERATOR_AREAS` in `app.py`.** The Swagger group an operator route files under is derived from the path it is mounted at (`common.endpoints.area_of`), but the group's description and its position on the page are not — an area with no entry renders last and undescribed. `MAST_unit` catches that with a test; this repo has no test job to hang one on, because importing it commands hardware (#77), so the list is kept in step by hand.
- **Give the route a verb.** A route registered at a component's bare base path (as `/fw` is) files under the *service's* area rather than the component's, since positionally it cannot be told from a layer-1 verb.

## Project-wide LLM guidance

Cross-repo LLM guidance for MAST lives in the **`mast-claude-config`** repo (`github.com/The-MAST-project/mast-claude-config`) — the overarching home for project-wide instructions (shared coding standards, team working-style, global environment facts), deployed into `~/.claude/` by its `setup.sh`. Keep repo-specific guidance in this file; put genuinely cross-repo guidance there. See `mast-claude-config/CLAUDE.md` for what belongs where.
