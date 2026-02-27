# Leviathan DevOps — OpenFang v0.1.3
# Phase 4: Triple-agent architecture — CTO + Neural Net + Prompt Architect
# CTO = conscious reasoning, full autonomy, primary interface
# Neural Net = subconscious, server-hardwired background process, executive layer
# Prompt Architect = DeepSeek R1 reasoning engine, meta-prompting refinement
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl libssl3 libsqlite3-0 python3 && rm -rf /var/lib/apt/lists/*

# Download OpenFang v0.1.3 release binary (latest — released 2026-02-26)
RUN curl -fsSL \
  "https://github.com/RightNow-AI/openfang/releases/download/v0.1.3/openfang-x86_64-unknown-linux-gnu.tar.gz" \
  -o /tmp/openfang.tar.gz \
  && tar -xzf /tmp/openfang.tar.gz -C /usr/local/bin/ \
  && chmod +x /usr/local/bin/openfang \
  && rm /tmp/openfang.tar.gz

# Initialize OpenFang directory structure
RUN openfang init --quick

# Copy agent manifests — CTO (primary) + Neural Net (executive) + Prompt Architect (meta-prompting)
COPY agents/leviathan/agent.toml /root/.openfang/agents/leviathan/agent.toml
COPY agents/neural-net/agent.toml /root/.openfang/agents/neural-net/agent.toml
COPY agents/prompt-architect/agent.toml /root/.openfang/agents/prompt-architect/agent.toml

# Full Leviathan config — DeepSeek V3 primary, OpenRouter + Groq fallbacks
# Port injected at runtime from Railway's $PORT env var
RUN cat > /root/.openfang/config.toml.template << 'TOML'
api_listen = "0.0.0.0:PORT_PLACEHOLDER"
api_key = "leviathan-test-key-2026"
log_level = "info"
usage_footer = "full"

[default_model]
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[[fallback_providers]]
provider = "openrouter"
model = "qwen/qwen3-32b"
api_key_env = "OPENROUTER_API_KEY"

[[fallback_providers]]
provider = "groq"
model = "llama-3.3-70b-versatile"
api_key_env = "GROQ_API_KEY"

[memory]
decay_rate = 0.05

[compaction]
threshold = 80
keep_recent = 20
max_summary_tokens = 1024

[channels.discord]
bot_token_env = "DISCORD_BOT_TOKEN"
default_agent = "leviathan"
intents = 37377

[channels.discord.overrides]
group_policy = "all"
dm_policy = "respond"

[channels.discord.overrides."1476978586828411073"]
agent = "prompt-architect"
TOML

ENV RUST_BACKTRACE=1
EXPOSE 4200

# Startup script: inject port, start OpenFang, then auto-spawn neural-net after boot
RUN cat > /root/start.sh << 'SCRIPT'
#!/bin/sh
PORT_VAL=${PORT:-4200}
sed "s/PORT_PLACEHOLDER/$PORT_VAL/" /root/.openfang/config.toml.template > /root/.openfang/config.toml

# Start OpenFang in background
openfang start &
OPENFANG_PID=$!

# Wait for API to be ready (max 30s)
for i in $(seq 1 30); do
  if curl -sf http://localhost:$PORT_VAL/api/health > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Auto-spawn neural-net agent from its manifest
NEURAL_NET_TOML=$(cat /root/.openfang/agents/neural-net/agent.toml)
curl -sf -X POST "http://localhost:$PORT_VAL/api/agents" \
  -H "Authorization: Bearer leviathan-test-key-2026" \
  -H "Content-Type: application/json" \
  -d "{\"manifest_toml\": $(echo "$NEURAL_NET_TOML" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
  > /dev/null 2>&1 && echo "Neural Net spawned" || echo "Neural Net spawn failed (will retry)"

# Auto-spawn prompt-architect agent from its manifest
PROMPT_ARCH_TOML=$(cat /root/.openfang/agents/prompt-architect/agent.toml)
curl -sf -X POST "http://localhost:$PORT_VAL/api/agents" \
  -H "Authorization: Bearer leviathan-test-key-2026" \
  -H "Content-Type: application/json" \
  -d "{\"manifest_toml\": $(echo "$PROMPT_ARCH_TOML" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
  > /dev/null 2>&1 && echo "Prompt Architect spawned" || echo "Prompt Architect spawn failed (will retry)"

# Foreground the main process
wait $OPENFANG_PID
SCRIPT
RUN chmod +x /root/start.sh

CMD ["/root/start.sh"]
