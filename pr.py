"""Show each workspace branch's pull request (Forgejo or GitHub) in the Herdr sidebar.

A background watcher resolves every workspace's Git checkout from its focused
pane, looks up the pull request whose head is that branch, and reports it as
the workspace token `$pr`: "#N open", "#N approved", "#N changes requested",
"#N draft", "#N merged" or "#N closed". Add "$pr" to [ui.sidebar.spaces] rows
to show it. The `open` action opens that pull request in the browser.

Forgejo deletes merged branches and rewrites head.ref to refs/pull/N/head, so
its PRs are matched on head.label (which keeps the branch name), then head.sha.
GitHub PRs are found with the `head=owner:branch` filter, in the remote's repo
and, for a fork, its parent.

Forgejo hosts are codeberg.org plus the ones forgejo-cli (fj) is logged in to;
GitHub hosts are github.com plus the ones gh is logged in to. SSH host aliases
in remotes are resolved through `ssh -G`.

Settings (all optional): $HERDR_PLUGIN_CONFIG_DIR/pr.conf, key=value
  forgejo  = host,...   extra Forgejo hosts
  github   = host,...   extra GitHub hosts (Enterprise)
  interval = 60         seconds between refreshes
Tokens: FORGEJO_TOKEN, else fj's keys.json (expired OAuth logins are renewed by
running `fj whoami`); GH_TOKEN/GITHUB_TOKEN, else `gh auth token`.
"""

import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-pr")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(
    "~/.config/herdr/plugins/config/twiking.pr")
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
# One watcher per Herdr session (each has its own socket).
SESSION = hashlib.sha1(os.path.realpath(SOCKET_PATH).encode()).hexdigest()[:12]

SOURCE = "twiking.pr"
TOKEN = "pr"
API_CACHE = 20          # seconds a PR lookup is reused, so focus bursts cost nothing
ANON_GITHUB_CACHE = 300  # without a token GitHub allows 60 requests an hour
RECONNECT = 30          # keep running this long while the Herdr server is unreachable
MAX_LOG = 256 * 1024    # watch log is cut back to empty above this size
DEFAULT_FORGEJO = {"codeberg.org"}
DEFAULT_GITHUB = {"github.com"}
HOST_ALIASES = {"ssh.github.com": "github.com"}  # GitHub's SSH-over-443 endpoint


class HerdrError(Exception):
    pass


class RateLimited(Exception):
    pass


def log(msg):
    print(f"{time.strftime('%F %T')} pr: {msg}", file=sys.stderr, flush=True)


# --- Logins -------------------------------------------------------------------

def fj_keys_path():
    """forgejo-cli's keys.json: XDG data dir on Linux, Application Support on macOS."""
    home = os.path.expanduser("~")
    for path in (
        os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local/share"),
                     "forgejo-cli", "keys.json"),
        os.path.join(home, "Library/Application Support/forgejo-cli.forgejo-cli/keys.json"),
        os.path.join(home, "Library/Application Support/Cyborus.forgejo-cli/keys.json"),
    ):
        if os.path.exists(path):
            return path
    return None


def fj_logins():
    """fj host key (host[:port][/path]) -> login info."""
    path = fj_keys_path()
    try:
        with open(path) as f:
            return dict(json.load(f).get("hosts") or {})
    except (TypeError, OSError, ValueError, AttributeError):
        return {}


