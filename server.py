"""
funnel-mcp — HTTP server for AI assistants.
Expose your workstation via Tailscale Funnel with secret-path auth.
No public HTTPS needed — all traffic through Tailscale.
"""

import json, os, subprocess, urllib.request, urllib.error, urllib.parse, datetime, sqlite3, sys, re, ssl, base64
from collections import Counter
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.requests import Request
import uvicorn

import platform
if platform.system() == "Windows":
    MCP_DIR = os.environ.get("MCP_DIR", os.path.expanduser("~/mcp-server"))
    MEMORY_DB = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "pc_tools_memory.db")
else:
    MCP_DIR = os.path.expanduser("~/mcp-server")
    MEMORY_DB = os.path.join(os.environ.get("TMPDIR", "/tmp"), "pc_tools_memory.db")
EXPERIENCE_LOG = os.path.join(MCP_DIR, ".mcp_experience.jsonl")
MAX_RETRIES = 5
RETRY_TOOL_CHAIN = ("run_command", "run_python", "file", "system", "web", "self")

# Helpers

GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"

def _truncate_output(text: str, max_chars: int = 50000, tail_lines: int = 0) -> str:
    if tail_lines and tail_lines > 0:
        lines = text.splitlines()
        if len(lines) > tail_lines:
            return "[...%d lines omitted...]\n" % (len(lines) - tail_lines) + "\n".join(lines[-tail_lines:])
    if len(text) > max_chars:
        half = max_chars // 2
        return text[:half] + "\n[...truncated %d chars...]\n" % (len(text) - max_chars) + text[-half:]
    return text

def _ps(cmd, timeout=30, cwd=None):
    try:
        r = subprocess.run(
            ["powershell", "-Command", cmd], capture_output=True, text=True,
            timeout=timeout, cwd=cwd or MCP_DIR,
        )
        parts = [s for s in [r.stdout.strip(), r.stderr.strip()] if s]
        parts.append("[EXIT %d]" % r.returncode)
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "Timeout (%ds)" % timeout
    except Exception as e:
        return "Error: %s" % e

def _git(cmd, repo, timeout=20):
    if not os.path.isfile(GIT_EXE):
        return "Error: git not found at %s" % GIT_EXE
    if not os.path.isdir(repo):
        return "Error: not a directory: %s" % repo
    try:
        r = subprocess.run(
            [GIT_EXE, "-C", repo] + cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        out = _truncate_output((r.stdout or "") + ("\n" + r.stderr if r.stderr else ""), 40000)
        return out + ("\n[EXIT %d]" % r.returncode)
    except subprocess.TimeoutExpired:
        return "Timeout (%ds)" % timeout
    except Exception as e:
        return "Error: %s" % e

def _file_tree(root: str, depth: int = 3, max_items: int = 250) -> str:
    lines = ["TREE %s (depth=%d)" % (root, depth)]
    count = 0
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        lvl = 0 if rel == "." else rel.count(os.sep) + 1
        if lvl > depth:
            dirnames[:] = []
            continue
        indent = "  " * lvl
        if rel != ".":
            lines.append("%s[%s]" % (indent, os.path.basename(dirpath)))
        sub_indent = "  " * (lvl + 1)
        for fn in sorted(filenames):
            if count >= max_items:
                lines.append("%s...truncated (%d+ items)" % (sub_indent, max_items))
                return "\n".join(lines)
            fp = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            lines.append("%s%s (%d)" % (sub_indent, fn, sz))
            count += 1
    return "\n".join(lines) if len(lines) > 1 else "(empty)"

def _file_grep(path: str, pattern: str, max_matches: int = 40) -> str:
    rx = re.compile(pattern)
    hits = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if rx.search(line):
                hits.append("%d: %s" % (i, line.rstrip()[:200]))
                if len(hits) >= max_matches:
                    hits.append("...truncated")
                    break
    return "\n".join(hits) if hits else "(no matches)"

def _init_memory():
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, value TEXT, ts TEXT)")
    conn.commit()
    conn.close()

DEPRECATED_SKILLS = ("skill:bot-log-check", "skill:organize-folder", "skill:discover-first", "skill:improve-mcp")

DEFAULT_SKILLS = {
    "skill:agent-loop": (
        "Every task: DISCOVER with tools -> PLAN -> ACT -> VERIFY -> LEARN. "
        "Store reusable wins as memory(skill:TOPIC). Continue after each tool call; never stop silently."
    ),
    "skill:discover": (
        "Explore before asking: combine system, file(list/search), run_command until you understand the situation. "
        "Ask the user only if discovery finds zero candidates or action is irreversible."
    ),
    "skill:self-improve": (
        "On failure or capability gap: self(review) + memory(read). "
        "Upgrade tools via self(improve,backup) -> file(str_replace, C:\\MCP\\server.py) -> self(heal) -> restart. "
        "Never rewrite whole files. Log lessons via self(log) and memory(skill:)."
    ),
    "skill:ask-last": (
        "Question the user only as last resort: empty discovery, permanent delete/format, or true tie between equals."
    ),
    "skill:retry-policy": (
        "On failure: think(), then switch tool (not same command). "
        "Chain: run_command->run_python->file->system->web. Max 5 tries then report. Always continue — never silent."
    ),
    "skill:github": (
        "GitHub profiles/repos: web(action=github, user=USERNAME, gh_action=profile|repos|readme|repo, repo=NAME). "
        "GitHub profile access via API. Use web(action=github, user=USERNAME)."
    ),
}

def _seed_default_skills():
    try:
        _init_memory()
        conn = sqlite3.connect(MEMORY_DB)
        ts = datetime.datetime.now().isoformat()
        for k in DEPRECATED_SKILLS:
            conn.execute("DELETE FROM memory WHERE key=?", (k,))
        for k, v in DEFAULT_SKILLS.items():
            conn.execute("INSERT OR REPLACE INTO memory VALUES (?,?,?)", (k, v, ts))
        conn.commit()
        conn.close()
    except Exception:
        pass

