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
database_path = "/data/memory.db"
knowledge_path = "/data/knowledge/"

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

# Ensure persistent volume directories exist (Railway volume mounted at /data)
mkdir -p /data/knowledge /data/logs /data/backups /data/github-sync

# Symlink OpenFang's default data directory to persistent volume
ln -sf /data /root/.openfang/data 2>/dev/null || true

# SQLite WAL mode + integrity check on boot
if [ -f /data/memory.db ]; then
  # Enable WAL mode for concurrent read/write safety (CTO + Neural Net)
  python3 -c "
import sqlite3, sys, shutil, os
from datetime import datetime
db = '/data/memory.db'
try:
    conn = sqlite3.connect(db)
    # Enable WAL mode — allows concurrent readers + single writer without locks
    conn.execute('PRAGMA journal_mode=WAL;')
    # Quick integrity check
    result = conn.execute('PRAGMA integrity_check;').fetchone()[0]
    if result == 'ok':
        print(f'Memory DB healthy ({os.path.getsize(db)//1024}KB), WAL mode enabled')
        # Verified backup — only back up HEALTHY databases
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(db, f'/data/backups/memory_{ts}_verified.db')
    else:
        print(f'WARNING: Memory DB integrity check failed: {result}', file=sys.stderr)
        # Try to recover from last verified backup
        import glob
        backups = sorted(glob.glob('/data/backups/memory_*_verified.db'), reverse=True)
        if backups:
            print(f'Restoring from last verified backup: {backups[0]}')
            shutil.copy2(backups[0], db)
        else:
            print('CRITICAL: No verified backups available. Starting fresh.', file=sys.stderr)
            os.rename(db, f'/data/backups/memory_corrupted_{ts}.db')
    conn.close()
except Exception as e:
    print(f'Memory DB error: {e}', file=sys.stderr)
"
  # Keep last 20 verified backups (rotate old ones)
  ls -t /data/backups/memory_*_verified.db 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
else
  echo "No existing memory DB — fresh start on persistent volume"
  # Pre-initialize with WAL mode
  python3 -c "
import sqlite3
conn = sqlite3.connect('/data/memory.db')
conn.execute('PRAGMA journal_mode=WAL;')
conn.close()
print('Fresh memory DB created with WAL mode')
"
fi

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
