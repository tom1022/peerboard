#!/usr/bin/env python3
"""peerboard: Claude Code セッション間の作業サマリ共有。hooks から呼ばれる。"""
import json, os, re, subprocess, sys, tempfile, time, traceback
from pathlib import Path

CLAUDE = Path(os.environ.get("PEERBOARD_CLAUDE_DIR", Path.home() / ".claude"))
SESSIONS = CLAUDE / "sessions"
BOARD = Path(os.environ.get("PEERBOARD_HOME", CLAUDE / "board"))
SEEN = BOARD / ".seen"
LOCKS = BOARD / ".lock"
LOG = BOARD / ".log"
INTERVAL = int(os.environ.get("PEERBOARD_INTERVAL_SEC", "300"))
MODEL = os.environ.get("PEERBOARD_AGY_MODEL", "gemini-3.8-flash-low")
AGY = os.environ.get("PEERBOARD_AGY", "agy")
STALE = int(os.environ.get("PEERBOARD_STALE_SEC", "21600"))
HEAD_CHARS = 4000
TAIL_CHARS = 16000
MAX_SUMMARY = 600

PROMPT = """あなたは Claude Code セッションの作業ログ要約器です。別リポジトリで作業中の同僚が読んで、このセッションが何についてのものか理解できる要約を、日本語で3行、各行80字以内で出力してください。前置き・装飾・コードブロックは禁止。
1行目: 目的: このセッションが何のシステム/機能の何を、なぜやっているか。リポジトリ外の人にも分かる粒度
2行目: 現状: 今どの段階か。抽象度は1段上げる。commit/PR/ブランチの手順の話は書かない
3行目: 共有: 他セッションに影響しうる共有リソース・インターフェース・決定 (API、スキーマ、インフラ、配信方式など)。なければ「なし」

書き方:
- 具体的な機能・コンポーネント名で書く (例: 「サイネージの緊急地震速報配信」)。内部の作業管理用語で書かない。
- 共有行では識別子 (PR 番号、環境変数/シークレット名、API パス、k8s リソース、namespace) を、何であるかを併記して書くことを推奨する。例: 「infra #391 で地震速報リレーのDeployment追加」「Infisical に EEW_RELAY_TOKEN 追加」。意味の分からない識別子だけの記述は禁止。
- タスク ID (1.1, FX-2 など) や worktree/ブランチ/コミット操作の話は書かない。
- 前回の要約の「目的」は、目的が明らかに変わらない限りそのまま維持する。

前回の要約:
{prev}

{text}
"""


def log(msg):
    try:
        BOARD.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
        if LOG.stat().st_size > 200_000:  # 無限肥大の防止
            LOG.write_text(LOG.read_text()[-50_000:])
    except Exception:
        pass


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def alive_sessions():
    """sessionId -> registry entry (interactive かつ pid 生存のみ)"""
    out = {}
    for p in SESSIONS.glob("*.json"):
        d = read_json(p)
        if d and d.get("kind") == "interactive" and pid_alive(d.get("pid")) and d.get("sessionId"):
            out[d["sessionId"]] = d
    return out


def prune(alive):
    for p in BOARD.glob("*.json"):
        if p.stem not in alive:
            p.unlink(missing_ok=True)
    for d in (SEEN, LOCKS):
        for p in d.glob("*.json"):
            if p.stem not in alive:
                p.unlink(missing_ok=True)


def transcript_text(path):
    """user/assistant の text のみ。冒頭の user 発言 (目的が書かれる) と末尾を分けて返す。"""
    msgs = []
    with open(path, errors="replace") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
                continue
            c = d.get("message", {}).get("content")
            if isinstance(c, list):
                c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                msgs.append((t, c.strip()))
    head, n = [], 0
    for t, c in msgs:
        if t == "user" and n < HEAD_CHARS:
            head.append(f"[user] {c}")
            n += len(head[-1])
    tail = "\n".join(f"[{t}] {c}" for t, c in msgs)[-TAIL_CHARS:]
    if not tail:
        return ""
    return f"## セッション冒頭\n{chr(10).join(head)[:HEAD_CHARS]}\n\n## 直近\n{tail}"


EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def short_path(fp):
    """絶対パスを `<repo名>/<repo 内の親dir>` に畳む。git 外は $HOME を ~ にした親dir。"""
    parent = Path(fp).parent
    for d in (parent, *parent.parents):
        if (d / ".git").exists():
            rel = parent.relative_to(d).as_posix()
            return d.name if rel == "." else f"{d.name}/{rel}"
    home = str(Path.home())
    s = str(parent)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


MCP_WRITE = ("create", "update", "delete", "write", "push", "merge", "upload", "use_figma", "generate", "send",
             "share", "trash", "apply", "edit", "set", "add", "remove", "replace", "batch")
# ponytail: 正規表現ヒューリスティック。任意の curl/API 呼び出しなどは拾えない
BASH_EFFECT = re.compile(r"\b(terraform apply|tofu apply|kubectl (?:apply|delete|patch|rollout)|helm (?:install|upgrade|uninstall)"
                         r"|gh pr (?:create|merge)|gh repo create|git push|docker push)\b")


def mcp_label(name, inp):
    _, server, *tool = name.split("__")
    server = server.removeprefix("plugin_").split("_")[-1] if server.startswith("plugin_") else server
    tool = "__".join(tool)
    if not any(w in tool for w in MCP_WRITE):
        return None
    for k in ("fileKey", "file_key"):
        if inp.get(k):
            return f"{server}:{inp[k]}"
    if inp.get("owner") and inp.get("repo"):
        return f"{server}:{inp['owner']}/{inp['repo']}" + (f":{inp['path']}" if inp.get("path") else "")
    for k in ("path", "file_id", "fileId", "url", "id", "name"):
        if inp.get(k):
            return f"{server}:{inp[k]}"
    return f"{server}:{tool}"


