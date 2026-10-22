FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    curl \
    git \
    socat \
    ca-certificates \
    libgl1 \
    libglib2.0-0 \
    libxcb1 \
    libx11-xcb1 \
    libxrender1 \
    libxi6 \
    && rm -rf /var/lib/apt/lists/*

# Install Tailscale
ENV TAILSCALE_VERSION=1.76.6
RUN curl -fsSL https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz | tar -xz \
    && mv tailscale_${TAILSCALE_VERSION}_amd64/tailscaled /usr/bin/tailscaled \
    && mv tailscale_${TAILSCALE_VERSION}_amd64/tailscale /usr/bin/tailscale \
    && rm -rf tailscale_${TAILSCALE_VERSION}_amd64

# Copy code
COPY packages/artemis-client/ ./packages/artemis-client/
COPY pyproject.toml setup.py README.md LICENSE ./
COPY apps/ ./apps/
COPY artemis/ ./artemis/
COPY config/ ./config/
COPY mcp_server/ ./mcp_server/
COPY third_party/ ./third_party/

# Install python dependencies
RUN pip install --no-cache-dir ./packages/artemis-client \
    && pip install --no-cache-dir .

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV ARTEMIS_APP_DIR=/app
ENV ARTEMIS_DISABLE_ORIGIN_GUARD=1
ENV PORT=10000

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
