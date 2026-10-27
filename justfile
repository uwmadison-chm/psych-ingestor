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
    cp -n pigsty.example.toml local/pigsty.toml
    pig check
    pigsty check

# The server and the client always share a version number; the client's tests check.
# Set the version of both, e.g. `just version 0.3.0`.
version VERSION:
    sed -i.bak 's/^version = ".*"/version = "{{VERSION}}"/' pyproject.toml && rm pyproject.toml.bak
    uv lock
    cd client && npm version --no-git-tag-version --allow-same-version {{VERSION}}
    sed -i.bak 's/CLIENT_VERSION = ".*"/CLIENT_VERSION = "{{VERSION}}"/' client/src/version.ts && rm client/src/version.ts.bak

# Tag the version in pyproject.toml and push the tag, which releases it: the server to
# PyPI (see .github/workflows/server.yml), the client to npm (client.yml). Only from a
# clean main that matches GitHub's, and only when all three version numbers agree.
release:
    #!/usr/bin/env bash
    set -euo pipefail
    git fetch --quiet origin main
    if [ "$(git branch --show-current)" != "main" ]; then echo "Not on main."; exit 1; fi
    if [ -n "$(git status --porcelain)" ]; then echo "There are uncommitted changes."; exit 1; fi
    if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
        echo "This main isn't the same as GitHub's. Pull or push first."; exit 1
    fi
    server=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
    client=$(node -p 'require("./client/package.json").version')
    recorded=$(sed -n 's/.*CLIENT_VERSION = "\(.*\)".*/\1/p' client/src/version.ts)
    if [ "$server" != "$client" ] || [ "$server" != "$recorded" ]; then
        echo "The versions disagree: pyproject.toml $server, package.json $client, version.ts $recorded."
        echo "Set them with \`just version\`."; exit 1
    fi
    if git rev-parse --quiet --verify "refs/tags/v$server" >/dev/null || git ls-remote --exit-code --tags origin "v$server" >/dev/null; then
        echo "v$server is already tagged."; exit 1
    fi
    git tag "v$server"
    git push origin "v$server"
    echo "Pushed v$server. GitHub Actions publishes it to PyPI and npm."
