_:
    just --list

# Build the specification in the _build/ directory.
build:
    uv run python pre_build.py
    uv run jupyter book build --html --ci

# Check spelling in the source files;
# configured in pyproject.toml.
spell:
    uv run codespell

# Run all lint commands.
lint: spell pre-commit

# Run schema tests.
test:
    uv run pytest -v

# Install pre-commit hooks to lint changed files.
pre-commit-install:
    uv run prek install

# Run pre-commit lints on all files.
pre-commit:
    uv run prek run --all-files