def gh_hosts():
    """Hosts the GitHub CLI (gh) is logged in to: top-level keys of hosts.yml."""
    base = os.environ.get("GH_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "gh")
    try:
        with open(os.path.join(base, "hosts.yml")) as f:
            return {m.group(1).strip().strip("\"'") for m in
                    (re.match(r"^([^\s#-][^:]*):\s*$", line) for line in f) if m}
    except OSError:
        return set()


def bare_host(key):
    """'Example.com:3000/forgejo' -> 'example.com', as it appears in a remote URL."""
    return key.split("/")[0].split(":")[0].lower()


def settings():
    """({remote host: (Forge class, web base)}, refresh interval)."""
    conf = {"forgejo": "", "github": "", "interval": "60"}
    try:
        with open(os.path.join(CONFIG_DIR, "pr.conf")) as f:
            for line in f:
                key, _, value = line.partition("#")[0].partition("=")
                if key.strip() in conf:
                    conf[key.strip()] = value.strip()
    except OSError:
        pass

    def keys(v):
        return {h.strip().rstrip("/") for h in v.split(",") if h.strip()}
    forges = {}
    for key in DEFAULT_GITHUB | gh_hosts() | keys(conf["github"]):
        forges[bare_host(key)] = (GitHub, key.lower())
    for key in DEFAULT_FORGEJO | set(fj_logins()) | keys(conf["forgejo"]):
        forges[bare_host(key)] = (Forgejo, key.lower())
    interval = int(conf["interval"]) if conf["interval"].isdigit() else 60
    return forges, max(interval, 15)


# --- Herdr --------------------------------------------------------------------

def call(method, **params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps({"id": SOURCE, "method": method, "params": params}) + "\n").encode())
        reply = json.loads(s.makefile().readline())
    if "error" in reply:
        raise HerdrError(f"{method}: {reply['error']}")
    return reply["result"]


def workspace_dirs():
    """workspace_id -> cwd candidates, focused pane of the active tab first."""
    snap = call("session.snapshot")["snapshot"]
    focused = {l["tab_id"]: l.get("focused_pane_id") for l in snap.get("layouts", [])}
    panes = {}
    for p in snap.get("panes", []):
        panes.setdefault(p["workspace_id"], []).append(p)
    out = {}
    for ws in snap.get("workspaces", []):
        first = focused.get(ws.get("active_tab_id"))
        ps = sorted(panes.get(ws["workspace_id"], []), key=lambda p: p["pane_id"] != first)
        out[ws["workspace_id"]] = [p["cwd"] for p in ps if p.get("cwd")]
    return out


def report(workspace_id, value, ttl_ms):
    call("workspace.report_metadata", workspace_id=workspace_id, source=SOURCE,
         tokens={TOKEN: value}, seq=time.time_ns() // 1_000_000,
         **({"ttl_ms": ttl_ms} if value else {}))


def notify(title, body=None):
    try:
        call("notification.show", title=title, body=body, sound="none")
    except (OSError, HerdrError, ValueError):
        log(f"{title}: {body}")


# --- Git ----------------------------------------------------------------------

def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else ""


REMOTE_RE = re.compile(r"^(?:([a-z+]+)://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", re.I)
_ssh_hosts = {}


def ssh_hostname(alias):
    """The real host behind an ~/.ssh/config alias (`ssh -G` reads config, never connects)."""
    if alias not in _ssh_hosts:
        try:
            r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=5)
            m = re.search(r"^hostname (\S+)$", r.stdout, re.M)
            _ssh_hosts[alias] = m.group(1).lower() if r.returncode == 0 and m else alias
        except (OSError, subprocess.TimeoutExpired):
            _ssh_hosts[alias] = alias
    return _ssh_hosts[alias]


def checkout(cwd, forges):
    """(forge, owner/repo, branch, head sha) for a checkout on a known forge, else None."""
    branch = git(cwd, "branch", "--show-current")
    if not branch:
        return None
    remote = git(cwd, "config", f"branch.{branch}.remote") or "origin"
    m = REMOTE_RE.match(git(cwd, "remote", "get-url", remote))
    if not m:
        return None
    scheme, host, path = (m.group(1) or "ssh").lower(), m.group(2).lower(), m.group(3)
    if host not in forges and not scheme.startswith("http"):
        host = ssh_hostname(host)
    host = HOST_ALIASES.get(host, host)
    if host not in forges:
        return None
    cls, key = forges[host]
    if cls is Forgejo and "/" in key:  # Forgejo served under a subpath
        path = path.removeprefix(key.split("/", 1)[1].strip("/") + "/")
    return forge(cls, key), path, branch, git(cwd, "rev-parse", "HEAD")


def first_checkout(dirs, forges):
    return next((c for c in (checkout(d, forges) for d in dirs) if c), None)


# --- Forges -------------------------------------------------------------------

_forges = {}


