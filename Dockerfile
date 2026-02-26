# Leviathan DevOps — OpenFang deployment
# Uses pre-compiled release binary — fast deploys, no 15-min Rust compile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl libssl3 libsqlite3-0 && rm -rf /var/lib/apt/lists/*

# Download OpenFang v0.1.1 release binary (x86_64 Linux)
RUN curl -fsSL \
  "https://github.com/RightNow-AI/openfang/releases/download/v0.1.1/openfang-x86_64-unknown-linux-gnu.tar.gz" \
  -o /tmp/openfang.tar.gz \
  && tar -xzf /tmp/openfang.tar.gz -C /usr/local/bin/ \
  && chmod +x /usr/local/bin/openfang \
  && rm /tmp/openfang.tar.gz

# Config template — PORT injected at startup from Railway's $PORT env var
RUN mkdir -p /root/.openfang && cat > /root/.openfang/config.toml.template << 'TOML'
api_listen = "0.0.0.0:PORT_PLACEHOLDER"
usage_footer = "full"

[default_model]
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[memory]
decay_rate = 0.05

[compaction]
threshold = 80
keep_recent = 20
max_summary_tokens = 1024

[channels.discord]
bot_token_env = "DISCORD_BOT_TOKEN"
allowed_guilds = [1475947548811202613]
default_agent = "ops"
TOML

ENV RUST_BACKTRACE=1
EXPOSE 4200

# Write config with real PORT at startup, init (skips if config exists), then start
CMD ["/bin/sh", "-c", "PORT_VAL=${PORT:-4200} && sed \"s/PORT_PLACEHOLDER/$PORT_VAL/\" /root/.openfang/config.toml.template > /root/.openfang/config.toml && openfang init --quick && openfang start"]

