FROM python:3.11-slim

# Basic OS deps for common tooling.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with standard UID 1000
RUN useradd -m -s /bin/bash -u 1000 promptsmith

# Install Claude CLI as promptsmith user
USER promptsmith
WORKDIR /home/promptsmith
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/promptsmith/.local/bin:$PATH"

# Switch back to root for workspace setup
USER root
WORKDIR /workspace

# Copy only requirements first for better layer caching
COPY pyproject.toml README.md /workspace/

# Create minimal placeholder package for setuptools to find
RUN mkdir -p /workspace/tdadlib && echo "" > /workspace/tdadlib/__init__.py

# Install Python dependencies (cached via BuildKit mount)
RUN --mount=type=cache,target=/root/.cache/pip pip install -e .

# Now copy actual source code (this layer changes frequently but deps are cached)
COPY tdadlib /workspace/tdadlib
COPY scripts /workspace/scripts
COPY specs /workspace/specs
COPY seeds /workspace/seeds
COPY agent_artifacts /workspace/agent_artifacts
COPY tests_visible /workspace/tests_visible
COPY tests_hidden /workspace/tests_hidden
COPY mutation_packs /workspace/mutation_packs
COPY pytest.ini /workspace/
COPY pyproject.toml /workspace/

# Create results directory for output
RUN mkdir -p /workspace/results

# Fix ownership
RUN chown -R promptsmith:promptsmith /workspace

# Copy entrypoint (runs as promptsmith)
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# Default command runs visible tests (override in docker-compose).
CMD ["pytest", "-v", "--tb=short", "tests_visible", "-m", "visible"]
