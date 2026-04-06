#!/usr/bin/env python3
"""
OpenClaw → Claude Code Telegram Migration Tool

Discovers all OpenClaw profiles on the machine, extracts:
- Telegram bot tokens + allowlists
- Workspace persona files (SOUL.md, MEMORY.md, IDENTITY.md, USER.md, etc.)
- Cron jobs (schedule + prompts)
- Skills
- Model config

Outputs a ready-to-run config.json + assembled CLAUDE.md per bot.

Usage:
    python3 migrate.py                    # Auto-discover all profiles
    python3 migrate.py --profile bot2     # Migrate specific profile
    python3 migrate.py --config-dir ~/.openclaw-mybot  # Direct path
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_openclaw_profiles() -> list[dict]:
    """Find all OpenClaw config directories."""
    home = Path.home()
    profiles = []

    # Check default profile
    default = home / ".openclaw"
    if (default / "openclaw.json").exists():
        profiles.append({
            "name": "default",
            "profile": None,
            "config_dir": default,
        })

    # Check named profiles (~/.openclaw-*)
    for d in sorted(home.iterdir()):
        if d.name.startswith(".openclaw-") and d.is_dir():
            cfg = d / "openclaw.json"
            if cfg.exists():
                profile_name = d.name.replace(".openclaw-", "")
                profiles.append({
                    "name": profile_name,
                    "profile": profile_name,
                    "config_dir": d,
                })

    return profiles


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_telegram(config: dict) -> dict | None:
    """Extract Telegram config from openclaw.json."""
    tg = config.get("channels", {}).get("telegram", {})
    if not tg:
        tg = config.get("telegram", {})
    if not tg or not tg.get("botToken"):
        return None
    return {
        "botToken": tg["botToken"],
        "allowFrom": tg.get("allowFrom", []),
        "dmPolicy": tg.get("dmPolicy", "open"),
        "groupPolicy": tg.get("groupPolicy", "open"),
        "groups": tg.get("groups", {}),
        "streaming": tg.get("streaming", False),
    }


def extract_cron_jobs(config_dir: Path) -> list[dict]:
    """Extract cron jobs from cron/jobs.json."""
    cron_file = config_dir / "cron" / "jobs.json"
    if not cron_file.exists():
        return []

    data = json.loads(cron_file.read_text())
    jobs_raw = data.get("jobs", [])
    jobs = []

    for j in jobs_raw:
        if not j.get("enabled", True):
            continue

        schedule = j.get("schedule", {})
        prompt = ""
        payload = j.get("payload", {})
        if isinstance(payload, dict):
            prompt = payload.get("text", "") or payload.get("message", "") or payload.get("prompt", "")
        elif isinstance(payload, str):
            prompt = payload

        # Convert schedule to cron expression or interval
        cron_expr = None
        interval_seconds = None
        timezone = schedule.get("tz", "UTC")

        if schedule.get("kind") == "cron":
            cron_expr = schedule.get("expr")
        elif schedule.get("kind") == "every":
            interval_seconds = schedule.get("everyMs", 0) // 1000

        delivery = j.get("delivery", {})
        delivery_mode = delivery.get("mode", "none") if isinstance(delivery, dict) else "none"
        delivery_channel = delivery.get("channel", "none") if isinstance(delivery, dict) else "none"
        delivery_to = delivery.get("to", None) if isinstance(delivery, dict) else None

        jobs.append({
            "id": j.get("id", ""),
            "name": j.get("name", "unnamed"),
            "enabled": True,
            "cron": cron_expr,
            "interval_seconds": interval_seconds,
            "timezone": timezone,
            "prompt": prompt,
            "delivery": {
                "mode": delivery_mode,
                "channel": delivery_channel,
                "to": delivery_to,
            },
        })

    return jobs


def extract_models(config: dict) -> dict:
    """Extract model configuration."""
    models = config.get("models", {})
    agents = config.get("agents", {})

    # Try to find default model
    default_model = "sonnet"
    if isinstance(models, dict):
        primary = models.get("primary", {})
        if isinstance(primary, dict):
            model_id = primary.get("model", "")
            if "opus" in model_id.lower():
                default_model = "opus"
            elif "haiku" in model_id.lower():
                default_model = "haiku"

    return {"default": default_model}


def find_workspace(config_dir: Path) -> Path | None:
    """Find the workspace directory."""
    ws = config_dir / "workspace"
    if ws.is_dir():
        return ws
    return None


def find_persona_files(workspace: Path) -> dict[str, Path]:
    """Find persona definition files in workspace."""
    persona_files = {}
    for name in ["SOUL.md", "MEMORY.md", "IDENTITY.md", "USER.md",
                  "AGENTS.md", "TOOLS.md", "COGNITION.md", "META.md",
                  "HEARTBEAT.md", "WRITING-STYLE-GUIDE.md"]:
        f = workspace / name
        if f.exists():
            persona_files[name] = f
    # Also check for MEMORY-*.md split files
    for f in workspace.glob("MEMORY-*.md"):
        persona_files[f.name] = f
    return persona_files


def find_skills(workspace: Path) -> list[dict]:
    """Find all skills in workspace/skills/."""
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return []

    skills = []
    for d in sorted(skills_dir.iterdir()):
        if d.is_dir():
            skill_md = d / "SKILL.md"
            if skill_md.exists():
                skills.append({
                    "name": d.name,
                    "path": str(d),
                    "skill_file": str(skill_md),
                })
    return skills


def extract_mcp_servers(config_dir: Path, workspace: Path) -> dict:
    """Extract MCP server configs from mcporter.json and openclaw.json plugins."""
    mcp_servers = {}

    # Check mcporter.json (workspace/config/mcporter.json)
    mcporter = workspace / "config" / "mcporter.json"
    if mcporter.exists():
        try:
            data = json.loads(mcporter.read_text())
            servers = data.get("mcpServers", {})
            for name, cfg in servers.items():
                if "command" in cfg:
                    # Command-based MCP (local spawn)
                    mcp_servers[name] = {
                        "type": "stdio",
                        "command": cfg["command"],
                        "args": cfg.get("args", []),
                        "env": cfg.get("env", {}),
                    }
                elif "baseUrl" in cfg:
                    # HTTP-based MCP (remote)
                    mcp_servers[name] = {
                        "type": "sse",
                        "url": cfg["baseUrl"],
                        "headers": cfg.get("headers", {}),
                    }
        except Exception as e:
            print(f"  WARNING: Failed to parse mcporter.json: {e}")

    # Check openclaw.json plugins for web search (Tavily, Brave, etc.)
    config_file = config_dir / "openclaw.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            plugins = cfg.get("plugins", {}).get("entries", {})
            for pname, pcfg in plugins.items():
                if not pcfg.get("enabled"):
                    continue
                plugin_config = pcfg.get("config", {})

                # Tavily web search
                if pname == "tavily" and "webSearch" in plugin_config:
                    api_key = plugin_config["webSearch"].get("apiKey", "")
                    if api_key:
                        mcp_servers["tavily-search"] = {
                            "type": "env_var",
                            "note": "Set TAVILY_API_KEY in your environment",
                            "api_key": api_key,
                        }
        except Exception:
            pass

    return mcp_servers


def extract_browser_config(config: dict) -> dict | None:
    """Extract browser/CDP configuration."""
    browser = config.get("browser", {})
    if not browser:
        return None

    profiles = browser.get("profiles", {})
    default = browser.get("defaultProfile", "")

    result = {
        "default_profile": default,
        "profiles": {},
    }
    for pname, pcfg in profiles.items():
        result["profiles"][pname] = {
            "cdp_url": pcfg.get("cdpUrl", ""),
            "cdp_port": pcfg.get("cdpPort", 0),
        }
    return result


# ---------------------------------------------------------------------------
# CLAUDE.md Assembly
# ---------------------------------------------------------------------------

def assemble_claude_md(persona_files: dict[str, Path], bot_name: str, model: str) -> str:
    """Assemble a CLAUDE.md from existing persona files."""
    sections = []

    sections.append(f"# {bot_name} - System Instructions\n")
    sections.append(f"> Auto-generated from OpenClaw migration on {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    sections.append(f"> Read this file completely on every session start.\n")
    sections.append(f"**Default model:** {model}\n")

    # SOUL.md is the core personality - include verbatim
    if "SOUL.md" in persona_files:
        sections.append("---\n## SOUL\n")
        sections.append(persona_files["SOUL.md"].read_text().strip())

    # IDENTITY.md
    if "IDENTITY.md" in persona_files:
        sections.append("\n---\n## IDENTITY\n")
        sections.append(persona_files["IDENTITY.md"].read_text().strip())

    # USER.md
    if "USER.md" in persona_files:
        sections.append("\n---\n## USER\n")
        sections.append(persona_files["USER.md"].read_text().strip())

    # MEMORY.md (index)
    if "MEMORY.md" in persona_files:
        sections.append("\n---\n## MEMORY INDEX\n")
        sections.append("> Read the full MEMORY.md file for detailed context. Load MEMORY-*.md files as needed.\n")
        # Don't inline the full memory - just reference it
        content = persona_files["MEMORY.md"].read_text().strip()
        # Include first 100 lines max to avoid bloating the system prompt
        lines = content.split("\n")
        if len(lines) > 100:
            sections.append("\n".join(lines[:100]))
            sections.append(f"\n... ({len(lines) - 100} more lines in MEMORY.md)")
        else:
            sections.append(content)

    # AGENTS.md
    if "AGENTS.md" in persona_files:
        sections.append("\n---\n## AGENTS\n")
        sections.append(persona_files["AGENTS.md"].read_text().strip())

    # WRITING-STYLE-GUIDE.md
    if "WRITING-STYLE-GUIDE.md" in persona_files:
        sections.append("\n---\n## WRITING STYLE GUIDE\n")
        sections.append(persona_files["WRITING-STYLE-GUIDE.md"].read_text().strip())

    # Skills section
    sections.append("\n---\n## SKILLS\n")
    sections.append("""You have skills installed in `skills/` directory. Each skill is a folder with a `SKILL.md` file containing specialized instructions for a specific task.