def forge(cls, key):
    """One instance per host, so caches survive across refreshes."""
    if (cls, key) not in _forges:
        _forges[(cls, key)] = cls(key)
    return _forges[(cls, key)]


def verdict(reviews):
    """"approved", "changes requested", or None from each reviewer's latest verdict.

    Comments, pending reviews and review requests don't change a verdict, and
    dismissed reviews no longer count. One "request changes" outranks approvals.
    """
    latest = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        state = {"CHANGES_REQUESTED": "REQUEST_CHANGES"}.get(r.get("state"), r.get("state"))
        if not r.get("dismissed") and state in ("APPROVED", "REQUEST_CHANGES"):
            latest[(r.get("user") or {}).get("login")] = state
    states = set(latest.values())
    return ("changes requested" if "REQUEST_CHANGES" in states
            else "approved" if "APPROVED" in states else None)


class Forge:
    name = "forge"
    api = ""

    def __init__(self, key):
        self.key = key  # host[:port][/path], as configured
        self.cache = {}  # url -> (fetched_at, etag, data)
        self.verdicts = {}  # (repo, number) -> (updated_at, verdict)
        self.blocked_until = 0

    def token(self):
        return None

    def max_age(self):
        return API_CACHE

    def on_unauthorized(self):
        """Try to get a fresh token after a 401; True if a retry is worthwhile."""
        return False

    def get(self, path, max_age=None, retry=True):
        url = f"{self.api}/{path}"
        hit = self.cache.get(url)
        if hit and time.monotonic() - hit[0] < (self.max_age() if max_age is None else max_age):
            return hit[2]
        if time.time() < self.blocked_until:
            raise RateLimited(f"{self.name} {self.key}: rate limited until "
                              f"{time.strftime('%H:%M', time.localtime(self.blocked_until))}")
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "herdr-pr"})
        tok = self.token()
        if tok:
            req.add_header("Authorization", f"token {tok}")
        if hit and hit[1]:
            req.add_header("If-None-Match", hit[1])  # a 304 is free on GitHub
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                etag, data = r.headers.get("ETag"), json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 304 and hit:
                self.cache[url] = (time.monotonic(), hit[1], hit[2])
                return hit[2]
            if e.code in (403, 429) and (e.headers.get("Retry-After")
                                          or e.headers.get("X-RateLimit-Remaining") == "0"):
                wait = int(e.headers.get("Retry-After") or 0)
                reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                self.blocked_until = max(time.time() + wait, reset, time.time() + 60)
                raise RateLimited(f"{self.name} {self.key}: rate limited") from e
            if e.code == 401 and retry and self.on_unauthorized():
                return self.get(path, max_age, retry=False)
            raise
        self.cache[url] = (time.monotonic(), etag, data)
        return data

    def describe(self, repo, pr):
        if pr.get("merged") or pr.get("merged_at"):
            state = "merged"
        elif pr.get("state") == "closed":
            state = "closed"
        elif pr.get("draft") or re.match(r"^\s*(wip|draft)\b", pr.get("title", ""), re.I):
            state = "draft"
        else:
            key = (repo, pr["number"])
            hit = self.verdicts.get(key)
            if not hit or hit[0] != pr.get("updated_at"):
                hit = (pr.get("updated_at"), verdict(self.reviews(repo, pr["number"])))
                self.verdicts[key] = hit
            state = hit[1] or "open"
        return f"#{pr['number']} {state}"


