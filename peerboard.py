#!/usr/bin/env python3
"""peerboard: Claude Code セッション間の作業サマリ共有。hooks から呼ばれる。"""
import json, os, subprocess, sys, tempfile, time, traceback
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
MAX_CHARS = 20000
MAX_SUMMARY = 600

PROMPT = """あなたは Claude Code セッションの作業ログ要約器です。以下の会話抜粋から、このセッションの現状を日本語で最大3行、各行60字以内で出力してください。前置き・装飾・コードブロックは禁止。
1行目: 今やっていること: ...
2行目: 決定事項: ... (なければ省略)
3行目: 未解決・他セッションに関係しそうな点: ... (なければ省略)

前回の要約 (継続性のため参照。古ければ更新):
{prev}

会話抜粋:
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
    """user/assistant の text のみ。末尾 MAX_CHARS を返す。"""
    lines = []
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
                lines.append(f"[{t}] {c.strip()}")
    return "\n".join(lines)[-MAX_CHARS:]


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
                                               "summary": summary, "updatedAt": time.time()})
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


def peers(sid, alive, only_unseen):
    seen = read_json(SEEN / f"{sid}.json", {}) if only_unseen else {}
    out = []
    for s in alive:
        if s == sid:
            continue
        e = read_json(BOARD / f"{s}.json")
        if e and e.get("summary") and e.get("updatedAt", 0) > seen.get(s, 0):
            out.append(e)
    return out


def inject(hook, event, only_unseen):
    sid = hook.get("session_id")
    if not sid:
        return
    alive = alive_sessions()
    prune(alive)
    es = peers(sid, alive, only_unseen)
    if not es:
        return
    seen = read_json(SEEN / f"{sid}.json", {})
    seen.update({e["sessionId"]: e["updatedAt"] for e in es})
    write_json(SEEN / f"{sid}.json", seen)
    body = "\n".join(f"- {e['name']} ({os.path.basename(e.get('cwd', '').rstrip('/'))}): "
                     f"{e['summary'].replace(chr(10), ' / ')}" for e in es)
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
