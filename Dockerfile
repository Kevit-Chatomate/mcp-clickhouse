FROM python:3.13-slim-bookworm

WORKDIR /app

# Install build dependencies for ClickHouse client
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the MCP ClickHouse server by default
CMD ["python", "-m", "mcp_clickhouse.main"]
