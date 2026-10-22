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

# The server and the client always share a version number; the client's tests check.
# Set the version of both, e.g. `just version 0.3.0`.
version VERSION:
    sed -i.bak 's/^version = ".*"/version = "{{VERSION}}"/' pyproject.toml && rm pyproject.toml.bak
    uv lock
    cd client && npm version --no-git-tag-version --allow-same-version {{VERSION}}
    sed -i.bak 's/CLIENT_VERSION = ".*"/CLIENT_VERSION = "{{VERSION}}"/' client/src/version.ts && rm client/src/version.ts.bak
