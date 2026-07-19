# Foundry Hosted Agent image for the chkpmcpaz security-operations agent.
# Build context is the REPO ROOT (the CLI runs: az acr build --file agent/Dockerfile .)
# linux/amd64 required by Foundry Hosted Agents; base via ECR public mirror
# (not docker.io) to dodge Docker Hub anonymous-pull 429s from build fleets.
FROM public.ecr.aws/docker/library/python:3.12-slim
# Node 20 for the @chkp stdio child processes (npx); the packages themselves
# stay version-pinned at spawn time (npx -y @chkp/<server>-mcp@<pin>).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
ENV TELEMETRY_DISABLED=true
WORKDIR /app
COPY agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY chkpmcpaz /app/chkpmcpaz
COPY agent/main.py /app/main.py
# THE FIX (confirmed from container logs): the Foundry sandbox runs the agent as
# uid 10001 with HOME=/home/session -- a persistent mount it does NOT own -- so
# npm/npx cannot mkdir the default $HOME/.npm cache and every @chkp server died
# with EACCES (network egress itself is fine). Redirect npm's cache to a writable
# system dir owned by uid 10001. Also pre-install the pinned servers globally at
# build time (bonus: prefer-offline => instant cold start, no per-session
# download). Best-effort per package; list from config.SERVERS (single source).
ENV npm_config_cache=/opt/npm-cache \
    npm_config_prefer_offline=true
RUN mkdir -p /opt/npm-cache \
 && python -c "from chkpmcpaz import config; print(chr(10).join(s.pinned for s in config.SERVERS.values()))" > /tmp/chkp-pkgs.txt \
 && while read -r p; do [ -n "$p" ] && (npm install -g "$p" || echo "WARN: @chkp preinstall failed for $p"); done < /tmp/chkp-pkgs.txt \
 && (chmod -R a+rX "$(npm root -g)" 2>/dev/null || true) \
 && chown -R 10001:10001 /opt/npm-cache
# Org policy: run as a non-root user. HOME=/home/agent is the image default; the
# sandbox overrides it to /home/session at runtime, but npm_config_cache above
# keeps npx writing to the writable /opt/npm-cache either way.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app
USER agent
ENV HOME=/home/agent
EXPOSE 8088
CMD ["python", "main.py"]
