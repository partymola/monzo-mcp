FROM python:3.13-slim

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
