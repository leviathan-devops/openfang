#!/usr/bin/env python3
"""
Leviathan Discord Bridge v2.0 — Cloud & Brain Bots + Slash Commands
====================================================================
Bridges DISCORD_BOT_TOKEN_CLOUD and DISCORD_BOT_TOKEN_BRAIN to the
OpenFang kernel API. Each bot runs its own Discord gateway connection
and forwards messages to the appropriate agent via HTTP API.

NEW in v2.0: Discord Slash Commands
  /status     — System health + agent overview
  /agent      — Detailed agent info
  /memory     — Search agent memories
  /compact    — Trigger memory compaction
  /tasks      — List active/pending tasks
  /spawn      — Spawn a new sub-agent
  /help       — Command reference

This exists because the upstream OpenFang binary (v0.2.3) does not include
our extra_discord multi-bot routing code. The CTO bot connects natively
through the kernel's built-in Discord adapter; Cloud and Brain use this bridge.

Architecture:
  Discord Gateway (Cloud) → discord_bridge.py → POST /api/agents/{cloud_id}/message
  Discord Gateway (Brain) → discord_bridge.py → POST /api/agents/{brain_id}/message
  Slash Commands → discord_bridge.py → GET/POST /api/* → Discord response
"""

import asyncio
import os
import sys
import logging
import json
import aiohttp
import discord
from discord import Intents, app_commands

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ'
)
log = logging.getLogger('discord_bridge')

API_URL = os.environ.get('OPENFANG_API_URL', 'http://localhost:4200')
API_KEY = os.environ.get('OPENFANG_API_KEY', 'leviathan-test-key-2026')
GUILD_ID = int(os.environ.get('DISCORD_GUILD_ID', '1475947548811202613'))
OWNER_ID = int(os.environ.get('DISCORD_OWNER_ID', '1475946314393981135'))

# Auth headers for API calls
API_HEADERS = {
    'Authorization': f'Bearer {API_KEY}',
    'Content-Type': 'application/json',
}


async def get_agent_id(session: aiohttp.ClientSession, agent_name: str) -> str | None:
    """Fetch agent ID by name from the kernel API."""
    try:
        async with session.get(
            f'{API_URL}/api/agents',
            headers={'Authorization': f'Bearer {API_KEY}'}
        ) as resp:
            if resp.status == 200:
                agents = await resp.json()
                for a in agents:
                    if a['name'] == agent_name:
                        return a['id']
    except Exception as e:
        log.error(f'Failed to fetch agent ID for {agent_name}: {e}')
    return None


