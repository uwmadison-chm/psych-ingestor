# Everything you'd do before committing.
default: format check test

format:
    ruff format src tests
    ruff check --fix src tests

# Lint and typecheck.
check:
    ruff check src tests
    ty check src

test:
    pytest -q

# Run the service against ./local/pig.toml.
serve:
    pig serve

# Set up a local deployment: configuration, database, and data, all under ./local.
local-setup:
    mkdir -p local
    cp -n pig.example.toml local/pig.toml
    pig check
