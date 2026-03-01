#!/usr/bin/env python3
"""
Leviathan Memory Manager v1.0 — Dynamic Memory Management Daemon
=================================================================
Implements the 3 HIGHEST PRIORITY paper-only systems as a Python companion
to the OpenFang kernel. Runs alongside openfang as a background daemon.

Systems Implemented:
  1. Dynamic Memory Management (DMM) — Per-agent quotas, priority retention,
     tier management (hot/warm/cold), intelligent garbage collection.
  2. Smart Context Caching — LRU cache for hot memories, precomputed context
     windows, tier-aware caching (T1 hot in-memory, T2 warm SQLite, T3 cold).
  3. Knowledge Harvesting — Automated entity extraction from conversations,
     knowledge graph population, 15-minute harvest cycles.

Architecture:
  - Connects directly to OpenFang's SQLite database (/data/memory.db)
  - Uses WAL mode for concurrent read/write with the kernel
  - Runs harvest/compaction/enforcement cycles on configurable intervals
  - Exposes a simple HTTP health endpoint for monitoring

Author: Leviathan DevOps (External Claude)
Date: 2026-03-01
"""

import sqlite3
import json
import os
import sys
import time
import re
import logging
import hashlib
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
DB_PATH = os.environ.get("MEMORY_DB_PATH", "/data/memory.db")
CYCLE_INTERVAL = int(os.environ.get("DMM_CYCLE_SECONDS", "900"))  # 15 min
HEALTH_PORT = int(os.environ.get("DMM_HEALTH_PORT", "4201"))
LOG_LEVEL = os.environ.get("DMM_LOG_LEVEL", "INFO")

# DMM Configuration
DEFAULT_QUOTA_PER_AGENT = 10000       # max memories per agent
MAX_HOT_MEMORIES = 100                # max T1 (hot) memories per agent
MAX_WARM_MEMORIES = 5000              # max T2 (warm) memories per agent
COLD_PRUNE_DAYS = 30                  # prune T3 (cold) memories older than this
DECAY_RATE = 0.05                     # confidence decay per cycle
ACCESS_PROMOTE_THRESHOLD = 5          # access_count to promote warm→hot
ACCESS_DEMOTE_THRESHOLD_DAYS = 7      # days without access to demote hot→warm
COLD_CONFIDENCE_THRESHOLD = 0.2       # below this confidence → cold tier

# Context Cache Configuration
CACHE_SIZE_PER_AGENT = 50             # LRU cache entries per agent
CACHE_TTL_SECONDS = 3600              # 1 hour cache TTL

# Knowledge Harvesting Configuration
HARVEST_BATCH_SIZE = 50               # messages to process per harvest cycle
MIN_ENTITY_LENGTH = 2                 # minimum entity name length
MAX_ENTITY_LENGTH = 100               # maximum entity name length

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [DMM] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("memory_manager")


# ══════════════════════════════════════════════
# 1. DYNAMIC MEMORY MANAGEMENT (DMM)
# ══════════════════════════════════════════════