def _load_skills(limit: int = 5) -> list:
    try:
        _init_memory()
        conn = sqlite3.connect(MEMORY_DB)
        cur = conn.execute(
            "SELECT key, value FROM memory WHERE key LIKE 'skill:%' ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []

# ═══════════════════════════════════════════
# COGNITIVE FRAMEWORK — injected via instructions
# ═══════════════════════════════════════════

def _build_context(include_memory: bool = False, include_skills: bool = False) -> str:
    parts = []

    parts.append(
        "=== AGENT SOP ===\\n"
        "Autonomous PC operator. Act first, ask last. DISCOVER -> PLAN -> ACT -> VERIFY -> LEARN.\\n"
        "On failure: switch tool (not same command). Max %d tries then report."
        % MAX_RETRIES
    )

    parts.append(
        "=== IMPROVE ===\\n"
        "self(review) -> memory(store skill:) -> self(improve,guide) -> "
        "backup -> file(str_replace) -> self(heal) -> restart."
    )

    if include_skills:
        rows = _load_skills()
        if rows:
            parts.append("=== SKILLS ===")
            for k, v in rows:
                parts.append("%s: %s" % (k, v[:160]))

    if include_memory:
        try:
            _init_memory()
            conn = sqlite3.connect(MEMORY_DB)
            cur = conn.execute("SELECT key, value, ts FROM memory ORDER BY ts DESC LIMIT 10")
            rows = cur.fetchall()
            conn.close()
            if rows:
                parts.append("=== PERSISTENT MEMORY ===")
                for k, v, ts in rows:
                    parts.append(f"[{ts[:16]}] {k}: {v[:250]}")
        except Exception:
            pass
        try:
            if os.path.exists(EXPERIENCE_LOG):
                with open(EXPERIENCE_LOG, encoding="utf-8") as f:
                    entries = [json.loads(l) for l in f if l.strip()]
                if entries:
                    parts.append("\n=== RECENT LEARNING ===")
                    for e in entries[-5:]:
                        tag = "OK" if e.get("success") else "FAIL"
                        parts.append(f"[{tag}] {e.get('topic','?')}: {e.get('content','')[:200]}")
        except Exception:
            pass

    parts.append(
        "=== TOOLS ===\\n"
        "think | run_command | run_python | file | web | git | system | memory | self | task | browser | tailscale"
    )

    return "\n".join(parts)

# Web search

def _search_wikipedia(query, limit, ctx):
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": limit
    })
    req = urllib.request.Request(url, headers={"User-Agent": "funnel-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    results = data.get("query", {}).get("search", [])
    if not results: return ""
    parts = [f'Wikipedia: "{query}"']
    for i, r in enumerate(results[:limit], 1):
        snippet = re.sub(r'<[^>]+>', '', r.get("snippet", "")).strip()
        parts.append(f"\n{i}. {r['title']}")
        parts.append(f"   https://en.wikipedia.org/wiki/{r['title'].replace(' ', '_')}")
        if snippet: parts.append(f"   {snippet[:200]}...")
    return "\n".join(parts)

def _search_ddg_api(query, limit, ctx):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
        "q": query, "format": "json", "no_html": "1", "skip_disambig": "1"
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        raw = r.read().decode("utf-8", errors="replace")
    if not raw.strip().startswith("{"): return ""
    data = json.loads(raw)
    parts = [f'Web: "{query}"']
    if data.get("AbstractText"):
        parts.append(f"\n{data['AbstractText']}")
        if data.get("AbstractURL"): parts.append(f"Source: {data['AbstractURL']}")
    for t in data.get("RelatedTopics", [])[:limit]:
        if isinstance(t, dict) and t.get("Text"):
            parts.append(f"\n  - {t['Text'][:200]}")
    return "\n".join(parts) if len(parts) > 1 else ""

def _search_google(query, limit, ctx):
    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query, "hl": "en"})
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        html = r.read().decode("utf-8", errors="replace")
    blocks = re.findall(r'<h3[^>]*>(.*?)</h3>', html, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'role="heading"[^>]*>(.*?)</', html, re.DOTALL)
    urls = re.findall(r'href="(https?://[^"]+)"', html)
    real_urls = [u for u in urls if "google.com" not in u and "gstatic" not in u][:limit]
    if blocks:
        parts = [f'Google: "{query}"']
        for i, block in enumerate(blocks[:limit]):
            title = re.sub(r'<[^>]+>', '', block).strip()
            link = real_urls[i] if i < len(real_urls) else ""
            parts.append(f"\n{i+1}. {title}")
            if link: parts.append(f"   {link}")
        return "\n".join(parts)
    return ""

def _search_bing_rss(query, limit, ctx):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) funnel-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
        xml = r.read().decode("utf-8", errors="replace")
    items = re.findall(r"<item>\s*<title>(.*?)</title>\s*<link>(.*?)</link>", xml, re.DOTALL)
    if not items:
        return ""
    parts = [f'Bing: "{query}"']
    for i, (title, link) in enumerate(items[:limit], 1):
        title = re.sub(r"<[^>]+>", "", title).strip()
        link = link.strip()
        parts.append(f"\n{i}. {title}")
        if link: parts.append(f"   {link}")
    return "\n".join(parts)

def _search_pypi(query, limit, ctx):
    skip = {"pypi", "pip", "latest", "version", "package", "the", "a", "on", "install", "python"}
    words = re.findall(r"[a-zA-Z0-9_-]+", query)
    for pkg in [w for w in words if w.lower() not in skip][:4]:
        try:
            api = "https://pypi.org/pypi/%s/json" % urllib.parse.quote(pkg)
            req = urllib.request.Request(api, headers={"User-Agent": "funnel-mcp/1.0"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            info = data.get("info", {})
            summary = (info.get("summary") or "")[:300]
            return (
                'PyPI: "%s"\nVersion: %s\nSummary: %s\nURL: https://pypi.org/project/%s/'
                % (pkg, info.get("version", "?"), summary, pkg)
            )
        except Exception:
            continue
    return ""

def _pypi_query(query: str) -> bool:
    q = query.lower()
    return any(x in q for x in ("pypi", "pip install", "pip ", "latest version", "package version", "module version"))

def _web_search(query, limit=5):
    if not query or not query.strip():
        return 'Search needs a non-empty query.'
    ctx = ssl._create_unverified_context()
    errors = []
    if _pypi_query(query):
        try:
            result = _search_pypi(query, limit, ctx)
            if result:
                return result
        except Exception as e:
            errors.append("PyPI:%s" % type(e).__name__)
    engines = [
        ("Bing", _search_bing_rss),
        ("Wikipedia", _search_wikipedia),
        ("DDG", _search_ddg_api),
        ("Google", _search_google),
    ]
    for name, fn in engines:
        try:
            result = fn(query, limit, ctx)
            if result and len(result) > 30:
                return result
        except Exception as e:
            errors.append("%s:%s" % (name, type(e).__name__))
            continue
    detail = (" (%s)" % ", ".join(errors)) if errors else ""
    return 'Search unavailable: "%s"%s. Use web(action="fetch", url=...) instead.' % (query, detail)

def _html_to_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()

def _truncate_web(text: str, limit: int = 15000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[...%d total chars]" % len(text)

def _web_fetch(url: str, timeout: int = 15, mode: str = "auto") -> str:
    if not url:
        return "Error: url required"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 funnel-mcp/1.0",
        "Accept": "text/html,application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=min(timeout, 60), context=ctx) as r:
        raw = r.read()
        ctype = (r.headers.get("Content-Type") or "").lower()
        final_url = r.geturl()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    use_json = mode == "json" or "application/json" in ctype or url.rstrip("/").endswith(".json")
    use_text = mode == "text"
    if use_json:
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except Exception:
            pass
    elif use_text or (mode == "auto" and "text/html" in ctype):
        if mode == "text" or (mode == "auto" and "<html" in content[:2000].lower()):
            content = _html_to_text(content)
    header = "URL: %s\nMode: %s\n\n" % (final_url, "json" if use_json else ("text" if use_text else "html"))
    return _truncate_web(header + content)

def _web_browse(url: str, timeout: int = 30, wait_until: str = "domcontentloaded",
                extract: str = "text", selector: str = "", screenshot: bool = False) -> str:
    if not url:
        return "Error: url required"
    wait_until = wait_until if wait_until in ("load", "domcontentloaded", "networkidle", "commit") else "domcontentloaded"
    extract = extract if extract in ("text", "html") else "text"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Error: playwright not installed. pip install playwright && python -m playwright install chromium"
    lines = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=min(timeout, 120) * 1000, wait_until=wait_until)
            if selector:
                el = page.query_selector(selector)
                if not el:
                    browser.close()
                    return "Error: selector not found: %s" % selector
                content = el.inner_html() if extract == "html" else el.inner_text()
            else:
                content = page.content() if extract == "html" else page.inner_text("body")
            lines.append("Title: %s" % page.title())
            lines.append("URL: %s" % page.url)
            lines.append("Extract: %s%s" % (extract, (" selector=" + selector) if selector else ""))
            if screenshot:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                shot = os.path.join(MCP_DIR, "browse_%s.png" % ts)
                page.screenshot(path=shot, full_page=False)
                lines.append("Screenshot: %s" % shot)
            browser.close()
        lines.append("")
        if extract == "html":
            lines.append(_truncate_web(content))
        else:
            lines.append(_truncate_web(re.sub(r"\s+", " ", content).strip()))
        return "\n".join(lines)
    except Exception as e:
        return "Error: %s" % e

def _parse_github_user(value: str) -> str:
    v = (value or "").strip().rstrip("/")
    if "github.com/" in v:
        parts = v.split("github.com/")[-1].split("/")
        return parts[0] or v
    return v

def _github_api_get(path: str, timeout: int = 15) -> dict:
    url = "https://api.github.com" + path
    req = urllib.request.Request(url, headers={
        "User-Agent": "funnel-mcp/1.0",
        "Accept": "application/vnd.github+json",
    })
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))

