# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import difflib
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

import typer

from dimos.core.coordination.blueprints import Blueprint
from dimos.robot.all_blueprints import all_blueprints, all_modules

PATCHBAY_DIR = Path.home() / ".dimos" / "blueprints"

# Patchbay-installed blueprints and modules discovered at import time.
_patchbay_blueprints: dict[str, dict] = {}
_patchbay_modules: dict[str, dict] = {}


def _discover_patchbay_packages() -> None:
    """Scan ~/.dimos/blueprints/*/patchbay.json and register installed packages."""
    if not PATCHBAY_DIR.is_dir():
        return
    for pkg_dir in sorted(PATCHBAY_DIR.iterdir()):
        manifest_path = pkg_dir / "patchbay.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        name = manifest.get("name", pkg_dir.name)
        modules = manifest.get("modules", [])

        if not modules:
            continue

        # Make the package directory importable
        pkg_str = str(pkg_dir)
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)

        # Also add a modules/ subdirectory if it exists
        modules_dir = pkg_dir / "modules"
        if modules_dir.is_dir():
            modules_str = str(modules_dir)
            if modules_str not in sys.path:
                sys.path.insert(0, modules_str)

        for mod in modules:
            entry = mod.get("entry", "")
            mod_name = mod.get("name", "")
            if not entry or not mod_name:
                continue

            # entry format: "modules/dog_mode.py:DogModeModule"
            file_part, _, class_name = entry.rpartition(":")
            if not class_name:
                continue

            # Convert module name to kebab-case registry key
            registry_key = class_name_to_registry_key(class_name)
            _patchbay_modules[registry_key] = {
                "pkg_dir": pkg_dir,
                "file": file_part,
                "class_name": class_name,
                "manifest": manifest,
            }

        # Register the package name itself as a composite blueprint
        _patchbay_blueprints[name] = {
            "pkg_dir": pkg_dir,
            "modules": modules,
            "manifest": manifest,
        }


_discover_patchbay_packages()

all_names = sorted(
    set(all_blueprints.keys())
    | set(all_modules.keys())
    | set(_patchbay_blueprints.keys())
    | set(_patchbay_modules.keys())
)


def class_name_to_registry_key(class_name: str) -> str:
    """Convert a CamelCase class name to its kebab-case registry key."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower().replace("_", "-")


def _raise_unknown(name: str, candidates: list[str]) -> NoReturn:
    msg = f"Unknown blueprint or module: {name!r}"
    suggestions = difflib.get_close_matches(name, candidates, n=5, cutoff=0.4)
    if suggestions:
        msg += f". Did you mean: {', '.join(suggestions)}?"
    raise ValueError(msg)


def _load_patchbay_module_class(info: dict) -> type:
    """Import a Module class from a patchbay package on disk."""
    pkg_dir = info["pkg_dir"]
    file_part = info["file"]
    class_name = info["class_name"]

    # Resolve file path: "modules/dog_mode.py" → full path
    file_path = pkg_dir / file_part
    if not file_path.exists():
        # Try without the directory prefix (flat layout)
        file_path = pkg_dir / Path(file_part).name
    if not file_path.exists():
        raise ImportError(f"Cannot find {file_part} in {pkg_dir}")

    module_name = file_path.stem
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {file_path}")
    python_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = python_module
    spec.loader.exec_module(python_module)
    return getattr(python_module, class_name)


def _load_patchbay_blueprint(info: dict) -> Blueprint:
    """Load all modules from a patchbay package and compose via autoconnect."""
    from dimos.core.coordination.blueprints import autoconnect

    atoms = []
    for mod in info["modules"]:
        entry = mod.get("entry", "")
        _, _, class_name = entry.rpartition(":")
        if not class_name:
            continue
        key = class_name_to_registry_key(class_name)
        if key in _patchbay_modules:
            cls = _load_patchbay_module_class(_patchbay_modules[key])
            atoms.append(cls.blueprint())

    if not atoms:
        raise ValueError(f"No loadable modules in patchbay package: {info['manifest'].get('name')}")

    # If the package declares a base blueprint, compose on top of it
    base_name = info["manifest"].get("base", "")
    if base_name and base_name in all_blueprints:
        base = get_blueprint_by_name(base_name)
        return autoconnect(base, *atoms)

    return autoconnect(*atoms)


def get_blueprint_by_name(name: str) -> Blueprint:
    if name not in all_blueprints:
        _raise_unknown(name, list(all_blueprints.keys()))
    module_path, attr = all_blueprints[name].split(":")
    module = __import__(module_path, fromlist=[attr])
    return getattr(module, attr)  # type: ignore[no-any-return]


def get_module_by_name(name: str) -> Blueprint:
    if name not in all_modules:
        _raise_unknown(name, list(all_modules.keys()))
    module_path, class_name = all_modules[name].rsplit(".", 1)
    python_module = __import__(module_path, fromlist=[class_name])
    return getattr(python_module, class_name).blueprint()  # type: ignore[no-any-return]


def get_by_name(name: str) -> Blueprint:
    # Built-in blueprints first
    if name in all_blueprints:
        return get_blueprint_by_name(name)
    # Built-in modules
    if name in all_modules:
        return get_module_by_name(name)
    # Patchbay-installed blueprints (composite)
    if name in _patchbay_blueprints:
        return _load_patchbay_blueprint(_patchbay_blueprints[name])
    # Patchbay-installed individual modules
    if name in _patchbay_modules:
        cls = _load_patchbay_module_class(_patchbay_modules[name])
        return cls.blueprint()
    _raise_unknown(name, all_names)


def _fail_or_exit(name: str, candidates: list[str]) -> NoReturn:
    typer.echo(typer.style(f"Unknown blueprint or module: {name}", fg=typer.colors.RED), err=True)
    suggestions = difflib.get_close_matches(name, candidates, n=5, cutoff=0.4)
    if suggestions:
        typer.echo("Did you mean one of these?", err=True)
        for s in suggestions:
            typer.echo(f"  {s}", err=True)
    sys.exit(1)


def get_by_name_or_exit(name: str) -> Blueprint:
    if name in all_blueprints:
        return get_blueprint_by_name(name)
    if name in all_modules:
        return get_module_by_name(name)
    if name in _patchbay_blueprints:
        return _load_patchbay_blueprint(_patchbay_blueprints[name])
    if name in _patchbay_modules:
        cls = _load_patchbay_module_class(_patchbay_modules[name])
        return cls.blueprint()
    _fail_or_exit(name, all_names)


def get_module_by_name_or_exit(name: str) -> Blueprint:
    if name in all_modules:
        return get_module_by_name(name)
    if name in _patchbay_modules:
        cls = _load_patchbay_module_class(_patchbay_modules[name])
        return cls.blueprint()
    _fail_or_exit(name, list(all_modules.keys()) + list(_patchbay_modules.keys()))