async def api_get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    """Generic API GET request."""
    try:
        async with session.get(
            f'{API_URL}{path}',
            headers=API_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            log.error(f'API GET {path} returned {resp.status}')
    except Exception as e:
        log.error(f'API GET {path} failed: {e}')
    return None


async def api_post(session: aiohttp.ClientSession, path: str, data: dict = None) -> dict | None:
    """Generic API POST request."""
    try:
        async with session.post(
            f'{API_URL}{path}',
            headers=API_HEADERS,
            json=data or {},
            timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            log.error(f'API POST {path} returned {resp.status}: {text[:200]}')
    except Exception as e:
        log.error(f'API POST {path} failed: {e}')
    return None


async def send_to_agent(
    session: aiohttp.ClientSession,
    agent_id: str,
    content: str,
    author: str,
    channel_name: str = '',
    reply_context: str = ''
) -> str | None:
    """Send a message to an agent via the kernel API and return the response."""
    parts = []
    if channel_name:
        parts.append(f'[#{channel_name}]')
    if reply_context:
        parts.append(reply_context)
    parts.append(f'{author}: {content}')
    full_message = ' '.join(parts)

    try:
        async with session.post(
            f'{API_URL}/api/agents/{agent_id}/message',
            headers=API_HEADERS,
            json={'message': full_message},
            timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get('response', '')
            else:
                text = await resp.text()
                log.error(f'Agent API returned {resp.status}: {text[:200]}')
    except asyncio.TimeoutError:
        log.error(f'Agent {agent_id} timed out after 120s')
    except Exception as e:
        log.error(f'Failed to send to agent {agent_id}: {e}')
    return None


# ══════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════

def register_slash_commands(tree: app_commands.CommandTree, bot: 'BridgeBot'):
    """Register all slash commands on the command tree."""

    guild = discord.Object(id=GUILD_ID)

    @tree.command(name="status", description="System health + agent overview", guild=guild)
    async def cmd_status(interaction: discord.Interaction):
        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        # Fetch system status
        status = await api_get(bot.http_session, '/api/status')
        agents = await api_get(bot.http_session, '/api/agents')
        health = await api_get(bot.http_session, '/api/health')

        embed = discord.Embed(
            title="Leviathan System Status",
            color=0x00FF88 if health else 0xFF4444,
        )

        if status:
            uptime = status.get('uptime', 'unknown')
            embed.add_field(name="Uptime", value=str(uptime), inline=True)
            embed.add_field(name="Version", value=status.get('version', '?'), inline=True)

        if agents and isinstance(agents, list):
            agent_lines = []
            for a in agents:
                name = a.get('name', '?')
                model = a.get('model', '?')
                agent_lines.append(f"**{name}** — `{model}`")
            embed.add_field(
                name=f"Agents ({len(agents)})",
                value="\n".join(agent_lines) if agent_lines else "None",
                inline=False,
            )
        else:
            embed.add_field(name="Agents", value="Failed to fetch", inline=False)

        # Memory Manager health
        try:
            async with bot.http_session.get(
                'http://localhost:4201/health',
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    mm = await resp.json()
                    cycles = mm.get('cycles', 0)
                    embed.add_field(
                        name="Memory Manager",
                        value=f"OK — {cycles} cycles",
                        inline=True,
                    )
        except Exception:
            embed.add_field(name="Memory Manager", value="Offline", inline=True)

        embed.set_footer(text="Leviathan Cloud OS v3.2")
        await interaction.followup.send(embed=embed)

    @tree.command(name="agent", description="Detailed info about an agent", guild=guild)
    @app_commands.describe(name="Agent name (cto/cloud/brain/auditor/debugger)")
    async def cmd_agent(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        # Resolve name aliases
        name_map = {
            'cto': 'leviathan', 'cloud': 'neural-net', 'neural-net': 'neural-net',
            'brain': 'brain', 'auditor': 'auditor', 'debugger': 'debugger',
        }
        resolved = name_map.get(name.lower(), name.lower())

        agent_id = await get_agent_id(bot.http_session, resolved)
        if not agent_id:
            await interaction.followup.send(f"Agent `{name}` not found.")
            return

        # Fetch agent session
        session_data = await api_get(bot.http_session, f'/api/agents/{agent_id}/session')

        embed = discord.Embed(title=f"Agent: {resolved}", color=0x5865F2)
        embed.add_field(name="ID", value=f"`{agent_id[:12]}...`", inline=True)

        if session_data:
            msg_count = session_data.get('message_count', session_data.get('messages', '?'))
            embed.add_field(name="Messages", value=str(msg_count), inline=True)

        await interaction.followup.send(embed=embed)

    @tree.command(name="memory", description="Search agent memories", guild=guild)
    @app_commands.describe(
        query="Search query",
        agent="Agent name (default: leviathan)"
    )
    async def cmd_memory(interaction: discord.Interaction, query: str, agent: str = "leviathan"):
        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        name_map = {
            'cto': 'leviathan', 'cloud': 'neural-net',
            'brain': 'brain', 'auditor': 'auditor', 'debugger': 'debugger',
        }
        resolved = name_map.get(agent.lower(), agent.lower())
        agent_id = await get_agent_id(bot.http_session, resolved)

        if not agent_id:
            await interaction.followup.send(f"Agent `{agent}` not found.")
            return

        # Send a memory_recall request to the agent
        response = await send_to_agent(
            bot.http_session,
            agent_id,
            f'Run memory_recall with query: "{query}" and report the results concisely.',
            'System',
            channel_name='slash-command',
        )

        if response:
            # Truncate if too long
            if len(response) > 1900:
                response = response[:1900] + "..."
            await interaction.followup.send(f"**Memory Search** (`{query}`):\n{response}")
        else:
            await interaction.followup.send("No results or agent unreachable.")

    @tree.command(name="compact", description="Trigger memory compaction for an agent", guild=guild)
    @app_commands.describe(agent="Agent name (default: leviathan)")
    async def cmd_compact(interaction: discord.Interaction, agent: str = "leviathan"):
        # Only owner can trigger compaction
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the Owner can trigger compaction.", ephemeral=True)
            return

        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        name_map = {
            'cto': 'leviathan', 'cloud': 'neural-net',
            'brain': 'brain', 'auditor': 'auditor', 'debugger': 'debugger',
        }
        resolved = name_map.get(agent.lower(), agent.lower())
        agent_id = await get_agent_id(bot.http_session, resolved)

        if not agent_id:
            await interaction.followup.send(f"Agent `{agent}` not found.")
            return

        result = await api_post(bot.http_session, f'/api/agents/{agent_id}/session/compact')
        if result:
            await interaction.followup.send(f"Compaction triggered for **{resolved}**.")
        else:
            await interaction.followup.send(f"Compaction failed for **{resolved}**.")

    @tree.command(name="tasks", description="List active and pending tasks", guild=guild)
    async def cmd_tasks(interaction: discord.Interaction):
        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        # Fetch tasks from the CTO agent
        agents = await api_get(bot.http_session, '/api/agents')
        if not agents:
            await interaction.followup.send("Could not fetch agents.")
            return

        cto_id = None
        for a in agents:
            if a.get('name') == 'leviathan':
                cto_id = a['id']
                break

        if not cto_id:
            await interaction.followup.send("CTO agent not found.")
            return

        response = await send_to_agent(
            bot.http_session,
            cto_id,
            'List all active and pending tasks from the task queue. Be concise.',
            'System',
            channel_name='slash-command',
        )

        if response:
            if len(response) > 1900:
                response = response[:1900] + "..."
            await interaction.followup.send(f"**Active Tasks:**\n{response}")
        else:
            await interaction.followup.send("No tasks found or CTO unreachable.")

    @tree.command(name="spawn", description="Spawn a new sub-agent", guild=guild)
    @app_commands.describe(
        name="Agent name (kebab-case)",
        model="Model to use (default: deepseek-chat)",
        provider="Provider (default: deepseek)"
    )
    async def cmd_spawn(
        interaction: discord.Interaction,
        name: str,
        model: str = "deepseek-chat",
        provider: str = "deepseek"
    ):
        # Only owner can spawn agents
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the Owner can spawn agents.", ephemeral=True)
            return

        await interaction.response.defer()
        if not bot.http_session:
            await interaction.followup.send("Bridge not ready.")
            return

        result = await api_post(
            bot.http_session,
            '/api/agents',
            {"name": name, "model": model, "provider": provider},
        )

        if result:
            agent_id = result.get('id', result.get('agent_id', '?'))
            await interaction.followup.send(f"Spawned **{name}** (ID: `{str(agent_id)[:12]}...`)")
        else:
            await interaction.followup.send(f"Failed to spawn **{name}**.")

    @tree.command(name="lev-help", description="Leviathan slash command reference", guild=guild)
    async def cmd_help(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Leviathan Slash Commands",
            description="Control the Leviathan Cloud OS from Discord.",
            color=0x5865F2,
        )
        commands = [
            ("/status", "System health + agent overview"),
            ("/agent <name>", "Detailed agent info (cto/cloud/brain/auditor/debugger)"),
            ("/memory <query>", "Search agent memories"),
            ("/compact <agent>", "Trigger memory compaction (Owner only)"),
            ("/tasks", "List active/pending tasks"),
            ("/spawn <name> [model] [provider]", "Spawn a sub-agent (Owner only)"),
            ("/lev-help", "This help message"),
        ]
        for cmd, desc in commands:
            embed.add_field(name=cmd, value=desc, inline=False)
        embed.set_footer(text="Leviathan Cloud OS v3.2 — Slash Commands v2.0")
        await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════
# BOT CLIENT
# ══════════════════════════════════════════════

class BridgeBot(discord.Client):
    """Discord bot that bridges messages to an OpenFang agent + slash commands."""

    def __init__(self, bot_name: str, agent_name: str, respond_policy: str = 'mention',
                 enable_slash: bool = False, **kwargs):
        """
        Args:
            bot_name: Display name for logging (e.g., "Cloud", "Brain")
            agent_name: OpenFang agent name (e.g., "neural-net", "brain")
            respond_policy: "all" = respond to all messages, "mention" = only @mentions/DMs,
                           "channels" = only in specific channel IDs
            enable_slash: Whether to register slash commands on this bot
        """
        super().__init__(**kwargs)
        self.bot_name = bot_name
        self.agent_name = agent_name
        self.respond_policy = respond_policy
        self.agent_id: str | None = None
        self.http_session: aiohttp.ClientSession | None = None
        self.allowed_channels: set[int] = set()
        self.enable_slash = enable_slash

        if enable_slash:
            self.tree = app_commands.CommandTree(self)
        else:
            self.tree = None

    async def setup_hook(self):
        """Called after login, before on_ready. Register slash commands here."""
        if self.tree and self.enable_slash:
            register_slash_commands(self.tree, self)
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f'{self.bot_name}: Slash commands synced to guild {GUILD_ID}')

    async def on_ready(self):
        log.info(f'{self.bot_name} bot ready: {self.user} (ID: {self.user.id})')
        self.http_session = aiohttp.ClientSession()
        # Resolve agent ID
        for attempt in range(10):
            self.agent_id = await get_agent_id(self.http_session, self.agent_name)
            if self.agent_id:
                log.info(f'{self.bot_name} → agent {self.agent_name} (ID: {self.agent_id})')
                break
            log.warning(f'{self.bot_name}: agent {self.agent_name} not found, retry {attempt+1}/10...')
            await asyncio.sleep(5)
        if not self.agent_id:
            log.error(f'{self.bot_name}: could not find agent {self.agent_name} after 10 retries')

    async def on_message(self, message: discord.Message):
        # Ignore own messages and other bots
        if message.author == self.user or message.author.bot:
            return

        # Guild filter
        if message.guild and message.guild.id != GUILD_ID:
            return

        # Check if we should respond
        should_respond = False

        if isinstance(message.channel, discord.DMChannel):
            should_respond = True
        elif self.respond_policy == 'all':
            should_respond = True
        elif self.respond_policy == 'mention':
            should_respond = self.user in message.mentions
        elif self.respond_policy == 'channels':
            should_respond = message.channel.id in self.allowed_channels

        if not should_respond:
            return

        if not self.agent_id:
            if self.http_session:
                self.agent_id = await get_agent_id(self.http_session, self.agent_name)
            if not self.agent_id:
                log.warning(f'{self.bot_name}: no agent_id, cannot respond')
                return

        # Build reply context from referenced message
        reply_context = ''
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                ref_content = ref.content[:200] if ref.content else ''
                reply_context = f'[Replying to @{ref.author.display_name}: "{ref_content}"]'

        # Get channel name
        channel_name = ''
        if hasattr(message.channel, 'name'):
            channel_name = message.channel.name

        # Show typing while processing
        async with message.channel.typing():
            response = await send_to_agent(
                self.http_session,
                self.agent_id,
                message.content,
                message.author.display_name,
                channel_name=channel_name,
                reply_context=reply_context
            )

        if response:
            # Split long messages (Discord 2000 char limit)
            chunks = [response[i:i+1990] for i in range(0, len(response), 1990)]
            for chunk in chunks:
                await message.channel.send(chunk)

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

async def run_bridge():
    """Launch Cloud and Brain bots concurrently."""
    cloud_token = os.environ.get('DISCORD_BOT_TOKEN_CLOUD')
    brain_token = os.environ.get('DISCORD_BOT_TOKEN_BRAIN')

    if not cloud_token and not brain_token:
        log.warning('No DISCORD_BOT_TOKEN_CLOUD or DISCORD_BOT_TOKEN_BRAIN set. Bridge idle.')
        await asyncio.Event().wait()
        return

    intents = Intents.default()
    intents.message_content = True
    intents.guilds = True

    tasks = []

    if cloud_token:
        # Cloud bot gets slash commands (it's the always-on bot)
        cloud_bot = BridgeBot(
            bot_name='Cloud',
            agent_name='neural-net',
            respond_policy='all',
            enable_slash=True,
            intents=intents
        )
        tasks.append(cloud_bot.start(cloud_token))
        log.info('Cloud bot bridge starting (respond_policy=all, slash_commands=enabled)')
    else:
        log.warning('DISCORD_BOT_TOKEN_CLOUD not set — Cloud bot disabled')

    if brain_token:
        brain_intents = Intents.default()
        brain_intents.message_content = True
        brain_intents.guilds = True
        brain_bot = BridgeBot(
            bot_name='Brain',
            agent_name='brain',
            respond_policy='channels',
            enable_slash=False,
            intents=brain_intents
        )
        # Brain only responds in #meta-prompting and #agent-prompting
        brain_bot.allowed_channels = {1476978586828411073, 1477054899161141402}
        tasks.append(brain_bot.start(brain_token))
        log.info('Brain bot bridge starting (channels: meta-prompting, agent-prompting)')
    else:
        log.warning('DISCORD_BOT_TOKEN_BRAIN not set — Brain bot disabled')

    await asyncio.gather(*tasks)


if __name__ == '__main__':
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        log.info('Bridge shutting down')
    except Exception as e:
        log.error(f'Bridge crashed: {e}', exc_info=True)
        sys.exit(1)