def _web_github(user: str = "", repo: str = "", gh_action: str = "profile", n: int = 20) -> str:
    user = _parse_github_user(user or repo or "")
    if not user:
        return "Error: user required (username or github profile URL)"
    gh_action = (gh_action or "profile").lower()
    try:
        if gh_action == "profile":
            d = _github_api_get("/users/%s" % urllib.parse.quote(user))
            return (
                "GitHub profile: %s (%s)\nURL: %s\nBio: %s\nPublic repos: %s | Followers: %s\nCreated: %s"
                % (d.get("login"), d.get("name") or "-", d.get("html_url"), d.get("bio") or "-",
                   d.get("public_repos", 0), d.get("followers", 0), (d.get("created_at") or "")[:10])
            )
        if gh_action == "repos":
            repos = _github_api_get("/users/%s/repos?per_page=%d&sort=updated" % (urllib.parse.quote(user), min(n, 100)))
            if not repos:
                return "No public repos for %s" % user
            lines = ["Repos for %s (%d):" % (user, len(repos))]
            for i, r in enumerate(repos, 1):
                lines.append(
                    "%d. %s — %s\n   %s | ★%s | updated %s"
                    % (i, r.get("name"), r.get("description") or "(no description)",
                       r.get("html_url"), r.get("stargazers_count", 0),
                       (r.get("updated_at") or "")[:10])
                )
            return "\n".join(lines)
        if gh_action == "repo":
            if not repo:
                return "Error: repo name required"
            d = _github_api_get("/repos/%s/%s" % (urllib.parse.quote(user), urllib.parse.quote(repo)))
            return (
                "Repo: %s/%s\nURL: %s\nDescription: %s\nStars: %s | Language: %s | Updated: %s\nDefault branch: %s"
                % (user, repo, d.get("html_url"), d.get("description") or "-",
                   d.get("stargazers_count", 0), d.get("language") or "-",
                   (d.get("updated_at") or "")[:10], d.get("default_branch") or "-")
            )
        if gh_action == "readme":
            if not repo:
                return "Error: repo name required for readme"
            d = _github_api_get("/repos/%s/%s/readme" % (urllib.parse.quote(user), urllib.parse.quote(repo)))
            content = d.get("content", "")
            if d.get("encoding") == "base64":
                content = base64.b64decode(content).decode("utf-8", errors="replace")
            return "README %s/%s\nURL: %s\n\n%s" % (user, repo, d.get("html_url", ""), _truncate_web(content, 12000))
        return "github gh_action: profile | repos | repo | readme"
    except urllib.error.HTTPError as e:
        return "GitHub HTTP %d: %s" % (e.code, e.reason)
    except Exception as e:
        return "Error: %s" % e

# Tool definitions

TOOLS = [
    {
        "name": "think",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Reasoning scratchpad. Log plan, hypothesis, next tool. Optional tags for categorization.",
        "inputSchema": {"type": "object", "properties": {
            "thought": {"type": "string"},
            "tags": {"type": "string", "description": "Comma-separated tags e.g. retry,debug,plan"}
        }, "required": ["thought"]}
    },
    {
        "name": "run_command",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "PowerShell. Supports cwd, tail_lines for log output. Switch tool on failure.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "default": 60},
            "cwd": {"type": "string", "description": "Working directory"},
            "tail_lines": {"type": "integer", "description": "Return only last N lines of output"}
        }, "required": ["command"]}
    },
    {
        "name": "run_python",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Python 3 execution. cwd + tail_lines supported. Fallback when PowerShell fails.",
        "inputSchema": {"type": "object", "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer", "default": 60},
            "cwd": {"type": "string"},
            "tail_lines": {"type": "integer"}
        }, "required": ["code"]}
    },
    {
        "name": "file",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "File I/O. action: read|write|append|list|tree|search|grep|stat|move|str_replace. read supports offset/limit lines.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "read|write|append|list|tree|search|grep|stat|move|str_replace"},
            "path": {"type": "string"},
            "dest": {"type": "string", "description": "move destination"},
            "content": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "pattern": {"type": "string"},
            "file_filter": {"type": "string"},
            "offset": {"type": "integer", "description": "read: start line (1-based)"},
            "limit": {"type": "integer", "description": "read: max lines"},
            "depth": {"type": "integer", "default": 3, "description": "tree: max depth"}
        }, "required": ["action"]}
    },
    {
        "name": "web",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Web: search|fetch|browse|github. For GitHub use github action (API, reliable). Tool may appear as hybridsapiens.web in ChatGPT — call it directly.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "search | fetch | browse | github"},
            "query": {"type": "string", "description": "Search query (search)"},
            "url": {"type": "string", "description": "URL (fetch/browse)"},
            "user": {"type": "string", "description": "github: username or profile URL"},
            "repo": {"type": "string", "description": "github: repo name for readme/repo"},
            "gh_action": {"type": "string", "description": "github: profile|repos|repo|readme"},
            "mode": {"type": "string", "description": "fetch: auto|html|text|json"},
            "extract": {"type": "string", "description": "browse: text|html"},
            "selector": {"type": "string", "description": "browse: CSS selector"},
            "wait_until": {"type": "string", "description": "browse: load|domcontentloaded|networkidle"},
            "screenshot": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 5},
            "timeout": {"type": "integer", "default": 15}
        }, "required": ["action"]}
    },
    {
        "name": "git",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Git. action: status|log|diff|branch|show|remote|blame. diff supports staged=true.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "status|log|diff|branch|show|remote|blame"},
            "path": {"type": "string", "default": "C:\\MCP"},
            "n": {"type": "integer", "default": 15},
            "file": {"type": "string", "description": "File path for blame/show/diff"},
            "staged": {"type": "boolean", "default": False},
            "ref": {"type": "string", "description": "show: commit hash"}
        }, "required": ["action"]}
    },
    {
        "name": "system",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "System. action: info|processes|ports|services|env|kill|screenshot. Discover running apps/ports before acting.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "info|processes|ports|services|env|kill|screenshot"},
            "filter": {"type": "string"},
            "pid": {"type": "integer"},
            "name": {"type": "string", "description": "env var name or service filter"}
        }, "required": ["action"]}
    },
    {
        "name": "memory",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Persistent memory SQLite. action: store|read|search|delete. Use key prefix skill: for learned patterns.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "store|read|search|delete"},
            "key": {"type": "string"},
            "value": {"type": "string"},
            "pattern": {"type": "string", "description": "search: SQL LIKE pattern e.g. skill:%"}
        }, "required": ["action"]}
    },
    {
        "name": "self",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Self-improvement. action: log|review|improve|insight|heal. improve: read|backup|patch|guide|restart. Call improve,guide when unsure how to proceed or upgrade capabilities.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "log | review | improve | insight | heal"},
            "topic": {"type": "string", "description": "Topic for log"},
            "content": {"type": "string", "description": "Content for log"},
            "success": {"type": "boolean", "default": True, "description": "Was action successful?"},
            "self_action": {"type": "string", "description": "For improve: read | backup | patch | guide | restart"},
            "old_string": {"type": "string", "description": "For improve patch"},
            "new_string": {"type": "string", "description": "For improve patch"},
            "replace_all": {"type": "boolean", "default": False}
        }, "required": ["action"]}
    },
    {
        "name": "task",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Multi-step runner. action: run|note. run chains tools with stop_on_error. note: add|list|done|clear.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "run|note"},
            "steps": {"type": "array", "items": {"type": "object"}},
            "stop_on_error": {"type": "boolean", "default": True},
            "note_action": {"type": "string", "description": "add|list|done|clear"},
            "task_name": {"type": "string"},
            "detail": {"type": "string"}
        }, "required": ["action"]}
    },
    {
        "name": "browser",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Control the already-open Chrome via CDP (127.0.0.1:9222), using YOUR logged-in sessions (Fiverr etc) - no password needed. Actions: get_tabs (list tabs+ids); navigate (url, opens/goes to page); evaluate (expression=JS, returns value - most powerful, use React setters for inputs); text (visible text); get_html (full DOM); click (expression=CSS selector); type (text=string to type into focused element); screenshot (saves PNG to disk); scroll; reload; wait (url=seconds); cookies. Pick a tab with target_id from get_tabs (defaults to first tab).",
        "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "target_id": {"type": "string", "description": "tab id prefix from get_tabs"}, "url": {"type": "string"}, "expression": {"type": "string", "description": "JS for evaluate, or CSS selector for click"}, "text": {"type": "string", "description": "text to type for action=type"}}, "required": ["action"]}
    },
    {
        "name": "tailscale",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "description": "Tailscale. action: status|ip|ping (default status).",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "status|ip|ping", "default": "status"},
            "host": {"type": "string", "description": "ping target hostname"}
        }, "required": []}
    },
]