class Forgejo(Forge):
    name = "Forgejo"

    def __init__(self, key):
        super().__init__(key)
        self.api = f"https://{key}/api/v1"
        self.last_renewal = 0

    def login(self):
        logins = fj_logins()
        return logins.get(self.key) or next(
            (v for k, v in logins.items() if k.lower() == self.key), None) or {}

    def token(self):
        return os.environ.get("FORGEJO_TOKEN") or self.login().get("token")

    def on_unauthorized(self):
        # fj renews an expired OAuth token (and saves it) whenever it talks to
        # the host. Run it from the state dir so it can't pick up a repo remote.
        if (os.environ.get("FORGEJO_TOKEN") or self.login().get("type") != "OAuth"
                or not shutil.which("fj") or time.monotonic() - self.last_renewal < 300):
            return False
        self.last_renewal = time.monotonic()
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            r = subprocess.run(["fj", "--host", self.key, "whoami"], cwd=STATE_DIR,
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            log(f"fj could not renew the {self.key} login; run `fj auth login`")
        return r.returncode == 0

    def find(self, repo, branch, sha):
        repos = [repo]
        # A fork's PRs live in its parent; repo metadata barely changes.
        parent = (self.get(f"repos/{repo}", 3600).get("parent") or {}).get("full_name")
        if parent:
            repos.append(parent)
        # `limit` is ignored unless `page` is sent too; 50 is Forgejo's default max page.
        pulls = [(r, p) for r in repos
                 for p in self.get(f"repos/{r}/pulls?state=all&sort=recentupdate&page=1&limit=50")]

        def ours(p):
            # Fork PR labels are the bare branch name, so check whose branch it is.
            head_repo = ((p.get("head") or {}).get("repo") or {}).get("full_name")
            return not head_repo or head_repo.lower() == repo.lower()
        candidates = [(r, p) for r, p in pulls if ours(p) and branch in (
            (p.get("head") or {}).get("label"), (p.get("head") or {}).get("ref"))]
        if not candidates and sha:
            candidates = [(r, p) for r, p in pulls if (p.get("head") or {}).get("sha") == sha]
        return candidates

    def reviews(self, repo, number):
        return self.get(f"repos/{repo}/pulls/{number}/reviews?page=1&limit=50", 0)


class GitHub(Forge):
    name = "GitHub"

    def __init__(self, key):
        super().__init__(key)
        self.api = "https://api.github.com" if key == "github.com" else f"https://{key}/api/v3"
        self._token = None

    def token(self):
        env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if env:
            return env
        if self._token is None:
            self._token = ""
            if shutil.which("gh"):
                try:
                    r = subprocess.run(["gh", "auth", "token", "--hostname", self.key],
                                       capture_output=True, text=True, timeout=10)
                    self._token = r.stdout.strip() if r.returncode == 0 else ""
                except (OSError, subprocess.TimeoutExpired):
                    pass
        return self._token or None

    def max_age(self):
        return API_CACHE if self.token() else ANON_GITHUB_CACHE

    def on_unauthorized(self):
        self._token = None  # gh may have been re-authenticated since
        return True

    def find(self, repo, branch, sha):
        owner = repo.split("/")[0]
        repos = [repo]
        # A fork's PRs usually target its parent; repo metadata barely changes.
        parent = (self.get(f"repos/{repo}", 3600).get("parent") or {}).get("full_name")
        if parent:
            repos.append(parent)
        head = urllib.parse.quote(f"{owner}:{branch}", safe=":")
        return [(r, p) for r in repos
                for p in self.get(f"repos/{r}/pulls?state=all&head={head}&per_page=20")]

    def reviews(self, repo, number):
        return self.get(f"repos/{repo}/pulls/{number}/reviews?per_page=100", 0)


class Remembered:
    """Merged/closed results per branch, for PRs that age out of the recent page.

    Shared by every session's watcher, so writes merge under a lock.
    """

    path = os.path.join(STATE_DIR, "remembered.json")

    def __init__(self):
        self.data = self.load()

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def get(self, key, sha):
        hit = self.data.get(key)
        return (hit["value"], hit.get("url")) if hit and hit.get("sha") == sha else (None, None)

    def put(self, key, sha, value, url):
        entry = {"sha": sha, "value": value, "url": url}
        if self.data.get(key) == entry:
            return
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.data = self.load()
            self.data[key] = entry
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)


def find_pr(fg, repo, branch, sha, remembered):
    """(label, url) of the branch's pull request, else (None, None)."""
    key = f"{fg.key}/{repo}:{branch}"
    try:
        candidates = fg.find(repo, branch, sha)
    except urllib.error.HTTPError as e:
        if e.code != 404:  # a deleted or inaccessible repo just has no PR
            raise
        candidates = []
    if not candidates:
        return remembered.get(key, sha)
    # A reused branch name can have several PRs; an open one wins, then the newest.
    pr_repo, pr = max(candidates, key=lambda c: (c[1].get("state") == "open", c[1].get("created_at") or ""))
    value, url = fg.describe(pr_repo, pr), pr.get("html_url")
    if pr.get("state") == "closed":
        remembered.put(key, sha, value, url)
    return value, url


