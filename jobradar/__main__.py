"""`python -m jobradar ...` runs the CLI without the jobradar.exe shim, so a long-running
dashboard never locks the shim while `uv sync` reinstalls the package."""

from jobradar.cli import app

app()