# Tool handler

def handle(name, args):
    if name == "think":
        thought = args.get("thought", "")
        tags = args.get("tags", "reasoning")
        try:
            with open(EXPERIENCE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.datetime.now().isoformat(), "topic": "thinking",
                    "content": thought[:800], "success": True, "tags": tags,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return "Thought recorded (%d chars, tags=%s). Continue." % (len(thought), tags)

    elif name == "run_command":
        out = _ps(
            args.get("command", ""),
            min(args.get("timeout", 60), 120),
            cwd=args.get("cwd") or MCP_DIR,
        )
        return _truncate_output(out, tail_lines=args.get("tail_lines", 0) or 0)

    elif name == "run_python":
        sc = os.path.join(MCP_DIR, ".mcp_tmp.py")
        cwd = args.get("cwd") or MCP_DIR
        try:
            with open(sc, "w", encoding="utf-8") as f:
                f.write(args["code"])
            r = subprocess.run(
                ["python", sc], capture_output=True, text=True,
                timeout=min(args.get("timeout", 60), 120), cwd=cwd,
            )
            parts = [s for s in [r.stdout.strip(), "[STDERR] " + r.stderr.strip() if r.stderr.strip() else ""] if s]
            parts.append("[EXIT %d]" % r.returncode)
            out = "\n".join(parts)
            return _truncate_output(out, tail_lines=args.get("tail_lines", 0) or 0)
        except subprocess.TimeoutExpired:
            return "Timeout (%ds)" % args.get("timeout", 30)
        except Exception as e:
            return "Error: %s" % e
        finally:
            try:
                os.remove(sc)
            except Exception:
                pass

    elif name == "file":
        a = args.get("action", "")
        p = args.get("path", "")
        if a == "read":
            try:
                offset = int(args.get("offset", 0) or 0)
                limit = int(args.get("limit", 0) or 0)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if offset or limit:
                        lines = f.readlines()
                        start = max(offset - 1, 0)
                        end = start + limit if limit else len(lines)
                        c = "".join("%d|%s" % (start + i + 1, ln) for i, ln in enumerate(lines[start:end]))
                    else:
                        c = f.read()
                return _truncate_output(c, 200000)
            except Exception as e:
                return "Error: %s" % e
        elif a == "write":
            c = args.get("content", "")
            try:
                d = os.path.dirname(p)
                if d: os.makedirs(d, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f: f.write(c)
                return "OK - %d chars written to %s" % (len(c), p)
            except Exception as e: return "Error: %s" % e
        elif a == "append":
            c = args.get("content", "")
            try:
                d = os.path.dirname(p)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    f.write(c)
                return "OK - appended %d chars to %s" % (len(c), p)
            except Exception as e:
                return "Error: %s" % e
        elif a == "stat":
            try:
                st = os.stat(p)
                return (
                    "Path: %s\nType: %s\nSize: %d\nModified: %s"
                    % (p, "dir" if os.path.isdir(p) else "file", st.st_size,
                       datetime.datetime.fromtimestamp(st.st_mtime).isoformat()[:19])
                )
            except Exception as e:
                return "Error: %s" % e
        elif a == "tree":
            try:
                return _file_tree(p or MCP_DIR, int(args.get("depth", 3) or 3))
            except Exception as e:
                return "Error: %s" % e
        elif a == "grep":
            pat = args.get("pattern", "")
            if not p or not pat:
                return "Error: path and pattern required"
            try:
                return _file_grep(p, pat)
            except Exception as e:
                return "Error: %s" % e
        elif a == "move":
            dest = args.get("dest", "")
            if not p or not dest:
                return "Error: path and dest required"
            try:
                import shutil
                shutil.move(p, dest)
                return "OK - moved %s -> %s" % (p, dest)
            except Exception as e:
                return "Error: %s" % e
        elif a == "list":
            try:
                items = []
                for e in sorted(os.listdir(p or MCP_DIR)):
                    fp = os.path.join(p or MCP_DIR, e)
                    items.append("[DIR] %s" % e if os.path.isdir(fp) else "      %s (%d)" % (e, os.path.getsize(fp)))
                return "\n".join(items) if items else "(empty)"
            except Exception as e: return "Error: %s" % e
        elif a == "search":
            pat = args.get("pattern", "")
            pth = args.get("path", MCP_DIR)
            ff = args.get("file_filter", "")
            cmd = "Get-ChildItem '%s' -Recurse -Depth 5 -EA 0" % pth
            if ff: cmd += " -Filter '%s'" % ff
            cmd += " | Select-String -Pattern '%s' -EA 0 | Select -First 30 | ForEach-Object { $_.Filename + ':' + $_.LineNumber + ' ' + $_.Line.Trim() }" % pat
            return _ps(cmd, 30)[:8000]
        elif a == "str_replace":
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            if not p or not old:
                return "Error: path and old_string required"
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                count = content.count(old)
                if count == 0:
                    return "Error: old_string not found in %s" % p
                if count > 1 and not args.get("replace_all", False):
                    return "Error: old_string found %d times — make it unique or set replace_all=true" % count
                if args.get("replace_all", False):
                    content = content.replace(old, new)
                    n = count
                else:
                    content = content.replace(old, new, 1)
                    n = 1
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                return "OK - replaced %d occurrence(s) in %s" % (n, p)
            except Exception as e:
                return "Error: %s" % e
        return "file: read|write|append|list|tree|search|grep|stat|move|str_replace"

    elif name == "web":
        a = args.get("action", "")
        if a == "search":
            return _web_search(args.get("query", ""), args.get("limit", 5))
        elif a == "fetch":
            try:
                return _web_fetch(args.get("url", ""), args.get("timeout", 15), args.get("mode", "auto"))
            except urllib.error.HTTPError as e:
                return "HTTP %d: %s" % (e.code, e.reason)
            except Exception as e:
                return "Error: %s" % e
        elif a == "browse":
            t = args.get("timeout", 30)
            return _web_browse(
                args.get("url", ""),
                timeout=t,
                wait_until=args.get("wait_until", "domcontentloaded"),
                extract=args.get("extract", "text"),
                selector=args.get("selector", ""),
                screenshot=args.get("screenshot", False),
            )
        elif a == "github":
            return _web_github(
                user=args.get("user", ""),
                repo=args.get("repo", ""),
                gh_action=args.get("gh_action", "profile"),
                n=args.get("limit", 20),
            )
        return "web actions: search | fetch | browse | github"

    elif name == "git":
        a = args.get("action", "")
        p = args.get("path", MCP_DIR)
        n = args.get("n", 15)
        fp = args.get("file", "")
        if a == "status":
            return _git(["status", "-sb"], p)
        elif a == "log":
            cmd = ["log", "--oneline", "-n", str(n)]
            if fp:
                cmd += ["--", fp]
            return _git(cmd, p)
        elif a == "diff":
            cmd = ["diff", "--cached"] if args.get("staged") else ["diff"]
            if fp:
                cmd += ["--", fp]
            return _git(cmd, p)
        elif a == "branch":
            return _git(["branch", "-vv"], p)
        elif a == "remote":
            return _git(["remote", "-v"], p)
        elif a == "show":
            ref = args.get("ref", "HEAD")
            return _git(["show", "--stat", "--oneline", ref], p)
        elif a == "blame":
            if not fp:
                return "Error: file required for blame"
            return _git(["blame", "-n", fp], p, timeout=30)
        return "git: status|log|diff|branch|show|remote|blame"

    elif name == "system":
        a = args.get("action", "")
        if a == "info":
            parts = []
            for c in [
                "Get-ComputerInfo | Select OsName,OsArchitecture,WindowsVersion,@{N='RAM_GB';E={[math]::Round($_.CsTotalPhysicalMemory/1GB,1)}} | Format-List",
                "Get-CimInstance Win32_LogicalDisk -Filter DriveType=3 | Select DeviceID,@{N='GB';E={[math]::Round($_.Size/1GB,1)}},@{N='Free';E={[math]::Round($_.FreeSpace/1GB,1)}} | Format-Table -AutoSize",
                "$u=(Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; 'Uptime: ' + $u.Days + 'd ' + $u.Hours + 'h ' + $u.Minutes + 'm'"
            ]:
                try:
                    r = subprocess.run(["powershell", "-Command", c], capture_output=True, text=True, timeout=15)
                    if r.stdout.strip(): parts.append(r.stdout.strip())
                except: pass
            return "\n".join(parts)
        elif a == "processes":
            f = args.get("filter", "")
            cmd = "Get-Process *%s* -EA 0 | Select Name,Id,CPU,@{N='MB';E={[math]::Round($_.WorkingSet/1MB,1)}} | Sort CPU -Descending | Select -First 50 | Format-Table -AutoSize -Wrap" % f
            return _ps(cmd, 15)
        elif a == "ports":
            return _ps(
                "Get-NetTCPConnection -State Listen -EA 0 | Select LocalAddress,LocalPort,OwningProcess,@{N='Proc';E={(Get-Process -Id $_.OwningProcess -EA 0).Name}} | Sort LocalPort | Format-Table -AutoSize",
                20,
            )
        elif a == "services":
            f = args.get("filter", args.get("name", ""))
            cmd = "Get-Service *%s* -EA 0 | Where Status -eq Running | Select Name,Status,DisplayName | Format-Table -AutoSize" % f
            return _ps(cmd, 20)
        elif a == "env":
            nm = args.get("name", args.get("filter", ""))
            if nm:
                return _ps("$v=[Environment]::GetEnvironmentVariable('%s','Machine'); $u=[Environment]::GetEnvironmentVariable('%s','User'); 'Machine: ' + $v; 'User: ' + $u" % (nm, nm), 10)
            return _ps("Get-ChildItem Env: | Sort-Object Name | Select -First 60 Name,Value | Format-Table -AutoSize", 15)
        elif a == "kill":
            return _ps("Stop-Process -Id %d -Force" % args["pid"], 10)
        elif a == "screenshot":
            try:
                import mss
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                out = os.path.join(MCP_DIR, "screen_%s.png" % ts)
                with mss.mss() as s: s.shot(mon=1, output=out)
                return "Screenshot: %s" % out
            except Exception as e: return "Screenshot error: %s" % e
        return "system: info|processes|ports|services|env|kill|screenshot"

    elif name == "memory":
        a = args.get("action", "")
        k = args.get("key", "")
        v = args.get("value", "")
        _init_memory()
        if a == "store":
            try:
                conn = sqlite3.connect(MEMORY_DB)
                conn.execute("INSERT OR REPLACE INTO memory VALUES (?,?,?)", (k, v, datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Stored: %s" % k
            except Exception as e: return "Error: %s" % e
        elif a == "read":
            try:
                conn = sqlite3.connect(MEMORY_DB)
                if k:
                    cur = conn.execute("SELECT value, ts FROM memory WHERE key=?", (k,))
                    row = cur.fetchone()
                    conn.close()
                    return "[%s] %s" % (row[1][:16], row[0]) if row else "Not found: %s" % k
                cur = conn.execute("SELECT key, LENGTH(value), ts FROM memory ORDER BY ts DESC")
                rows = cur.fetchall()
                conn.close()
                if rows:
                    return "\n".join("  %-30s %6dc [%s]" % (r[0], r[1], r[2][:16]) for r in rows)
                return "(empty)"
            except Exception as e:
                return "Error: %s" % e
        elif a == "search":
            pat = args.get("pattern", args.get("key", "skill:%"))
            try:
                conn = sqlite3.connect(MEMORY_DB)
                cur = conn.execute(
                    "SELECT key, substr(value,1,200), ts FROM memory WHERE key LIKE ? ORDER BY ts DESC LIMIT 20",
                    (pat,),
                )
                rows = cur.fetchall()
                conn.close()
                if not rows:
                    return "No matches for: %s" % pat
                return "\n".join("[%s] %s: %s" % (r[2][:16], r[0], r[1]) for r in rows)
            except Exception as e:
                return "Error: %s" % e
        elif a == "delete":
            if not k:
                return "Error: key required"
            try:
                conn = sqlite3.connect(MEMORY_DB)
                conn.execute("DELETE FROM memory WHERE key=?", (k,))
                conn.commit()
                conn.close()
                return "Deleted: %s" % k
            except Exception as e:
                return "Error: %s" % e
        return "memory: store|read|search|delete"

    elif name == "self":
        a = args.get("action", "")
        if a == "log":
            e = {"ts": datetime.datetime.now().isoformat(), "topic": args.get("topic",""), "content": args.get("content",""), "success": args.get("success",True), "tags": args.get("tags","")}
            try:
                with open(EXPERIENCE_LOG, "a", encoding="utf-8") as f: f.write(json.dumps(e, ensure_ascii=False) + "\n")
                return "Logged: %s" % e["topic"]
            except Exception as ex: return "Error: %s" % ex
        elif a == "review":
            try:
                if not os.path.exists(EXPERIENCE_LOG): return "No log yet"
                with open(EXPERIENCE_LOG, encoding="utf-8") as f: entries = [json.loads(l) for l in f if l.strip()]
                lim = args.get("limit", 20)
                entries = entries[-lim:]
                s = sum(1 for e in entries if e.get("success"))
                lines = ["Self-Review: %d entries (%d/%d S/F)" % (len(entries), s, len(entries)-s)]
                # Tag frequency
                tags = Counter()
                for e in entries:
                    for t in e.get("tags","").split(","):
                        if t.strip(): tags[t.strip()] += 1
                if tags: lines.append("Tags: %s" % ", ".join("%s(%d)" % (t,c) for t,c in tags.most_common(5)))
                # Recent
                lines.append("Recent:")
                for e in reversed(entries[-5:]):
                    lines.append("  %s %s — %s" % ("OK" if e.get("success") else "FAIL", e.get("topic","?"), e.get("content","")[:100]))
                return "\n".join(lines)
            except Exception as ex: return "Error: %s" % ex
        elif a == "improve":
            sp = os.path.join(MCP_DIR, "server.py")
            sa = args.get("self_action", "")
            if sa == "read":
                with open(sp, encoding="utf-8") as f: full = f.read()
                i = full.find("TOOLS = [")
                return full[i:] if i >= 0 else full[:2000]
            elif sa == "backup":
                bp = "%s.bak.%s" % (sp, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                try:
                    with open(sp, encoding="utf-8") as src, open(bp, "w", encoding="utf-8") as dst: dst.write(src.read())
                    return "Backup: %s" % bp
                except Exception as e: return "Error: %s" % e
            elif sa == "patch":
                old = args.get("old_string", "")
                new = args.get("new_string", "")
                if not old:
                    return "old_string required. Workflow: backup -> patch or file(str_replace) -> heal -> restart"
                return handle("file", {
                    "action": "str_replace",
                    "path": sp,
                    "old_string": old,
                    "new_string": new,
                    "replace_all": args.get("replace_all", False),
                })
            elif sa == "guide":
                return (
                    "AGENT LOOP (any task):\n"
                    "1. DISCOVER — explore with system/file/run_command; do not ask user yet\n"
                    "2. PLAN — pick smallest next action\n"
                    "3. ACT — chain tools until done\n"
                    "4. VERIFY — check outputs\n"
                    "5. LEARN — memory(store, skill:TOPIC) if reusable; self(log) on failure\n"
                    "RETRY RULE: no user reply until success. Each retry = DIFFERENT tool (cmd->python->file->system->web->self). Max %d.\n"
                    "UPGRADE TOOLS: self(improve,backup) -> file(str_replace) -> self(heal) -> restart\n"
                    "Ask user only after %d failures or when discovery is empty or action is irreversible."
                    % (MAX_RETRIES, MAX_RETRIES)
                )
            elif sa == "restart":
                subprocess.Popen([sys.executable, sp], cwd=os.environ.get("TEMP","C:\\Windows\\Temp"))
                return "Restarting..."
            return "self improve actions: read | backup | patch | guide | restart"
        elif a == "insight":
            try:
                _init_memory()
                conn = sqlite3.connect(MEMORY_DB)
                mc = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]; conn.close()
                lines = ["Memory: %d entries" % mc]
            except: lines = ["Memory: ?"]
            try:
                if os.path.exists(EXPERIENCE_LOG):
                    with open(EXPERIENCE_LOG, encoding="utf-8") as f: entries = [json.loads(l) for l in f if l.strip()]
                    s = sum(1 for e in entries if e.get("success"))
                    lines.append("Experience: %d entries (%d/%d S/F)" % (len(entries), s, len(entries)-s))
                    # Skills count
                    _init_memory()
                    conn = sqlite3.connect(MEMORY_DB)
                    skills = conn.execute("SELECT COUNT(*) FROM memory WHERE key LIKE 'skill:%'").fetchone()[0]
                    conn.close()
                    lines.append("Skills saved: %d" % skills)
            except: pass
            return "\n".join(lines)
        elif a == "heal":
            lines = ["=== SERVER HEALTH ==="]
            lines.append("Tools: %d (v10.1)" % len(TOOLS))
            try:
                lines.append("Web search: %s" % ("OK" if len(_web_search("test", 1)) > 20 else "check"))
            except Exception:
                lines.append("Web search: ?")
            try:
                from playwright.sync_api import sync_playwright  # noqa: F401
                lines.append("Playwright: installed")
            except ImportError:
                lines.append("Playwright: missing")
            try:
                _init_memory()
                conn = sqlite3.connect(MEMORY_DB)
                mc = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
                conn.close()
                lines.append("Memory entries: %d" % mc)
            except Exception as e: lines.append("Memory: ERROR %s" % e)
            try:
                if os.path.exists(EXPERIENCE_LOG):
                    lines.append("Experience log: %d bytes" % os.path.getsize(EXPERIENCE_LOG))
                else: lines.append("Experience log: (none)")
            except: pass
            try:
                sp = os.path.join(MCP_DIR, "server.py")
                with open(sp, encoding="utf-8") as f: compile(f.read(), sp, "exec")
                lines.append("server.py syntax: OK")
            except SyntaxError as se: lines.append("server.py syntax: FAIL line %d — %s" % (se.lineno, se.msg))
            try:
                r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
                port8000 = [l for l in r.stdout.split("\n") if ":8000" in l and "LISTENING" in l]
                lines.append("Port 8000: %s" % ("LISTENING" if port8000 else "NOT listening"))
            except: lines.append("Port 8000: ?")
            return "\n".join(lines)
        return "self actions: log | review | improve | insight | heal"

    elif name == "task":
        a = args.get("action", "")
        if a == "run":
            steps = args.get("steps", [])
            if not steps:
                return "No steps"
            stop = args.get("stop_on_error", True)
            lines = []
            for i, s in enumerate(steps, 1):
                t = s.get("tool", s.get("name", ""))
                p = s.get("params", s.get("arguments", {}))
                try:
                    res = handle(t, p)
                    preview = _truncate_output(str(res), 500)
                    lines.append("Step %d [%s]: %s" % (i, t, preview))
                    if stop and _is_tool_failure(str(res), t):
                        lines.append("Stopped at step %d (failure)" % i)
                        break
                except Exception as ex:
                    lines.append("Step %d [%s]: ERROR %s" % (i, t, ex))
                    if stop:
                        break
            return "\n".join(lines)
        elif a == "note":
            na = args.get("note_action", "")
            tn = args.get("task_name", "")
            d = args.get("detail", "")
            _init_memory()
            conn = sqlite3.connect(MEMORY_DB)
            cur = conn.execute("SELECT value FROM memory WHERE key='tasks'")
            row = cur.fetchone()
            tasks = json.loads(row[0]) if row else []
            if na == "add":
                tasks.append({"task": tn, "detail": d, "ts": datetime.datetime.now().isoformat()})
                conn.execute("INSERT OR REPLACE INTO memory VALUES ('tasks',?,?)", (json.dumps(tasks), datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Task added: %s" % tn
            elif na == "list":
                conn.close()
                if not tasks: return "(no tasks)"
                return "\n".join("  %d. %s — %s" % (i+1, x["task"], x.get("detail","")) for i, x in enumerate(tasks))
            elif na == "done":
                tasks = [x for x in tasks if x.get("task") != tn]
                conn.execute("INSERT OR REPLACE INTO memory VALUES ('tasks',?,?)", (json.dumps(tasks), datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Task done: %s" % tn
            elif na == "clear":
                conn.execute("INSERT OR REPLACE INTO memory VALUES ('tasks',?,?)", ("[]", datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "All task notes cleared"
            conn.close()
            return "note: add|list|done|clear"
        return "task: run|note"

    # Chrome CDP bridge
    elif name == "browser":
        import urllib.request as _u, urllib.error, socket, base64 as _b64, os as _os, struct, time as _t
        from urllib.parse import urlparse
        CDP = "http://127.0.0.1:9222"
        a = args.get("action", "")
        def _cdp_json(pth):
            with _u.urlopen(CDP + pth, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        class _WS:
            def __init__(self, ws_url, timeout=20):
                u = urlparse(ws_url); self.to = timeout
                self.s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
                key = _b64.b64encode(_os.urandom(16)).decode()
                hs = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (u.path, u.hostname, u.port or 80, key))
                self.s.sendall(hs.encode()); self.s.recv(4096); self._id = 0
            def _send(self, method, params=None):
                self._id += 1
                data = json.dumps({"id": self._id, "method": method, "params": params or {}}).encode("utf-8")
                hdr = bytearray([0x81]); l = len(data); mask = _os.urandom(4)
                if l < 126: hdr.append(0x80 | l)
                elif l < 65536: hdr.append(0x80 | 126); hdr += struct.pack(">H", l)
                else: hdr.append(0x80 | 127); hdr += struct.pack(">Q", l)
                hdr += mask
                self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                return self._id
            def _recv_frame(self):
                b1 = self.s.recv(1)[0]; fin = b1 & 0x80
                b2 = self.s.recv(1)[0]; l = b2 & 0x7f
                if l == 126: l = struct.unpack(">H", self.s.recv(2))[0]
                elif l == 127: l = struct.unpack(">Q", self.s.recv(8))[0]
                buf = b""
                while len(buf) < l: buf += self.s.recv(min(65536, l - len(buf)))
                return fin, buf
            def _recv_msg(self):
                fin, buf = self._recv_frame()
                while not fin:
                    f2, b2 = self._recv_frame(); buf += b2; fin = f2
                return buf.decode("utf-8", "replace")
            def call(self, method, params=None):
                mid = self._send(method, params)
                deadline = _t.time() + self.to
                while _t.time() < deadline:
                    try: j = json.loads(self._recv_msg())
                    except: continue
                    if j.get("id") == mid: return j
                return None
            def close(self):
                try: self.s.close()
                except: pass
        def _tab(tid=None):
            tabs = [t for t in _cdp_json("/json") if t.get("type") == "page"]
            if tid:
                return next((t for t in tabs if t.get("id","").startswith(tid)), None)
            return tabs[0] if tabs else None
        try:
            if a == "get_tabs":
                out = []
                for t in _cdp_json("/json"):
                    if t.get("type") == "page":
                        out.append("[%s] %s | %s" % (t.get("id","")[:8], t.get("title","")[:50], t.get("url","")[:70]))
                return "\n".join(out) if out else "no page tabs"
            tid = args.get("target_id", "")
            tab = _tab(tid)
            if a == "navigate" and not tid and args.get("url"):
                _cdp_json("/json/new?" + args["url"]); return "opened new tab -> %s" % args["url"]
            if not tab: return "no target tab (pass target_id from get_tabs)"
            ws = _WS(tab["webSocketDebuggerUrl"])
            try:
                if a == "navigate":
                    ws.call("Page.enable"); ws.call("Page.navigate", {"url": args.get("url","")})
                    _t.sleep(1); return "navigated -> %s" % args.get("url","")
                elif a == "reload":
                    ws.call("Page.enable"); ws.call("Page.reload"); return "reloaded"
                elif a in ("evaluate", "text", "get_html"):
                    if a == "text": expr = "document.body.innerText"
                    elif a == "get_html": expr = "document.documentElement.outerHTML"
                    else:
                        expr = args.get("expression","")
                        if not expr: return "expression required"
                    res = ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                    if not res: return "no response (timeout)"
                    r = res.get("result", {})
                    if r.get("result",{}).get("value") is not None or "value" in r.get("result",{}):
                        return str(r["result"]["value"])[:8000]
                    if "exceptionDetails" in r: return "JS ERROR: %s" % json.dumps(r["exceptionDetails"])[:500]
                    return json.dumps(r)[:2000]
                elif a == "click":
                    sel = args.get("expression","")  # CSS selector
                    if not sel: return "expression=CSS selector required"
                    js = "(function(){var e=document.querySelector(%s);if(!e)return null;var r=e.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()" % json.dumps(sel)
                    res = ws.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
                    val = res.get("result",{}).get("result",{}).get("value") if res else None
                    if not val: return "selector not found: %s" % sel
                    pos = json.loads(val)
                    for typ in ("mousePressed","mouseReleased"):
                        ws.call("Input.dispatchMouseEvent", {"type": typ, "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
                    return "clicked %s at (%d,%d)" % (sel, pos["x"], pos["y"])
                elif a == "type":
                    txt = args.get("url","") or args.get("expression","")  # reuse field for text
                    txt = args.get("text", txt)
                    for ch in txt:
                        ws.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
                        ws.call("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch})
                    return "typed %d chars" % len(txt)
                elif a == "screenshot":
                    ws.call("Page.enable")
                    try: ws.call("Page.bringToFront")
                    except: pass
                    res = ws.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
                    data = res.get("result",{}).get("data") if res else None
                    if not data: return "screenshot failed"
                    d = os.path.join(MCP_DIR, "screenshots"); os.makedirs(d, exist_ok=True)
                    fn = os.path.join(d, "shot_%s.png" % datetime.datetime.now().strftime("%H%M%S"))
                    with open(fn, "wb") as f: f.write(_b64.b64decode(data))
                    return "screenshot saved: %s (%d bytes)" % (fn, os.path.getsize(fn))
                elif a == "wait":
                    _t.sleep(min(float(args.get("url","2") or 2), 10)); return "waited"
                elif a == "scroll":
                    ws.call("Runtime.evaluate", {"expression": "window.scrollBy(0, window.innerHeight)"}); return "scrolled"
                elif a == "cookies":
                    res = ws.call("Network.getCookies")
                    cs = res.get("result",{}).get("cookies",[]) if res else []
                    return "\n".join("%s=%s (%s)" % (c.get("name"), str(c.get("value"))[:20], c.get("domain")) for c in cs[:40]) or "no cookies"
                return "browser actions: get_tabs|navigate|evaluate|text|get_html|click|type|screenshot|wait|scroll|reload|cookies"
            finally:
                ws.close()
        except urllib.error.URLError as e:
            return "CDP not reachable (Chrome --remote-debugging-port=9222?): %s" % e
        except Exception as e:
            return "browser error: %s" % e

    elif name == "tailscale":
        ta = args.get("action", "status")
        try:
            if ta == "ip":
                r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
                return (r.stdout or r.stderr).strip() or "No tailscale IP"
            elif ta == "ping":
                host = args.get("host", "")
                if not host:
                    return "Error: host required for ping"
                r = subprocess.run(["tailscale", "ping", host], capture_output=True, text=True, timeout=15)
                return _truncate_output((r.stdout or "") + ("\n" + r.stderr if r.stderr else ""), 5000)
            r = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=10)
            out = r.stdout.strip()
            if out:
                lines = [l for l in out.split("\n") if l.strip() and not l.startswith("#")]
                if lines:
                    return "Tailscale:\n" + "\n".join(lines)
            return out or r.stderr.strip() or "Tailscale not available"
        except Exception as e:
            return "Error: %s" % e

    return "Unknown tool: %s" % name

# MCP transport (HTTP SSE)

class MCPSession:
    def __init__(self):
        self.initialized = False
        self.attempts = 0
        self.failed_tools = []
        self.failed_sigs = []

def _call_signature(tool: str, args: dict) -> str:
    if tool == "run_command":
        return "run_command:%s" % args.get("command", "")[:300]
    if tool == "run_python":
        return "run_python:%s" % args.get("code", "")[:300]
    if tool == "file":
        return "file:%s:%s:%s" % (
            args.get("action", ""),
            args.get("path", "")[:120],
            args.get("pattern", "")[:80],
        )
    if tool == "system":
        return "system:%s:%s" % (args.get("action", ""), args.get("filter", ""))
    if tool == "web":
        return "web:%s:%s" % (args.get("action", ""), args.get("query", args.get("url", ""))[:120])
    if tool == "self":
        return "self:%s:%s" % (args.get("action", ""), args.get("self_action", ""))
    return tool

def _next_tool(session: MCPSession, current: str) -> str:
    tried = set(session.failed_tools + [current])
    for t in RETRY_TOOL_CHAIN:
        if t not in tried:
            return t
    for t in RETRY_TOOL_CHAIN:
        if t != current:
            return t
    return "run_python"

def _is_tool_failure(text: str, tool: str) -> bool:
    if tool in ("think", "memory", "tailscale", "web", "git"):
        return False
    t = text.strip()
    if not t:
        return True
    if t.startswith("Error:"):
        return True
    if t.startswith("Timeout ("):
        return True
    if "SYNTAX ERROR" in t:
        return True
    m = re.search(r"\[EXIT (\d+)\]", t)
    if m and int(m.group(1)) != 0:
        return True
    if tool == "file" and "not found" in t.lower() and t.startswith("Error"):
        return True
    return False

def _retry_wrap(text: str, session: MCPSession, tool: str, sig: str = "") -> str:
    if not _is_tool_failure(text, tool):
        session.attempts = 0
        session.failed_tools = []
        session.failed_sigs = []
        return text
    same = sig and sig in session.failed_sigs
    if tool not in session.failed_tools:
        session.failed_tools.append(tool)
    if sig and sig not in session.failed_sigs:
        session.failed_sigs.append(sig)
    session.attempts += 1
    n, mx = session.attempts, MAX_RETRIES
    nxt = _next_tool(session, tool)
    if n < mx:
        hint = "[attempt %d/%d — next try tool: %s" % (n, mx, nxt)
        if same:
            hint += ", same command failed — switch method"
        hint += "]"
        return "%s\n%s" % (text, hint)
    return (
        "%s\n[failed %d/%d — report to user: methods tried %s, errors above]"
        % (text, n, mx, ", ".join(session.failed_tools) or tool)
    )

sessions = {}
sid_counter = 0

def new_sid():
    global sid_counter
    sid_counter += 1
    return "s%d" % sid_counter

def sse(data):
    return "event: message\ndata: %s\n\n" % json.dumps(data, ensure_ascii=False, default=str)

async def mcp_handler(request: Request):
    sid = request.headers.get("mcp-session-id", "")
    if not sid: sid = new_sid()
    if sid not in sessions: sessions[sid] = MCPSession()

    # CORS preflight
    if request.method == "OPTIONS":
        return JSONResponse({}, status_code=204, headers={
            "mcp-session-id": sid,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, mcp-session-id, Authorization",
        })

    if request.method == "GET":
        return JSONResponse({"status": "ok", "server": "funnel-mcp", "version": "1.0"}, status_code=200,
            headers={"mcp-session-id": sid})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        sessions[sid].initialized = True
        sessions[sid].attempts = 0
        sessions[sid].failed_tools = []
        sessions[sid].failed_sigs = []
        try:
            cname = (params.get("clientInfo", {}) or {}).get("name", "")
        except Exception:
            cname = ""
        cname_l = str(cname).lower()
        is_claude = "claude" in cname_l
        is_gpt = any(x in cname_l for x in ("chatgpt", "openai", "gpt"))
        ctx = _build_context(include_memory=is_claude, include_skills=is_gpt or not is_claude)
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}, "experimental": {}},
            "serverInfo": {"name": "funnel-mcp", "version": "1.0"},
            "instructions": ctx
        }}
        return StreamingResponse(iter([sse(resp)]), media_type="text/event-stream",
            headers={"mcp-session-id": sid, "Cache-Control": "no-cache, no-store, must-revalidate",
                     "Access-Control-Allow-Origin": "*"})
    elif method == "notifications/initialized":
        return JSONResponse("", status_code=202)
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        return StreamingResponse(iter([sse(resp)]), media_type="text/event-stream",
            headers={"mcp-session-id": sid, "Cache-Control": "no-cache",
                     "Access-Control-Allow-Origin": "*"})
    elif method == "tools/call":
        n = params.get("name", "")
        a = params.get("arguments", {})
        try:
            sig = _call_signature(n, a)
            result = handle(n, a)
            text = str(result)
            if n != "think":
                text = _retry_wrap(text, sessions[sid], n, sig)
            if len(text) > 200000:
                text = text[:200000] + "\n[...truncated]"
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as ex:
            text = _retry_wrap("Error: %s" % ex, sessions[sid], n)
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}]}}
        return StreamingResponse(iter([sse(resp)]), media_type="text/event-stream",
            headers={"mcp-session-id": sid, "Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
    else:
        resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}}
        return StreamingResponse(iter([sse(resp)]), media_type="text/event-stream",
            headers={"mcp-session-id": sid, "Access-Control-Allow-Origin": "*"})

routes = [Route("/", endpoint=mcp_handler, methods=["GET", "POST", "OPTIONS"]), Route("/sse", endpoint=mcp_handler, methods=["GET", "POST", "OPTIONS"])]

# Secret path auth — token from .funnel_token (keep out of git)
FUNNEL_TOKEN_FILE = os.path.join(MCP_DIR, ".funnel_token")

def _load_funnel_token():
    try:
        with open(FUNNEL_TOKEN_FILE, encoding="utf-8") as f:
            t = f.read().strip()
        return t if len(t) >= 16 else None
    except Exception:
        return None

FUNNEL_TOKEN = _load_funnel_token()

class SecretPathAuth:
    def __init__(self, inner):
        self.inner = inner
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        if not FUNNEL_TOKEN:
            resp = JSONResponse({"error": "auth not configured"}, status_code=503)
            await resp(scope, receive, send)
            return
        prefix = "/" + FUNNEL_TOKEN
        path = scope.get("path", "")
        if path == prefix or path.startswith(prefix + "/"):
            scope = dict(scope)
            newp = path[len(prefix):] or "/"
            scope["path"] = newp
            scope["raw_path"] = newp.encode()
            await self.inner(scope, receive, send)
        else:
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)

app = SecretPathAuth(Starlette(routes=routes))

if __name__ == "__main__":
    _init_memory()
    _seed_default_skills()
    uvicorn.run(app, host="127.0.0.1", port=8000)