**Before doing any complex task, check if a matching skill exists:**
```
ls skills/
cat skills/<skill-name>/SKILL.md
```

When a user asks for something that matches a skill, read that skill's SKILL.md first and follow its instructions.
""")

    # Universal rules
    sections.append("\n---\n## UNIVERSAL RULES (added by migration)\n")
    sections.append("""
1. **NEVER USE EM DASHES (—).** Use a hyphen (-) or rewrite the sentence. Check every response.
2. This directory is your working directory. Files you create/edit here are persistent.
3. Read MEMORY.md at session start to load context.
4. Read MEMORY-*.md files as needed based on the conversation topic.
5. Check skills/ directory for available skills before doing complex work manually.
""")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def migrate_profile(profile: dict, output_dir: Path, workspace_dir: Path | None = None) -> dict | None:
    """Migrate a single OpenClaw profile. Returns config entry or None."""
    config_dir = profile["config_dir"]
    name = profile["name"]

    print(f"\n{'='*60}")
    print(f"Migrating: {name} ({config_dir})")
    print(f"{'='*60}")

    # Load openclaw.json
    cfg = json.loads((config_dir / "openclaw.json").read_text())

    # Telegram
    tg = extract_telegram(cfg)
    if not tg:
        print(f"  SKIP: No Telegram bot token found")
        return None
    print(f"  Telegram: token={tg['botToken'][:12]}... allow={tg['allowFrom']}")

    # Workspace
    workspace = find_workspace(config_dir)
    if not workspace:
        print(f"  SKIP: No workspace directory found")
        return None

    # Persona files
    persona_files = find_persona_files(workspace)
    print(f"  Persona files: {list(persona_files.keys())}")

    # Bot name from IDENTITY.md or profile name
    bot_name = name
    if "IDENTITY.md" in persona_files:
        identity = persona_files["IDENTITY.md"].read_text()
        for line in identity.split("\n"):
            if "**Name:**" in line:
                extracted = line.split("**Name:**")[-1].strip()
                if extracted and extracted != "_(pick something you like)_":
                    bot_name = extracted
                break
    print(f"  Bot name: {bot_name}")

    # Models
    models = extract_models(cfg)
    print(f"  Model: {models['default']}")

    # Cron jobs
    cron_jobs = extract_cron_jobs(config_dir)
    print(f"  Cron jobs: {len(cron_jobs)}")
    for j in cron_jobs:
        sched = j["cron"] or f"every {j['interval_seconds']}s"
        print(f"    - {j['name']}: {sched}")

    # Skills
    skills = find_skills(workspace)
    print(f"  Skills: {len(skills)}")

    # MCP servers
    mcp_servers = extract_mcp_servers(config_dir, workspace)
    print(f"  MCP servers: {len(mcp_servers)}")
    for mname, mcfg in mcp_servers.items():
        print(f"    - {mname}: {mcfg.get('type', '?')}")

    # Browser
    browser_cfg = extract_browser_config(cfg)
    if browser_cfg:
        print(f"  Browser: {browser_cfg['default_profile']} ({len(browser_cfg['profiles'])} profiles)")

    # Determine working directory
    if workspace_dir:
        cwd = workspace_dir
    else:
        cwd = workspace
    print(f"  Working directory: {cwd}")

    # --- Write outputs ---

    bot_dir = output_dir / name
    bot_dir.mkdir(parents=True, exist_ok=True)

    # Save telegram config
    tg_path = bot_dir / "telegram.json"
    json.dump(tg, open(tg_path, "w"), indent=2)
    print(f"  Saved: {tg_path}")

    # Save cron jobs
    if cron_jobs:
        cron_path = bot_dir / "cron_jobs.json"
        json.dump(cron_jobs, open(cron_path, "w"), indent=2)
        print(f"  Saved: {cron_path}")

    # Assemble and write CLAUDE.md
    claude_md = assemble_claude_md(persona_files, bot_name, models["default"])
    claude_md_path = cwd / "CLAUDE.md"
    # Don't overwrite if it already exists
    if claude_md_path.exists():
        backup = cwd / "CLAUDE.md.pre-migration"
        shutil.copy2(claude_md_path, backup)
        print(f"  Backed up existing CLAUDE.md to {backup}")
    claude_md_path.write_text(claude_md)
    print(f"  Wrote: {claude_md_path} ({len(claude_md)} bytes)")

    # Copy skills to workspace if not already there
    if skills and cwd != workspace:
        skills_target = cwd / "skills"
        if not skills_target.exists():
            shutil.copytree(workspace / "skills", skills_target)
            print(f"  Copied {len(skills)} skills to {skills_target}")

    # Save MCP config as Claude Code settings format
    if mcp_servers:
        claude_mcp = {}
        env_vars_needed = []
        for mname, mcfg in mcp_servers.items():
            if mcfg["type"] == "stdio":
                cmd_parts = mcfg["command"].split()
                claude_mcp[mname] = {
                    "command": cmd_parts[0],
                    "args": cmd_parts[1:] + mcfg.get("args", []),
                }
                if mcfg.get("env"):
                    claude_mcp[mname]["env"] = mcfg["env"]
            elif mcfg["type"] == "sse":
                claude_mcp[mname] = {
                    "url": mcfg["url"],
                }
                if mcfg.get("headers"):
                    claude_mcp[mname]["headers"] = mcfg["headers"]
            elif mcfg["type"] == "env_var":
                env_vars_needed.append(f"{mname}: {mcfg.get('note', '')}")

        if claude_mcp:
            mcp_settings = {"mcpServers": claude_mcp}
            mcp_path = bot_dir / "mcp_servers.json"
            json.dump(mcp_settings, open(mcp_path, "w"), indent=2)
            print(f"  Saved MCP config: {mcp_path}")
            print(f"  NOTE: Copy mcpServers into ~/.claude/settings.json or .claude/settings.json in your project")

        if env_vars_needed:
            print(f"  ENV VARS NEEDED:")
            for ev in env_vars_needed:
                print(f"    {ev}")

    # Save browser config
    if browser_cfg:
        browser_path = bot_dir / "browser_config.json"
        json.dump(browser_cfg, open(browser_path, "w"), indent=2)
        print(f"  Saved browser config: {browser_path}")

    return {
        "name": name,
        "bot_name": bot_name,
        "cwd": str(cwd),
        "model": models["default"],
        "telegram_config": str(tg_path),
        "cron_jobs": str(bot_dir / "cron_jobs.json") if cron_jobs else None,
        "mcp_servers": str(bot_dir / "mcp_servers.json") if mcp_servers else None,
        "browser_config": str(bot_dir / "browser_config.json") if browser_cfg else None,
        "skills_count": len(skills),
        "persona_files": list(persona_files.keys()),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate OpenClaw bots to Claude Code Telegram")
    parser.add_argument("--profile", help="Migrate specific profile name (e.g., 'bot2')")
    parser.add_argument("--config-dir", help="Direct path to OpenClaw config dir")
    parser.add_argument("--workspace-dir", help="Override working directory for the bot")
    parser.add_argument("--output", default=str(Path.home() / "claude-agents"),
                       help="Output directory (default: ~/claude-agents)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    workspace_override = Path(args.workspace_dir) if args.workspace_dir else None

    # Discover profiles
    if args.config_dir:
        profiles = [{
            "name": Path(args.config_dir).name.replace(".openclaw-", "").replace(".openclaw", "default"),
            "profile": None,
            "config_dir": Path(args.config_dir),
        }]
    elif args.profile:
        config_dir = Path.home() / f".openclaw-{args.profile}"
        if not config_dir.exists():
            config_dir = Path.home() / ".openclaw"
        profiles = [{
            "name": args.profile,
            "profile": args.profile,
            "config_dir": config_dir,
        }]
    else:
        profiles = find_openclaw_profiles()

    if not profiles:
        print("No OpenClaw profiles found.")
        sys.exit(1)

    print(f"Found {len(profiles)} OpenClaw profile(s): {[p['name'] for p in profiles]}")

    # Migrate each
    migrated = []
    for p in profiles:
        result = migrate_profile(p, output_dir, workspace_override)
        if result:
            migrated.append(result)

    if not migrated:
        print("\nNo bots migrated.")
        sys.exit(1)

    # Write master config
    master_config = {
        "version": 1,
        "migrated_at": datetime.now().isoformat(),
        "bots": migrated,
    }
    config_path = output_dir / "config.json"
    json.dump(master_config, open(config_path, "w"), indent=2)

    print(f"\n{'='*60}")
    print(f"Migration complete!")
    print(f"{'='*60}")
    print(f"  Bots migrated: {len(migrated)}")
    print(f"  Config: {config_path}")
    print(f"\nNext steps:")
    print(f"  1. Review CLAUDE.md in each bot's working directory")
    print(f"  2. Run: python3 bridge.py")
    print(f"  3. Send a test message to your bot on Telegram")


if __name__ == "__main__":
    main()
