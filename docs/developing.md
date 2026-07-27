# Development Guidelines

Just a few quick notes from Nate:

We're using `uv` to manage this. For development work, assume an active venv, so CLI tools should be available in $PATH.

Format with `ruff`

Typecheck with `ty`

Test with `pytest` and `pytest-parallel`

For common tasks (linting / reformatting / typechecking / testing) use `just` with commands like `just test`. The default should be everything you'd do before committing.

Use pydantic for dataclasses & validation. Let's try cyclopts for CLI tooling.
