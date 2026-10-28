"""The spectrograph reads its configuration live, and never writes into it.

MAST_common delivers every edit made in the configuration database to the running process
through the change stream. A component that copies its configuration in `__init__` keeps
working -- it just quietly ignores every later change, until the service is restarted.
That was every component here until 2026-10-28: the shutter timings, the stage presets, the
filter names and each band's settings all needed a restart to take effect. MAST_unit had
the same sweep earlier (its tests/test_config_is_live.py, which this follows).

AST, not imports: the drivers bind Windows-only SDKs at import time and some command
hardware when imported (MAST_spec#77), so nothing here imports them. That also means this
runs on any machine.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent

#: The name a component's configuration is read through.
CONF_NAMES = ("conf",)

#: Component classes whose `conf` must be a property, and what its source must read.
LIVE_CONF = {
    ("calibration/lamp.py", "CalibrationLamp"): "get_specs().lamps",
    ("cooling/chiller.py", "Chiller"): "get_specs().chiller",
    ("deepspec.py", "Deepspec"): "get_specs().deepspec",
    ("highspec.py", "Highspec"): "get_specs().highspec",
    ("cameras/andor/newton.py", "NewtonEMCCD"): "get_specs().highspec.settings",
    ("cameras/qhy/qhy600.py", "QHY600"): "get_specs().highspec.settings",
    ("cameras/greateyes/greateyes.py", "GreatEyes"): "get_specs().deepspec[self.band]",
    ("stage/stage.py", "Stage"): "get_specs().stage",
    ("stage/stage.py", "StageController"): "get_specs().stage.controller",
    ("filter_wheel/wheel.py", "Wheel"): "get_specs().wheels[self._name]",
}

#: Values derived from the configuration that must be properties over `conf`. Each was an
#: `__init__` copy of one field.
LIVE_ATTRIBUTES = {
    ("cameras/greateyes/greateyes.py", "GreatEyes", "settings"),
    ("cameras/greateyes/greateyes.py", "GreatEyes", "enabled"),
    ("cameras/andor/newton.py", "NewtonEMCCD", "enabled"),
    ("stage/stage.py", "Stage", "max_position"),
    ("stage/stage.py", "Stage", "presets"),
    ("stage/stage.py", "Stage", "startup_preset"),
    ("stage/stage.py", "Stage", "shutdown_preset"),
    ("filter_wheel/wheel.py", "Wheel", "filters"),
    ("filter_wheel/wheel.py", "Wheel", "default_position"),
}

#: `self.<attribute> = <configuration>` sites that are right as they stand: each names the
#: device the component opened, so a new value means a new device, which takes a restart
#: however the code is written. A property would advertise a liveness it cannot deliver.
#: Keyed on the attribute, not the line. Every entry must match a site.
CONSTRUCTION_TIME = {
    ("cameras/greateyes/greateyes.py", "ge_device"): "the SDK's index for the camera the connection was opened with",
    ("filter_wheel/wheel.py", "serial_number"): "the serial number the device was found by",
    ("stage/stage.py", "peripheral"): "the Zaber model name the stage's axis was chosen by",
}

#: Sites the taint tracking flags that are not configuration at all.
NOT_CONFIGURATION = {
    ("cameras/andor/newton.py", "errors"): "a CanonicalResponse's errors, from a call the configuration was passed to",
}


def python_files():
    return [
        p
        for p in SRC.rglob("*.py")
        if not {"tests", "sdk", "another_demo", ".venv", "__pycache__"} & set(p.relative_to(SRC).parts)
    ]


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def attribute_chain(node: ast.AST) -> list[str]:
    """['self', 'conf', 'settings'] for `self.conf.settings`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def assignments(node: ast.AST) -> list[ast.Assign | ast.AnnAssign]:
    """Every assignment under `node`, in source order -- taint tracking needs the reader's."""
    found = [n for n in ast.walk(node) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def targets_of(node: ast.Assign | ast.AnnAssign) -> list[ast.AST]:
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    return [element for target in node.targets for element in (target.elts if isinstance(target, ast.Tuple) else [target])]


def reads_configuration(node: ast.AST, tainted: set[str]) -> bool:
    """`self.conf.x`, `Config().get_specs()`, or a local already bound to one of them."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in CONF_NAMES:
            return True
        if isinstance(n, ast.Name) and (n.id in CONF_NAMES or n.id in tainted):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Call)
            and isinstance(n.func.value.func, ast.Name)
            and n.func.value.func.id == "Config"
        ):
            return True
    return False


def configuration_derived_attributes(path: pathlib.Path) -> list[tuple[str, int]]:
    """`self.x = <configuration>` sites in one file, as (attribute, lineno). Per function,
    since a local's taint does not outlive the call that bound it."""
    found: list[tuple[str, int]] = []
    for function in [n for n in ast.walk(parse(path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        tainted: set[str] = set()
        for node in assignments(function):
            if not reads_configuration(node.value, tainted):
                continue
            for target in targets_of(node):
                chain = attribute_chain(target)
                if len(chain) == 2 and chain[0] == "self":
                    found.append((chain[1], node.lineno))
                elif len(chain) == 1:
                    tainted.add(chain[0])
    return found


def property_of(relpath: str, class_name: str, name: str) -> ast.FunctionDef | None:
    classes = [n for n in parse(SRC / relpath).body if isinstance(n, ast.ClassDef) and n.name == class_name]
    assert classes, f"{relpath}: no class {class_name}"
    for node in classes[0].body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == name
            and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
        ):
            return node
    return None


@pytest.mark.parametrize(("relpath", "class_name"), sorted(LIVE_CONF))
def test_conf_is_a_live_property(relpath, class_name):
    prop = property_of(relpath, class_name, "conf")
    expected = LIVE_CONF[(relpath, class_name)]
    assert prop is not None, f"{relpath}: {class_name}.conf must be a @property reading Config().{expected}"
    assert expected in ast.unparse(prop), f"{relpath}: {class_name}.conf should read Config().{expected}"


@pytest.mark.parametrize(("relpath", "class_name", "attribute"), sorted(LIVE_ATTRIBUTES))
def test_derived_value_is_a_live_property(relpath, class_name, attribute):
    prop = property_of(relpath, class_name, attribute)
    assert prop is not None, f"{relpath}: {class_name}.{attribute} must be a @property over self.conf"
    assert "self.conf" in ast.unparse(prop), f"{relpath}: {class_name}.{attribute} should derive from self.conf"


def test_nothing_copies_the_configuration():
    """No `self.<anything> = <read from the configuration>`, outside the listed exceptions."""
    allowed = set(CONSTRUCTION_TIME) | set(NOT_CONFIGURATION)
    offenders = [
        f"{relpath}:{lineno} -> self.{attribute} = <configuration>"
        for path in python_files()
        for relpath in [path.relative_to(SRC).as_posix()]
        for attribute, lineno in configuration_derived_attributes(path)
        if (relpath, attribute) not in allowed
    ]
    assert not offenders, (
        "read the configuration live, through a property over `conf` (or add a CONSTRUCTION_TIME "
        "entry, with its reason, if the value names the device and cannot change without a restart):\n  "
        + "\n  ".join(offenders)
    )


def test_every_exception_still_matches_a_site():
    """An allowlist nothing checks becomes a list of things that used to be true."""
    sites = {
        (path.relative_to(SRC).as_posix(), attribute)
        for path in python_files()
        for attribute, _ in configuration_derived_attributes(path)
    }
    unused = sorted((set(CONSTRUCTION_TIME) | set(NOT_CONFIGURATION)) - sites)
    assert not unused, f"exceptions that no longer match any site: {unused}"


def test_nothing_writes_into_the_configuration():
    """Accessors are memoized per generation: an edit to what `conf` returns would change it
    for every reader in the process, and be lost at the next generation."""
    offenders = [
        f"{path.relative_to(SRC)}:{node.lineno} -> {'.'.join(chain)} = ..."
        for path in python_files()
        for node in assignments(parse(path))
        for target in targets_of(node)
        for chain in [attribute_chain(target)]
        if "conf" in chain and chain[-1] != "conf"
    ]
    assert not offenders, "never write into the configuration:\n  " + "\n  ".join(offenders)
