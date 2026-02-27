# Leviathan DevOps — OpenFang v0.1.3 (fresh vanilla deploy)
# Phase 1: Get Discord working first, customize later
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl libssl3 libsqlite3-0 && rm -rf /var/lib/apt/lists/*

# Download OpenFang v0.1.3 release binary (latest — released 2026-02-26)
RUN curl -fsSL \
  "https://github.com/RightNow-AI/openfang/releases/download/v0.1.3/openfang-x86_64-unknown-linux-gnu.tar.gz" \
  -o /tmp/openfang.tar.gz \
  && tar -xzf /tmp/openfang.tar.gz -C /usr/local/bin/ \
  && chmod +x /usr/local/bin/openfang \
  && rm /tmp/openfang.tar.gz

# Initialize OpenFang directory structure (creates ~/.openfang/ with defaults)
RUN openfang init --quick

# Minimal config — ONLY what's needed for Discord + DeepSeek
# Port is injected at runtime from Railway's $PORT env var
RUN cat > /root/.openfang/config.toml.template << 'TOML'
api_listen = "0.0.0.0:PORT_PLACEHOLDER"
log_level = "debug"

[default_model]
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[channels.discord]
bot_token_env = "DISCORD_BOT_TOKEN"
default_agent = "assistant"
intents = 37377
TOML

ENV RUST_BACKTRACE=1
EXPOSE 4200

CMD ["/bin/sh", "-c", "PORT_VAL=${PORT:-4200} && sed \"s/PORT_PLACEHOLDER/$PORT_VAL/\" /root/.openfang/config.toml.template > /root/.openfang/config.toml && openfang start"]
