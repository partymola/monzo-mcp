FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

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