class DynamicMemoryManager:
    """
    Per-agent memory quotas, tier management, priority-based retention.

    Tiers:
      T1 (hot)  — Frequently accessed, high confidence, in-memory cache candidate
      T2 (warm) — Moderate access, default tier for new memories
      T3 (cold) — Rarely accessed, low confidence, pruning candidate
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def ensure_schema(self):
        """Add DMM columns if they don't exist (safe migration)."""
        conn = self._connect()
        try:
            # Check if 'tier' column exists on memories table
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = {row[1] for row in cursor.fetchall()}

            if "tier" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'warm'")
                log.info("Added 'tier' column to memories table")

            if "priority" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
                log.info("Added 'priority' column to memories table")

            # Create DMM tracking tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_quotas (
                    agent_id TEXT PRIMARY KEY,
                    max_memories INTEGER NOT NULL DEFAULT 10000,
                    max_hot INTEGER NOT NULL DEFAULT 100,
                    current_count INTEGER NOT NULL DEFAULT 0,
                    hot_count INTEGER NOT NULL DEFAULT 0,
                    warm_count INTEGER NOT NULL DEFAULT 0,
                    cold_count INTEGER NOT NULL DEFAULT 0,
                    last_enforced_at TEXT NOT NULL DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS dmm_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_at TEXT NOT NULL,
                    agent_id TEXT,
                    action TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    details TEXT
                )
            """)

            # Create indexes for tier queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_tier
                ON memories(agent_id, tier, confidence)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_access
                ON memories(agent_id, accessed_at, access_count)
            """)

            conn.commit()
            log.info("DMM schema verified/migrated")
        finally:
            conn.close()

    def run_cycle(self) -> dict:
        """Run a complete DMM cycle: quota enforcement, tier management, decay, pruning."""
        conn = self._connect()
        stats = {
            "promoted": 0,
            "demoted": 0,
            "decayed": 0,
            "pruned": 0,
            "quota_enforced": 0,
        }
        try:
            now = datetime.now(timezone.utc).isoformat()

            # 1. Get all agents with memories
            agents = conn.execute(
                "SELECT DISTINCT agent_id FROM memories WHERE deleted = 0"
            ).fetchall()

            for (agent_id,) in agents:
                agent_stats = self._process_agent(conn, agent_id, now)
                for k, v in agent_stats.items():
                    stats[k] = stats.get(k, 0) + v

            # 2. Global cold tier pruning (memories below threshold AND old)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=COLD_PRUNE_DAYS)).isoformat()
            pruned = conn.execute(
                """UPDATE memories SET deleted = 1
                   WHERE tier = 'cold' AND confidence < ? AND accessed_at < ?
                   AND deleted = 0""",
                (COLD_CONFIDENCE_THRESHOLD, cutoff),
            ).rowcount
            stats["pruned"] += pruned

            # 3. Log the cycle
            conn.execute(
                "INSERT INTO dmm_log (cycle_at, action, count, details) VALUES (?, 'full_cycle', ?, ?)",
                (now, sum(stats.values()), json.dumps(stats)),
            )

            conn.commit()
            log.info(
                f"DMM cycle complete: promoted={stats['promoted']}, "
                f"demoted={stats['demoted']}, decayed={stats['decayed']}, "
                f"pruned={stats['pruned']}, quota_enforced={stats['quota_enforced']}"
            )
        except Exception as e:
            log.error(f"DMM cycle error: {e}")
            conn.rollback()
        finally:
            conn.close()

        return stats

    def _process_agent(self, conn: sqlite3.Connection, agent_id: str, now: str) -> dict:
        """Process a single agent: tier management, quota enforcement."""
        stats = {"promoted": 0, "demoted": 0, "decayed": 0, "quota_enforced": 0}

        # ── Tier Promotion: warm → hot ──
        # Memories accessed frequently should be promoted to hot tier
        promoted = conn.execute(
            """UPDATE memories SET tier = 'hot'
               WHERE agent_id = ? AND tier = 'warm'
               AND access_count >= ? AND deleted = 0""",
            (agent_id, ACCESS_PROMOTE_THRESHOLD),
        ).rowcount
        stats["promoted"] = promoted

        # ── Tier Demotion: hot → warm ──
        # Memories not accessed recently should be demoted
        demote_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=ACCESS_DEMOTE_THRESHOLD_DAYS)
        ).isoformat()
        demoted = conn.execute(
            """UPDATE memories SET tier = 'warm'
               WHERE agent_id = ? AND tier = 'hot'
               AND accessed_at < ? AND deleted = 0""",
            (agent_id, demote_cutoff),
        ).rowcount
        stats["demoted"] = demoted

        # ── Tier Demotion: warm → cold ──
        # Low confidence memories move to cold
        demoted_cold = conn.execute(
            """UPDATE memories SET tier = 'cold'
               WHERE agent_id = ? AND tier = 'warm'
               AND confidence < ? AND deleted = 0""",
            (agent_id, COLD_CONFIDENCE_THRESHOLD),
        ).rowcount
        stats["demoted"] += demoted_cold

        # ── Confidence Decay ──
        # Decay memories not accessed in 7 days
        decay_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        decay_factor = 1.0 - DECAY_RATE
        decayed = conn.execute(
            """UPDATE memories SET confidence = MAX(0.05, confidence * ?)
               WHERE agent_id = ? AND accessed_at < ? AND confidence > 0.1 AND deleted = 0""",
            (decay_factor, agent_id, decay_cutoff),
        ).rowcount
        stats["decayed"] = decayed

        # ── Quota Enforcement ──
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND deleted = 0",
            (agent_id,),
        ).fetchone()[0]

        if count > DEFAULT_QUOTA_PER_AGENT:
            # Delete lowest-priority cold memories until under quota
            excess = count - DEFAULT_QUOTA_PER_AGENT
            conn.execute(
                """UPDATE memories SET deleted = 1
                   WHERE id IN (
                       SELECT id FROM memories
                       WHERE agent_id = ? AND deleted = 0
                       ORDER BY priority ASC, confidence ASC, accessed_at ASC
                       LIMIT ?
                   )""",
                (agent_id, excess),
            )
            stats["quota_enforced"] = excess

        # ── Hot tier cap enforcement ──
        hot_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE agent_id = ? AND tier = 'hot' AND deleted = 0",
            (agent_id,),
        ).fetchone()[0]

        if hot_count > MAX_HOT_MEMORIES:
            excess = hot_count - MAX_HOT_MEMORIES
            conn.execute(
                """UPDATE memories SET tier = 'warm'
                   WHERE id IN (
                       SELECT id FROM memories
                       WHERE agent_id = ? AND tier = 'hot' AND deleted = 0
                       ORDER BY access_count ASC, accessed_at ASC
                       LIMIT ?
                   )""",
                (agent_id, excess),
            )
            stats["demoted"] += excess

        # ── Update quota tracking ──
        tier_counts = conn.execute(
            """SELECT tier, COUNT(*) FROM memories
               WHERE agent_id = ? AND deleted = 0 GROUP BY tier""",
            (agent_id,),
        ).fetchall()
        tier_map = {t: c for t, c in tier_counts}

        conn.execute(
            """INSERT OR REPLACE INTO memory_quotas
               (agent_id, max_memories, max_hot, current_count, hot_count, warm_count, cold_count, last_enforced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                DEFAULT_QUOTA_PER_AGENT,
                MAX_HOT_MEMORIES,
                sum(tier_map.values()),
                tier_map.get("hot", 0),
                tier_map.get("warm", 0),
                tier_map.get("cold", 0),
                now,
            ),
        )

        return stats


# ══════════════════════════════════════════════
# 2. SMART CONTEXT CACHING
# ══════════════════════════════════════════════

class ContextCache:
    """
    LRU cache for hot memories per agent. Precomputes context windows
    for fast injection into agent conversations.

    Architecture:
      T1 (hot):  In-memory LRU cache — sub-millisecond access
      T2 (warm): SQLite memories table — millisecond access
      T3 (cold): Low-confidence archived memories — second-level access
    """

    def __init__(self, db_path: str, cache_size: int = CACHE_SIZE_PER_AGENT):
        self.db_path = db_path
        self.cache_size = cache_size
        self._caches: dict[str, OrderedDict] = {}  # agent_id → LRU cache
        self._cache_timestamps: dict[str, float] = {}  # agent_id → last warm time

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def ensure_schema(self):
        """Create context_cache table if not exists."""
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS context_cache (
                    agent_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    access_score REAL NOT NULL DEFAULT 0.0,
                    cached_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, memory_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_score
                ON context_cache(agent_id, access_score DESC)
            """)
            conn.commit()
            log.info("Context cache schema verified")
        finally:
            conn.close()

    def warm_cache(self, agent_id: str) -> int:
        """
        Preload hot memories for an agent into the in-memory LRU cache.
        Called at startup and periodically to refresh.
        Returns number of entries cached.
        """
        conn = self._connect()
        try:
            # Fetch top-N memories by access score (access_count * confidence)
            rows = conn.execute(
                """SELECT id, content, (access_count * confidence) as score
                   FROM memories
                   WHERE agent_id = ? AND deleted = 0
                   ORDER BY score DESC
                   LIMIT ?""",
                (agent_id, self.cache_size),
            ).fetchall()

            # Build LRU cache
            cache = OrderedDict()
            for mem_id, content, score in rows:
                cache[mem_id] = {"content": content, "score": score}

            self._caches[agent_id] = cache
            self._cache_timestamps[agent_id] = time.time()

            # Also persist to SQLite context_cache for cross-restart persistence
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("DELETE FROM context_cache WHERE agent_id = ?", (agent_id,))
            for mem_id, data in cache.items():
                conn.execute(
                    "INSERT INTO context_cache (agent_id, memory_id, content, access_score, cached_at) VALUES (?, ?, ?, ?, ?)",
                    (agent_id, mem_id, data["content"], data["score"], now),
                )
            conn.commit()

            log.debug(f"Warmed cache for agent {agent_id[:8]}: {len(cache)} entries")
            return len(cache)
        finally:
            conn.close()

    def get_cached_context(self, agent_id: str, limit: int = 10) -> list[dict]:
        """
        Fast retrieval of cached hot memories for context injection.
        Falls back to SQLite if in-memory cache is cold.
        """
        # Check in-memory cache first (T1)
        cache = self._caches.get(agent_id)
        if cache:
            entries = list(cache.items())[:limit]
            return [{"id": k, **v} for k, v in entries]

        # Fallback to SQLite cache (T2)
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT memory_id, content, access_score
                   FROM context_cache
                   WHERE agent_id = ?
                   ORDER BY access_score DESC
                   LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
            return [{"id": r[0], "content": r[1], "score": r[2]} for r in rows]
        finally:
            conn.close()

    def warm_all_agents(self) -> dict:
        """Warm caches for all active agents."""
        conn = self._connect()
        stats = {}
        try:
            agents = conn.execute(
                "SELECT DISTINCT agent_id FROM memories WHERE deleted = 0"
            ).fetchall()
        finally:
            conn.close()

        for (agent_id,) in agents:
            count = self.warm_cache(agent_id)
            stats[agent_id[:8]] = count

        log.info(f"Warmed caches for {len(stats)} agents")
        return stats

    def invalidate(self, agent_id: str):
        """Invalidate cache for an agent (e.g., after compaction)."""
        self._caches.pop(agent_id, None)
        self._cache_timestamps.pop(agent_id, None)

    def invalidate_all(self):
        """Invalidate all caches."""
        self._caches.clear()
        self._cache_timestamps.clear()