# --- Commands -----------------------------------------------------------------

def refresh(last, warned):
    forges, interval = settings()
    ttl_ms = min(interval * 5 * 1000, 86_400_000)
    remembered = Remembered()
    for ws_id, dirs in workspace_dirs().items():
        value = None
        try:
            co = first_checkout(dirs, forges)
            if co:
                value, _ = find_pr(*co, remembered)
        except RateLimited as e:
            if str(e) not in warned:
                warned.add(str(e))
                log(str(e))
            continue  # keep the last value until its TTL runs out
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            log(f"{ws_id}: {e}")
            continue
        if value or last.get(ws_id):
            report(ws_id, value, ttl_ms)
        last[ws_id] = value


def watch():
    lock = open(os.path.join(STATE_DIR, f"watch-{SESSION}.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # already running for this session
    lock.truncate(0)
    lock.write(str(os.getpid()))
    lock.flush()

    # SIGUSR1 from `start` cuts the wait short via a self-pipe.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGUSR1, lambda *_: None)
    last, warned = {}, set()
    unreachable_since = None
    while True:
        try:
            refresh(last, warned)
            unreachable_since = None
        except (OSError, HerdrError, ValueError, KeyError) as e:
            if isinstance(e, OSError) and not isinstance(e, urllib.error.URLError):
                unreachable_since = unreachable_since or time.monotonic()
                if time.monotonic() - unreachable_since > RECONNECT:
                    return  # Herdr server is gone; the startup hook brings us back
            log(str(e))
        _, interval = settings()
        if select.select([wake_r], [], [], interval)[0]:
            os.read(wake_r, 512)


def start():
    """Start this session's watcher if it isn't running, otherwise make it refresh now."""
    path = os.path.join(STATE_DIR, f"watch-{SESSION}.lock")
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free -> nobody is watching
    except BlockingIOError:
        try:
            with open(path) as f:
                os.kill(int(f.read().strip()), signal.SIGUSR1)
        except (OSError, ValueError):
            pass
        return
    except FileNotFoundError:
        pass
    log_path = os.path.join(STATE_DIR, f"watch-{SESSION}.log")
    try:
        if os.path.getsize(log_path) > MAX_LOG:
            os.truncate(log_path, 0)
    except OSError:
        pass
    logf = open(log_path, "a")
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "watch"],
                     stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                     start_new_session=True, env=os.environ)


def open_pr():
    """Open the focused workspace's pull request in the default browser."""
    forges, _ = settings()
    ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    dirs = [d for d in (ctx.get("focused_pane_cwd"), ctx.get("workspace_cwd")) if d]
    ws_id = ctx.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    if ws_id:
        dirs += workspace_dirs().get(ws_id, [])
    co = first_checkout(dirs, forges)
    if not co:
        return notify("No pull request", "This workspace isn't on a branch with a Forgejo or GitHub remote.")
    fg, repo, branch, _ = co
    try:
        _, url = find_pr(*co, Remembered())
    except (RateLimited, OSError, ValueError) as e:
        return notify(f"{fg.name} lookup failed", str(e))
    if not url:
        return notify("No pull request", f"{repo}: nothing found for {branch}")
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def check(*args):
    """Debug: `pr.py check` for every workspace, or `pr.py check HOST OWNER/REPO BRANCH`."""
    forges, _ = settings()
    remembered = Remembered()
    if args:
        host, repo, branch = args
        print(find_pr(forge(*forges[host]), repo, branch, "", remembered))
        return
    for ws_id, dirs in workspace_dirs().items():
        co = first_checkout(dirs, forges)
        print(ws_id, f"{co[0].key}/{co[1]}:{co[2]}" if co else "-", find_pr(*co, remembered) if co else "")


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    cmd, *args = sys.argv[1:] or ["start"]
    {"watch": watch, "start": start, "open": open_pr, "check": check}[cmd](*args)