def touched_places(path, limit=8):
    """main + subagent transcript 全体から副作用のある tool_use を新しい順・重複なしで返す (LLM 不使用)。
    subagent の編集は main transcript に現れず <sid>/subagents/ 配下にだけ残る。"""
    path = Path(path)
    files = [path, *sorted((path.with_suffix("") / "subagents").glob("agent-*.jsonl"))]
    events = []
    for f in files:
        with open(f, errors="replace") as fh:
            for raw in fh:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                c = d.get("message", {}).get("content") if d.get("type") == "assistant" else None
                for b in c if isinstance(c, list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name, inp = b.get("name", ""), b.get("input") or {}
                    label = None
                    if name in EDIT_TOOLS:
                        fp = inp.get(EDIT_TOOLS[name])
                        label = short_path(fp) if isinstance(fp, str) and fp.startswith("/") else None
                    elif name.startswith("mcp__"):
                        label = mcp_label(name, inp)
                    elif name == "Bash" and isinstance(inp.get("command"), str):
                        m = BASH_EFFECT.search(inp["command"])
                        label = m and f"cmd:{m.group(1)}@{os.path.basename(d.get('cwd', '').rstrip('/'))}"
                    if label:
                        events.append((d.get("timestamp", ""), label))
    seen = {}
    for _, label in sorted(events, key=lambda e: e[0]):  # stable: 同時刻は読み順
        seen.pop(label, None)
        seen[label] = 1
    return list(reversed(seen))[:limit]


def run_agy(prompt):
    r = subprocess.run([AGY, "-p", prompt, "--model", MODEL, "--output-format", "text"],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PEERBOARD_CHILD": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"agy rc={r.returncode}: {r.stderr[-300:]}")
    return r.stdout.strip()[:MAX_SUMMARY]


def summarize(sid, transcript, name, cwd):
    """子プロセス本体。ロックは親が pid 付きで作成済み。"""
    lock = LOCKS / f"{sid}.json"
    try:
        prev = (read_json(BOARD / f"{sid}.json") or {}).get("summary", "(なし)")
        text = transcript_text(transcript)
        if not text:
            return
        summary = run_agy(PROMPT.format(prev=prev, text=text))
        if summary:
            write_json(BOARD / f"{sid}.json", {"sessionId": sid, "name": name, "cwd": cwd,
                                               "summary": summary, "touched": touched_places(transcript),
                                               "updatedAt": time.time()})
    finally:
        lock.unlink(missing_ok=True)


def should_summarize(sid, transcript, now):
    entry = read_json(BOARD / f"{sid}.json")
    lock = read_json(LOCKS / f"{sid}.json")
    if lock and pid_alive(lock.get("pid")) and now - lock.get("t", 0) < 300:
        return False
    # 前回試行時刻を見る (失敗時の連打防止)。成功時は updatedAt と同値
    last = max((entry or {}).get("updatedAt", 0), (lock or {}).get("t", 0))
    if now - last < INTERVAL:
        return False
    try:
        return os.path.getmtime(transcript) > (entry or {}).get("updatedAt", 0)
    except OSError:
        return False


def cmd_stop(hook):
    sid, tp = hook.get("session_id"), hook.get("transcript_path")
    if not sid or not tp:
        return
    now = time.time()
    if not should_summarize(sid, tp, now):
        return
    reg = alive_sessions().get(sid, {})
    LOCKS.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as lf:
        p = subprocess.Popen(
            [sys.executable, __file__, "_summarize", sid, tp, reg.get("name") or sid[:8], hook.get("cwd", "")],
            stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True,
            env={**os.environ, "PEERBOARD_CHILD": "1"})
    # 失敗しても INTERVAL 内は再試行しないよう、時刻もロックに残す
    write_json(LOCKS / f"{sid}.json", {"pid": p.pid, "t": now})


def age_str(t):
    m = int((time.time() - t) / 60)
    return "たった今" if m < 1 else f"{m}分前" if m < 60 else f"{m // 60}時間前" if m < 2880 else f"{m // 1440}日前"


def peers(sid, alive, only_unseen, max_age=None):
    seen = read_json(SEEN / f"{sid}.json", {}) if only_unseen else {}
    out = []
    for s in alive:
        if s == sid:
            continue
        e = read_json(BOARD / f"{s}.json")
        if e and e.get("summary") and e.get("updatedAt", 0) > seen.get(s, 0) \
                and (max_age is None or time.time() - e["updatedAt"] <= max_age):
            out.append(e)
    return out


def inject(hook, event, only_unseen):
    sid = hook.get("session_id")
    if not sid:
        return
    alive = alive_sessions()
    prune(alive)
    es = peers(sid, alive, only_unseen, None if only_unseen else STALE)
    if not es:
        return
    seen = read_json(SEEN / f"{sid}.json", {})
    seen.update({e["sessionId"]: e["updatedAt"] for e in es})
    write_json(SEEN / f"{sid}.json", seen)
    body = "\n".join(f"- {e['name']} ({os.path.basename(e.get('cwd', '').rstrip('/'))}, {age_str(e['updatedAt'])}): "
                     f"{e['summary'].replace(chr(10), ' / ')}"
                     + (f" / 触った場所: {', '.join(e['touched'])}" if e.get("touched") else "") for e in es)
    ctx = ("[peerboard] 他セッションの最新状況:\n" + body +
           f"\n関連があれば SendMessage({{to: \"{es[0]['name']}\"}}) で詳細を問い合わせること。無関係なら無視。")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}, ensure_ascii=False))


def cmd_list():
    alive = alive_sessions()
    prune(alive)
    for s, reg in alive.items():
        e = read_json(BOARD / f"{s}.json")
        age = f"{int((time.time() - e['updatedAt']) / 60)}分前" if e else "-"
        print(f"{reg.get('name', s[:8])} ({os.path.basename(reg.get('cwd', ''))}) [{age}]")
        if e:
            print("  " + e["summary"].replace("\n", "\n  "))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "list":
        return cmd_list()
    if os.environ.get("PEERBOARD_CHILD") and cmd != "_summarize":
        return
    if cmd == "_summarize":
        return summarize(*sys.argv[2:6])
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return
    if cmd == "stop":
        cmd_stop(hook)
    elif cmd == "prompt":
        inject(hook, "UserPromptSubmit", True)
    elif cmd == "start":
        inject(hook, "SessionStart", False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
    sys.exit(0)
