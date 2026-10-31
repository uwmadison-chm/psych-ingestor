# Development Guidelines

Just a few quick notes from Nate:

We're using `uv` to manage this. For development work, assume an active venv, so CLI tools should be available in $PATH.

Format with `ruff`

Typecheck with `ty`

Test with `pytest` and `pytest-parallel`

Property tests, with Hypothesis and (for the client) fast-check, are in `fuzz/` and `client/fuzz/`. `just fuzz` runs both. CI doesn't, because they take a minute; run them before a release.

For common tasks (linting / reformatting / typechecking / testing) use `just` with commands like `just test`. The default should be everything you'd do before committing.

Use pydantic for dataclasses & validation. Let's try cyclopts for CLI tooling.
