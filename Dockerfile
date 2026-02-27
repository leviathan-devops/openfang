# Leviathan DevOps — OpenFang v0.1.3
# Phase 4: Dual-agent architecture — CTO (Opus) + Neural Net (Sonnet)
# CTO = conscious reasoning, full autonomy, primary interface
# Neural Net = subconscious, server-hardwired background process, monitoring + assist
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl libssl3 libsqlite3-0 && rm -rf /var/lib/apt/lists/*

# Download OpenFang v0.1.3 release binary (latest — released 2026-02-26)
RUN curl -fsSL \
  "https://github.com/RightNow-AI/openfang/releases/download/v0.1.3/openfang-x86_64-unknown-linux-gnu.tar.gz" \
  -o /tmp/openfang.tar.gz \
  && tar -xzf /tmp/openfang.tar.gz -C /usr/local/bin/ \
  && chmod +x /usr/local/bin/openfang \
  && rm /tmp/openfang.tar.gz

# Initialize OpenFang directory structure
RUN openfang init --quick

# Copy agent manifests — CTO (primary) + Neural Net (subconscious)
COPY agents/leviathan/agent.toml /root/.openfang/agents/leviathan/agent.toml
COPY agents/neural-net/agent.toml /root/.openfang/agents/neural-net/agent.toml

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
TOML

ENV RUST_BACKTRACE=1
EXPOSE 4200

CMD ["/bin/sh", "-c", "PORT_VAL=${PORT:-4200} && sed \"s/PORT_PLACEHOLDER/$PORT_VAL/\" /root/.openfang/config.toml.template > /root/.openfang/config.toml && openfang start"]
