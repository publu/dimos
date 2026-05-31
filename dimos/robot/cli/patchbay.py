"""Patchbay CLI — publish, install, and search robot blueprints.

Usage:
    dimos patchbay install <name>
    dimos patchbay publish <path>
    dimos patchbay search <query>
    dimos patchbay info <name>
    dimos patchbay list
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    help="Browse, install, and publish robot blueprints from the Patchbay registry.",
    no_args_is_help=True,
)

REGISTRY_URL = os.environ.get("PATCHBAY_REGISTRY", "https://patchbay.dev")
INSTALL_DIR = Path.home() / ".dimos" / "blueprints"


def _api(path: str, **kwargs) -> dict:
    import requests
    url = f"{REGISTRY_URL}/api{path}"
    resp = requests.request(**{"method": "GET", "url": url, **kwargs}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _echo_blueprint(bp: dict, verbose: bool = False) -> None:
    icon = bp.get("icon", "⬡")
    name = bp.get("name", "?")
    version = bp.get("version", "?")
    desc = bp.get("description", "")
    downloads = bp.get("downloads", 0)
    robots = bp.get("robots", [])
    tags = bp.get("tags", [])

    robot_str = ", ".join(robots) if robots else "any robot"
    typer.echo(f"  {icon}  {name} v{version}")
    typer.echo(f"     {desc}")
    typer.echo(f"     robots: {robot_str}  |  ↓ {downloads}  |  tags: {', '.join(tags)}")

    if verbose:
        skills = bp.get("skills", [])
        if skills:
            typer.echo(f"     skills: {', '.join(s['name'] for s in skills)}")
        deps = bp.get("dependencies", {})
        if deps.get("dimos"):
            typer.echo(f"     requires: dimos {deps['dimos']}")
        if deps.get("blueprints"):
            typer.echo(f"     depends on: {', '.join(deps['blueprints'])}")
        streams = bp.get("streams", {})
        requires = streams.get("requires", [])
        provides = streams.get("provides", [])
        if requires:
            typer.echo(f"     inputs:  {', '.join(f'{s['name']}:{s['type']}' for s in requires)}")
        if provides:
            typer.echo(f"     outputs: {', '.join(f'{s['name']}:{s['type']}' for s in provides)}")

    typer.echo()


@app.command()
def install(
    name: str = typer.Argument(..., help="Blueprint name to install, e.g. 'dog-mode'"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing installation"),
) -> None:
    """Install a blueprint from the Patchbay registry."""
    import requests

    typer.echo(f"⬡ Fetching {name} from {REGISTRY_URL}...")

    try:
        data = _api(f"/blueprints/{name}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            typer.echo(f"  Blueprint '{name}' not found in registry.", err=True)
            raise typer.Exit(1)
        raise

    bp = data["blueprint"]
    version = bp["version"]
    target_dir = INSTALL_DIR / name

    if target_dir.exists() and not force:
        existing_manifest = target_dir / "patchbay.json"
        if existing_manifest.exists():
            existing = json.loads(existing_manifest.read_text())
            typer.echo(f"  Already installed: {name} v{existing.get('version', '?')}")
            typer.echo(f"  Use --force to overwrite.")
            raise typer.Exit(1)

    # Check blueprint dependencies
    bp_deps = bp.get("dependencies", {}).get("blueprints", [])
    for dep in bp_deps:
        dep_dir = INSTALL_DIR / dep
        if not dep_dir.exists():
            typer.echo(f"  Dependency '{dep}' not installed. Installing...")
            install(dep, force=False)

    # Download package
    typer.echo(f"  Downloading {name}@{version}...")
    download_url = f"{REGISTRY_URL}/api/blueprints/{name}/download"
    resp = requests.get(download_url, timeout=60)
    resp.raise_for_status()

    # Extract
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz") as tar:
        tar.extractall(target_dir)

    # Install pip dependencies
    pip_deps = bp.get("dependencies", {}).get("pip", [])
    if pip_deps:
        typer.echo(f"  Installing pip dependencies: {', '.join(pip_deps)}")
        import subprocess
        subprocess.run(
            ["pip", "install", *pip_deps],
            capture_output=True,
        )

    typer.echo(f"  {bp.get('icon', '⬡')} Installed {name}@{version}")
    typer.echo(f"  Location: {target_dir}")

    skills = bp.get("skills", [])
    if skills:
        typer.echo(f"  Skills: {', '.join(s['name'] for s in skills)}")

    typer.echo()
    typer.echo(f"  Ready. Add to your blueprint or run:")
    typer.echo(f"    dimos run {bp.get('base', name)}")


@app.command()
def publish(
    path: str = typer.Argument(..., help="Path to blueprint directory or .tar.gz package"),
) -> None:
    """Publish a blueprint to the Patchbay registry."""
    import requests

    source = Path(path)

    if source.is_dir():
        manifest_path = source / "patchbay.json"
        if not manifest_path.exists():
            typer.echo("  No patchbay.json found in directory.", err=True)
            raise typer.Exit(1)

        manifest = json.loads(manifest_path.read_text())
        typer.echo(f"  Packaging {manifest.get('name', '?')}@{manifest.get('version', '?')}...")

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        with tarfile.open(tmp_path, "w:gz") as tar:
            for item in source.rglob("*"):
                if item.is_file() and not item.name.startswith("."):
                    arcname = str(item.relative_to(source))
                    tar.add(str(item), arcname=arcname)

        package_path = Path(tmp_path)
    elif source.suffix == ".gz" or source.name.endswith(".tar.gz"):
        package_path = source
    else:
        typer.echo("  Path must be a directory with patchbay.json or a .tar.gz package.", err=True)
        raise typer.Exit(1)

    typer.echo(f"  Publishing to {REGISTRY_URL}...")

    with open(package_path, "rb") as f:
        resp = requests.post(
            f"{REGISTRY_URL}/api/publish",
            files={"package": (package_path.name, f, "application/gzip")},
            timeout=60,
        )

    if source.is_dir():
        Path(tmp_path).unlink(missing_ok=True)

    if resp.status_code == 200:
        data = resp.json()
        typer.echo(f"  ✓ Published {data['name']}@{data['version']}")
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"  ✗ Publish failed: {detail}", err=True)
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument("", help="Search query"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Filter by tag"),
    robot: Optional[str] = typer.Option(None, "--robot", "-r", help="Filter by robot type"),
) -> None:
    """Search the Patchbay registry for blueprints."""
    params = {}
    if query:
        params["q"] = query
    if tag:
        params["tag"] = tag
    if robot:
        params["robot"] = robot

    query_str = "&".join(f"{k}={v}" for k, v in params.items())
    path = f"/blueprints?{query_str}" if query_str else "/blueprints"

    data = _api(path)
    blueprints = data.get("blueprints", [])

    if not blueprints:
        typer.echo("  No blueprints found.")
        return

    typer.echo(f"  {len(blueprints)} blueprint(s) found:\n")
    for bp in blueprints:
        _echo_blueprint(bp)


@app.command()
def info(
    name: str = typer.Argument(..., help="Blueprint name"),
) -> None:
    """Show details for a specific blueprint."""
    import requests

    try:
        data = _api(f"/blueprints/{name}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            typer.echo(f"  Blueprint '{name}' not found.", err=True)
            raise typer.Exit(1)
        raise

    _echo_blueprint(data["blueprint"], verbose=True)


@app.command(name="list")
def list_installed() -> None:
    """List locally installed patchbay blueprints."""
    if not INSTALL_DIR.exists():
        typer.echo("  No blueprints installed yet.")
        typer.echo(f"  Install one: dimos patchbay install <name>")
        return

    installed = []
    for d in sorted(INSTALL_DIR.iterdir()):
        manifest = d / "patchbay.json"
        if manifest.exists():
            bp = json.loads(manifest.read_text())
            installed.append(bp)

    if not installed:
        typer.echo("  No blueprints installed.")
        return

    typer.echo(f"  {len(installed)} installed blueprint(s):\n")
    for bp in installed:
        icon = bp.get("icon", "⬡")
        typer.echo(f"  {icon}  {bp.get('name', '?')} v{bp.get('version', '?')}")
        typer.echo(f"     {INSTALL_DIR / bp.get('name', '?')}")
        typer.echo()


@app.command()
def uninstall(
    name: str = typer.Argument(..., help="Blueprint name to uninstall"),
) -> None:
    """Remove a locally installed blueprint."""
    target = INSTALL_DIR / name
    if not target.exists():
        typer.echo(f"  Blueprint '{name}' is not installed.", err=True)
        raise typer.Exit(1)

    shutil.rmtree(target)
    typer.echo(f"  Uninstalled {name}")
