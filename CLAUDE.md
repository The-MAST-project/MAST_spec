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

## Read the configuration live

The configuration reaches the running service through MAST_common's change stream, so an
edit in the database takes effect without a restart -- **as long as nothing copied it**. A
component's `conf` is a property over `Config().get_specs()`, and so is every value derived
from it (`GreatEyes.settings`, `Stage.presets`, `Wheel.filters`, ...). Never write
`self.x = self.conf.y`: it captures one generation's value for the life of the process.
For a multi-step operation, bind `conf = self.conf` once at entry -- within a generation it
is the same object, so that is a stable view by design. Never assign into what `conf`
returns: it is shared with every reader, and lost at the next generation.

The exceptions name the device a component opened (a camera's SDK index, a wheel's serial
number, a stage's Zaber model), which takes a restart however the code is written. They are
listed, with their reasons, in `tests/test_config_is_live.py`, which enforces the rest at
the source level (AST, so it runs off the spectrograph machine). The same rule, and test,
exist in MAST_unit.

## Project-wide LLM guidance

Cross-repo LLM guidance for MAST lives in the **`mast-claude-config`** repo (`github.com/The-MAST-project/mast-claude-config`) — the overarching home for project-wide instructions (shared coding standards, team working-style, global environment facts), deployed into `~/.claude/` by its `setup.sh`. Keep repo-specific guidance in this file; put genuinely cross-repo guidance there. See `mast-claude-config/CLAUDE.md` for what belongs where.
