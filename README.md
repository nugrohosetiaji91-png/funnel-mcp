<p align="center">
  <img src="assets/funnel.png" alt="Project Banner" width="100%">
</p>

# funnel-mcp

> Turn any Windows machine into an AI workstation. No cloud, no port forwarding, no public IP.
> Just Tailscale Funnel + a single Python file.

## The Problem

You want ChatGPT or Grok to control your PC — run commands, browse the web, manage files. But MCP servers need either local stdio (Claude Desktop only) or a public HTTPS endpoint (hard to set up, risky to expose).

## The Solution

Tailscale Funnel gives you a **free public HTTPS URL** without opening ports. Secret-path auth means only someone with the token can reach your server. One Python file, zero config.

```mermaid
flowchart TB
    subgraph clients["AI Clients"]
        direction LR
        GPT["ChatGPT"]
        Grok["Grok"]
    end

    Funnel["Tailscale Funnel<br/>public HTTPS &mdash; zero open ports"]
    Auth["Secret-path auth<br/>fails closed without token"]
    Server["server.py :8000<br/>Starlette + Uvicorn"]

    subgraph tools["12 Tools"]
        direction LR
        Exec["Execute<br/>PowerShell<br/>Python<br/>Task Runner"]
        Data["Data<br/>Files<br/>Git<br/>SQLite Memory"]
        Web["Web<br/>Search / Fetch<br/>Playwright<br/>Chrome CDP"]
        Sys["System<br/>Info<br/>Screenshot<br/>Tailscale"]
        Meta["Meta<br/>Think<br/>Self-improve"]
    end

    GPT & Grok -- "HTTPS JSON-RPC" --> Funnel
    Funnel -- "WireGuard encrypted" --> Auth
    Auth --> Server
    Server --> Exec & Data & Web & Sys & Meta

    classDef client fill:#4285f4,stroke:#4285f4,color:#fff
    classDef network fill:#34a853,stroke:#34a853,color:#fff
    classDef security fill:#f9ab00,stroke:#f9ab00,color:#1a1a1a
    classDef core fill:#ea4335,stroke:#ea4335,color:#fff
    classDef tool fill:#5f6368,stroke:#5f6368,color:#fff

    class GPT,Grok client
    class Funnel network
    class Auth security
    class Server core
    class Exec,Data,Web,Sys,Meta tool
```

## Requirements

- Windows 10/11 (tools use PowerShell; `screenshot` and `browser` are Windows-tested)
- Python 3.11+
- [Tailscale](https://tailscale.com/) with [Funnel](https://tailscale.com/kb/1223/funnel) enabled

## Quick Start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate a secret token (server refuses to start without it)
python -c "import secrets; open('.funnel_token','w').write(secrets.token_hex(16))"

# 3. Start the server
python server.py

# 4. Expose it via Tailscale Funnel
tailscale up
tailscale funnel 8000
```

Connect GPT/Grok to: `https://YOUR-MACHINE.tailXXXXX.ts.net/YOUR-TOKEN/`

## Tools

| Tool | Description |
|------|-------------|
| `run_command` | PowerShell with timeout, cwd, tail_lines |
| `run_python` | Python 3 execution with cwd |
| `file` | Read, write, append, list, tree, search, grep, stat, move, str_replace |
| `web` | Google/Bing/Wikipedia/DuckDuckGo search, fetch, Playwright browse, GitHub API |
| `git` | Status, log, diff, branch, show, remote, blame |
| `system` | Info, processes, ports, services, env, kill, screenshot (mss) |
| `memory` | Persistent SQLite key-value — `skill:*`, `insight:*`, `fact:*` |
| `self` | Review, backup, patch, heal, restart — **the server can improve itself** |
| `task` | Multi-step autonomous runner with stop-on-error |
| `browser` | Chrome CDP bridge — uses your logged-in sessions |
| `tailscale` | Status, IP, ping |

## Self-Improving

AI assistants can upgrade the server at runtime:

```
backup → file(str_replace) → heal → restart
```

No human needed. The `self` tool chain lets AI patch `server.py`, verify syntax, and restart — all from within a conversation.

## Security

| Layer | Mechanism |
|-------|-----------|
| Transport | Tailscale WireGuard — end-to-end encrypted |
| Access | Tailscale Funnel — no open ports, no NAT config |
| Auth | Secret-path token in URL — 32-char random string |
| Fail-safe | Server returns 503 if `.funnel_token` is missing |
| Git-safe | `.gitignore` blocks the token file |

> ⚠️ The full URL (domain + token) is the only key. Share it carefully.
>
> Because the token lives in the URL path, it can end up in places URLs normally do:
> browser history, access logs, and referrer headers. Treat the URL like a password —
> don't paste it in public, and rotate it by regenerating `.funnel_token` and restarting.

## License

MIT
