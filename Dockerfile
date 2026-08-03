FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /app

# Copy package metadata and source
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package (no dev deps needed for runtime)
RUN pip install --no-cache-dir .

# Ownership proof for the MCP registry (must match server.json name)
LABEL io.modelcontextprotocol.server.name="io.github.partymola/monzo-mcp"

# MCP server uses stdio transport - no port to expose
ENTRYPOINT ["monzo-mcp"]