# ══════════════════════════════════════════════
# 3. KNOWLEDGE HARVESTING
# ══════════════════════════════════════════════

class KnowledgeHarvester:
    """
    Automated entity and fact extraction from agent conversations.
    Populates the knowledge graph (entities + relations tables).

    Extraction targets:
      - Named entities (people, tools, technologies, projects)
      - URLs and code references
      - Key decisions and commitments
      - Technical terms and concepts
    """

    # Patterns for entity extraction
    PATTERNS = {
        "url": re.compile(r'https?://[^\s<>"\')\]]+'),
        "github_repo": re.compile(r'(?:github\.com/|gh:)([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)'),
        "code_ref": re.compile(r'`([a-zA-Z_][a-zA-Z0-9_.]+(?:::[a-zA-Z_][a-zA-Z0-9_]*)*)`'),
        "file_path": re.compile(r'(?:^|\s)(/[a-zA-Z0-9_./\-]+\.[a-zA-Z]{1,6})'),
        "decision": re.compile(r'(?:decided|decision|agreed|committed|will|must|should)\s+(?:to\s+)?(.{10,80})', re.IGNORECASE),
        "named_entity": re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b'),
        "tech_term": re.compile(r'\b(?:Rust|Python|Docker|Railway|Discord|SQLite|Qdrant|DeepSeek|OpenRouter|Groq|WebSocket|TOML|WASM)\b', re.IGNORECASE),
        "bug_ref": re.compile(r'BUG-(\d+)'),
        "agent_name": re.compile(r'\b(?:CTO|Neural\s*Net|Brain|Auditor|Debugger|Leviathan)\b', re.IGNORECASE),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._last_harvest_cursor: dict[str, str] = {}  # agent_id → last processed timestamp

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def ensure_schema(self):
        """Create harvest tracking table."""
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harvest_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    entities_extracted INTEGER NOT NULL DEFAULT 0,
                    relations_extracted INTEGER NOT NULL DEFAULT 0,
                    messages_processed INTEGER NOT NULL DEFAULT 0,
                    harvested_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harvest_cursor (
                    agent_id TEXT PRIMARY KEY,
                    last_processed_at TEXT NOT NULL
                )
            """)
            conn.commit()
            log.info("Knowledge harvesting schema verified")
        finally:
            conn.close()

    def run_harvest(self) -> dict:
        """Run a harvest cycle across all agents."""
        conn = self._connect()
        stats = {"entities": 0, "relations": 0, "messages": 0, "agents": 0}
        try:
            # Get all agents
            agents = conn.execute(
                "SELECT DISTINCT agent_id FROM canonical_sessions"
            ).fetchall()

            for (agent_id,) in agents:
                agent_stats = self._harvest_agent(conn, agent_id)
                stats["entities"] += agent_stats.get("entities", 0)
                stats["relations"] += agent_stats.get("relations", 0)
                stats["messages"] += agent_stats.get("messages", 0)
                stats["agents"] += 1

            conn.commit()
            log.info(
                f"Harvest complete: {stats['entities']} entities, "
                f"{stats['relations']} relations from {stats['messages']} messages "
                f"across {stats['agents']} agents"
            )
        except Exception as e:
            log.error(f"Harvest error: {e}")
            conn.rollback()
        finally:
            conn.close()

        return stats

    def _harvest_agent(self, conn: sqlite3.Connection, agent_id: str) -> dict:
        """Harvest knowledge from a single agent's canonical session."""
        stats = {"entities": 0, "relations": 0, "messages": 0}

        # Get cursor for this agent
        cursor_row = conn.execute(
            "SELECT last_processed_at FROM harvest_cursor WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        last_processed = cursor_row[0] if cursor_row else "2000-01-01T00:00:00"

        # Fetch canonical session messages (serialized as msgpack BLOB)
        row = conn.execute(
            "SELECT messages, updated_at FROM canonical_sessions WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()

        if not row:
            return stats

        messages_blob, updated_at = row

        # Only process if session was updated after our last harvest
        if updated_at <= last_processed:
            return stats

        # Deserialize messages (msgpack format)
        try:
            import msgpack
            messages = msgpack.unpackb(messages_blob, raw=False)
        except ImportError:
            # Fallback: try to extract text content from raw bytes
            log.warning("msgpack not available, skipping message deserialization")
            return stats
        except Exception as e:
            log.warning(f"Failed to deserialize messages for agent {agent_id[:8]}: {e}")
            return stats

        # Process each message for entities
        now = datetime.now(timezone.utc).isoformat()
        processed = 0

        for msg in messages[-HARVEST_BATCH_SIZE:]:  # Process last N messages
            text = self._extract_text(msg)
            if not text:
                continue

            entities = self._extract_entities(text)
            for entity_type, entity_name in entities:
                entity_id = self._store_entity(conn, agent_id, entity_type, entity_name, now)
                if entity_id:
                    stats["entities"] += 1

            # Extract relations between entities found in same message
            relations = self._extract_relations(text, entities)
            for source, rel_type, target in relations:
                self._store_relation(conn, source, rel_type, target, now)
                stats["relations"] += 1

            processed += 1

        stats["messages"] = processed

        # Update cursor
        conn.execute(
            """INSERT OR REPLACE INTO harvest_cursor (agent_id, last_processed_at)
               VALUES (?, ?)""",
            (agent_id, now),
        )

        # Log harvest
        conn.execute(
            """INSERT INTO harvest_log (agent_id, entities_extracted, relations_extracted, messages_processed, harvested_at)
               VALUES (?, ?, ?, ?, ?)""",
            (agent_id, stats["entities"], stats["relations"], processed, now),
        )

        return stats

    def _extract_text(self, msg) -> str:
        """Extract text content from a message (handles various formats)."""
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                return " ".join(parts)
        if isinstance(msg, str):
            return msg
        return ""

    def _extract_entities(self, text: str) -> list[tuple[str, str]]:
        """Extract typed entities from text."""
        entities = []
        seen = set()

        for entity_type, pattern in self.PATTERNS.items():
            for match in pattern.finditer(text):
                name = match.group(1) if match.lastindex else match.group(0)
                name = name.strip()
                if (
                    len(name) >= MIN_ENTITY_LENGTH
                    and len(name) <= MAX_ENTITY_LENGTH
                    and name.lower() not in seen
                ):
                    seen.add(name.lower())
                    entities.append((entity_type, name))

        return entities

    def _extract_relations(
        self, text: str, entities: list[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        """Extract relations between entities found in the same message."""
        relations = []
        if len(entities) < 2:
            return relations

        # Simple co-occurrence relation: entities in same message are "mentioned_with"
        for i, (type_a, name_a) in enumerate(entities):
            for type_b, name_b in entities[i + 1 :]:
                if type_a != type_b or name_a != name_b:
                    relations.append((name_a, "mentioned_with", name_b))

        return relations[:10]  # Cap relations per message

    def _store_entity(
        self, conn: sqlite3.Connection, agent_id: str, entity_type: str, name: str, now: str
    ) -> str | None:
        """Store an entity, deduplicating by name + type."""
        entity_id = hashlib.sha256(f"{entity_type}:{name}".encode()).hexdigest()[:32]

        try:
            conn.execute(
                """INSERT OR IGNORE INTO entities (id, entity_type, name, properties, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    entity_id,
                    entity_type,
                    name,
                    json.dumps({"source_agent": agent_id, "auto_harvested": True}),
                    now,
                    now,
                ),
            )
            return entity_id
        except Exception:
            return None

    def _store_relation(
        self, conn: sqlite3.Connection, source: str, rel_type: str, target: str, now: str
    ):
        """Store a relation between two entities."""
        source_id = hashlib.sha256(f":{source}".encode()).hexdigest()[:32]
        target_id = hashlib.sha256(f":{target}".encode()).hexdigest()[:32]
        rel_id = hashlib.sha256(f"{source_id}:{rel_type}:{target_id}".encode()).hexdigest()[:32]

        try:
            conn.execute(
                """INSERT OR IGNORE INTO relations (id, source_entity, relation_type, target_entity, properties, confidence, created_at)
                   VALUES (?, ?, ?, ?, '{}', 0.7, ?)""",
                (rel_id, source_id, rel_type, target_id, now),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════
# 4. HEALTH ENDPOINT
# ══════════════════════════════════════════════

class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP health endpoint for monitoring."""

    stats = {"cycles": 0, "last_cycle": None, "errors": 0, "started_at": None}

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "memory_manager",
                "version": "1.0.0",
                **self.stats,
            }).encode())
        elif self.path == "/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.stats).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging


# ══════════════════════════════════════════════
# 5. MAIN DAEMON LOOP
# ══════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("Leviathan Memory Manager v1.0 starting")
    log.info(f"  DB: {DB_PATH}")
    log.info(f"  Cycle interval: {CYCLE_INTERVAL}s")
    log.info(f"  Health port: {HEALTH_PORT}")
    log.info("=" * 60)

    # Wait for database to be available
    retries = 0
    while not os.path.exists(DB_PATH) and retries < 60:
        log.info(f"Waiting for database at {DB_PATH}...")
        time.sleep(2)
        retries += 1

    if not os.path.exists(DB_PATH):
        log.error(f"Database not found at {DB_PATH} after 120s. Exiting.")
        sys.exit(1)

    # Initialize subsystems
    dmm = DynamicMemoryManager(DB_PATH)
    cache = ContextCache(DB_PATH)
    harvester = KnowledgeHarvester(DB_PATH)

    # Run schema migrations
    dmm.ensure_schema()
    cache.ensure_schema()
    harvester.ensure_schema()

    # Initial cache warming
    cache.warm_all_agents()

    # Start health endpoint in background thread
    HealthHandler.stats["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
        health_thread = threading.Thread(target=server.serve_forever, daemon=True)
        health_thread.start()
        log.info(f"Health endpoint listening on port {HEALTH_PORT}")
    except OSError as e:
        log.warning(f"Could not start health endpoint: {e}")

    # Main daemon loop
    log.info("Entering main cycle loop")
    while True:
        try:
            cycle_start = time.time()

            # Phase 1: Dynamic Memory Management
            log.info("── Phase 1: DMM cycle ──")
            dmm_stats = dmm.run_cycle()

            # Phase 2: Context Cache Refresh
            log.info("── Phase 2: Cache refresh ──")
            cache_stats = cache.warm_all_agents()

            # Phase 3: Knowledge Harvesting
            log.info("── Phase 3: Knowledge harvest ──")
            harvest_stats = harvester.run_harvest()

            # Update health stats
            elapsed = time.time() - cycle_start
            HealthHandler.stats["cycles"] += 1
            HealthHandler.stats["last_cycle"] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(elapsed, 2),
                "dmm": dmm_stats,
                "cache": cache_stats,
                "harvest": harvest_stats,
            }

            log.info(f"Cycle complete in {elapsed:.1f}s. Sleeping {CYCLE_INTERVAL}s...")

        except KeyboardInterrupt:
            log.info("Shutdown requested")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            HealthHandler.stats["errors"] += 1

        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    main()
