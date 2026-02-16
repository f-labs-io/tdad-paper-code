FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gosu \
    patch \
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
COPY tdadlib /workspace/tdadlib/
COPY scripts /workspace/scripts/
COPY specs /workspace/specs/
COPY seeds /workspace/seeds/
COPY tests_visible /workspace/tests_visible/
COPY agent_artifacts /workspace/agent_artifacts/
COPY mutation_packs /workspace/mutation_packs/
COPY pytest.ini /workspace/

# Create results directory for output
RUN mkdir -p /workspace/results

# Fix ownership
RUN chown -R promptsmith:promptsmith /workspace

# Copy entrypoint last (changes frequently)
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "scripts/compile_prompt.py"]
