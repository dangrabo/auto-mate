"""Command-line entrypoint for auto-mate.

For Phase 0 this only proves the wiring works: `uv run automate` should call
`main()` below. Later phases will make this start the poller and the worker.
"""


def main() -> None:
    """Entrypoint referenced by ``[project.scripts]`` in pyproject.toml."""
    print("auto-mate alive")
