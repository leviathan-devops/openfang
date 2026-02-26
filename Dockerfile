# Leviathan DevOps — OpenFang deployment
# Uses pre-compiled release binary — fast deploys, no 15-min Rust compile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Download OpenFang v0.1.1 release binary (x86_64 Linux)
RUN curl -fsSL \
  "https://github.com/RightNow-AI/openfang/releases/download/v0.1.1/openfang-x86_64-unknown-linux-gnu.tar.gz" \
  -o /tmp/openfang.tar.gz \
  && tar -xzf /tmp/openfang.tar.gz -C /usr/local/bin/ \
  && chmod +x /usr/local/bin/openfang \
  && rm /tmp/openfang.tar.gz

# Bake in Leviathan config
# CRITICAL: api_listen MUST be at root level, before any [section] header
RUN mkdir -p /root/.openfang && cat > /root/.openfang/config.toml << 'EOF'
api_listen = "0.0.0.0:4200"
usage_footer = "Full"

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
guild_ids = ["1475947548811202613"]

[channels.discord.overrides]
dm_policy = "respond"
group_policy = "respond"
EOF

ENV OPENFANG_HOME=/data
ENV RUST_BACKTRACE=1
EXPOSE 4200

CMD ["/bin/sh", "-c", "openfang init --quick && openfang start"]
