"""python3 test_peerboard.py  (実 ~/.claude には触れない)"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).parent
tmp = Path(tempfile.mkdtemp())
(tmp / "sessions").mkdir()
os.environ.update(PEERBOARD_CLAUDE_DIR=str(tmp), PEERBOARD_HOME=str(tmp / "board"))
os.environ.pop("PEERBOARD_CHILD", None)
sys.path.insert(0, str(ROOT))
import peerboard as pb

def reg(sid, name, pid):
    (tmp / "sessions" / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "cwd": f"/w/{name}", "kind": "interactive", "name": name}))

def board(sid, name, summary, t, **extra):
    pb.write_json(pb.BOARD / f"{sid}.json", {"sessionId": sid, "name": name, "cwd": f"/w/{name}", "summary": summary, "updatedAt": t, **extra})

def hook(cmd, sid, env=None):
    r = subprocess.run([sys.executable, str(ROOT / "peerboard.py"), cmd], input=json.dumps({"session_id": sid}),
                       capture_output=True, text=True, env={**os.environ, **(env or {})})
    assert r.returncode == 0, r.stderr
    return r.stdout

dead = subprocess.Popen(["true"]); dead.wait()
reg("me", "me", os.getpid()); reg("peer", "peer-1", os.getppid()); reg("gone", "gone-1", dead.pid)
board("peer", "peer-1", "今やっていること: X", time.time()); board("gone", "gone-1", "old", time.time())

# seen-diff: 初回は出力、2回目は無出力、更新後に再出力。自分と死んだセッションは除外
o = json.loads(hook("prompt", "me"))["hookSpecificOutput"]
assert o["hookEventName"] == "UserPromptSubmit" and "peer-1" in o["additionalContext"]
assert "gone-1" not in o["additionalContext"] and "(me)" not in o["additionalContext"]
assert hook("prompt", "me") == ""
board("peer", "peer-1", "今やっていること: Y", time.time() + 1)
assert "Y" in hook("prompt", "me")

# prune: 死んだセッションの board が消える
assert not (pb.BOARD / "gone.json").exists()

# SessionStart は既読でも全 peer を出す
assert "peer-1" in hook("start", "me")

# start は PEERBOARD_STALE_SEC 超の entry を省き、各行に経過時間が付く
reg("old", "old-1", 1); board("old", "old-1", "stale", time.time() - 7 * 3600)
o = hook("start", "me")
assert "peer-1, たった今)" in o and "old-1" not in o
assert (pb.BOARD / "old.json").exists()
assert "old-1, 7時間前)" in hook("start", "me", {"PEERBOARD_STALE_SEC": "86400"})
(tmp / "sessions" / "1.json").unlink(); pb.prune(pb.alive_sessions())

# recursion guard
board("peer", "peer-1", "Z", time.time() + 2)
assert hook("prompt", "me", {"PEERBOARD_CHILD": "1"}) == ""

# throttle: 間隔内は不可、間隔超過+transcript 更新で可、transcript 未更新は不可、ロック中は不可
tr = tmp / "t.jsonl"; tr.write_text("{}\n")
now = time.time()
board("me", "me", "s", now - 10)
assert not pb.should_summarize("me", str(tr), now)
board("me", "me", "s", now - 1000)
os.utime(tr, (now - 2000, now - 2000))
assert not pb.should_summarize("me", str(tr), now)
os.utime(tr, (now, now))
assert pb.should_summarize("me", str(tr), now)
pb.write_json(pb.LOCKS / "me.json", {"pid": os.getpid(), "t": now})
assert not pb.should_summarize("me", str(tr), now)

# transcript 抽出: tool_result / meta を除外
tr.write_text("\n".join(json.dumps(x) for x in [
    {"type": "user", "message": {"content": "hello"}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET"}]}},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "T"}, {"type": "text", "text": "hi"}]}},
]))
t = pb.transcript_text(tr)
assert "hello" in t and "hi" in t and "SECRET" not in t and "T\n" not in t

# head+tail: 冒頭の user 目的と末尾が両方入り、中間は落ちる。冒頭は user のみ
pb.TAIL_CHARS = 100
tr.write_text("\n".join(json.dumps(x) for x in [
    {"type": "user", "message": {"content": "GOAL: build X"}},
    {"type": "assistant", "message": {"content": "ASSIST_EARLY"}},
    {"type": "assistant", "message": {"content": "M" * 500}},
    {"type": "user", "message": {"content": "LAST_ASK"}},
]))
t = pb.transcript_text(tr)
head, tail = t.split("## 直近")
assert "## セッション冒頭" in head and "GOAL: build X" in head and "ASSIST_EARLY" not in head
assert "LAST_ASK" in tail and "GOAL" not in tail
pb.TAIL_CHARS = 16000

# 触った場所: repo 相対化・重複排除・新しい順・上限5・非 git は ~ 表記
repo = tmp / "repo"; (repo / ".git").mkdir(parents=True)
home = tmp / "home"; (home / "x").mkdir(parents=True)
pb.Path.home = staticmethod(lambda: home)
def tu(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
tr.write_text("\n".join(json.dumps(x) for x in [
    tu("Edit", file_path=str(repo / "a/b/f1.py")),
    tu("Write", file_path=str(repo / "c/f2.py")),
    tu("Read", file_path=str(repo / "READ/ONLY.py")),
    tu("NotebookEdit", notebook_path=str(repo / "nb/n.ipynb")),
    tu("Edit", file_path=str(repo / "a/b/f3.py")),
    tu("MultiEdit", file_path=str(repo / "f4.py")),
    tu("Edit", file_path=str(home / "x/y.txt")),
    tu("Edit", file_path=str(repo / "d/e/f5.py")),
]))
assert pb.touched_places(tr) == ["repo/d/e", "~/x", "repo", "repo/a/b", "repo/nb", "repo/c"]
assert pb.touched_places(tr, 2) == ["repo/d/e", "~/x"]

# subagent 編集のマージ (timestamp 順)・MCP 書き込み・Bash 副作用・上限8
def ev(ts, blk, cwd=""):
    return {"type": "assistant", "timestamp": ts, "cwd": cwd, "message": {"content": [blk]}}
def use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}
sid_dir = tmp / "sess"; (sid_dir / "subagents").mkdir(parents=True)
mt = tmp / "sess.jsonl"
mt.write_text("\n".join(json.dumps(x) for x in [
    ev("2026-01-01T00:00:01Z", use("Edit", file_path=str(repo / "m/f.py"))),
    ev("2026-01-01T00:00:03Z", use("mcp__plugin_figma_figma__use_figma", fileKey="AbC123")),
    ev("2026-01-01T00:00:04Z", use("mcp__github__create_or_update_file", owner="o", repo="r", path="README.md")),
    ev("2026-01-01T00:00:05Z", use("mcp__github__get_file_contents", owner="o", repo="r")),
    ev("2026-01-01T00:00:06Z", use("Bash", command="cd x && kubectl apply -f a.yaml"), cwd=str(repo)),
    ev("2026-01-01T00:00:07Z", use("Bash", command="ls")),
]))
(sid_dir / "subagents" / "agent-1.jsonl").write_text(json.dumps(
    ev("2026-01-01T00:00:02Z", use("Write", file_path=str(repo / "sub/g.py")))))
assert pb.touched_places(mt) == ["cmd:kubectl apply@repo", "github:o/r:README.md", "figma:AbC123", "repo/sub", "repo/m"]
mt.write_text(mt.read_text() + "\n" + "\n".join(json.dumps(ev(f"2026-01-02T00:00:0{i}Z", use("Edit", file_path=str(repo / f"d{i}/x")))) for i in range(9)))
assert len(pb.touched_places(mt)) == 8 and pb.touched_places(mt)[0] == "repo/d8"

# 注入: touched があれば末尾に付き、空なら付かない
board("peer", "peer-1", "S", time.time() + 5, touched=["repo/a", "repo/b"])
assert "S / 触った場所: repo/a, repo/b" in hook("start", "me")
board("peer", "peer-1", "S", time.time() + 6, touched=[])
assert "触った場所" not in hook("start", "me")

# 非同期サマライズ (agy をスタブ): stop hook -> board 書き込み
tr.write_text(json.dumps({"type": "user", "message": {"content": "hello"}}))
stub = tmp / "agy"; stub.write_text("#!/bin/sh\necho 今やっていること: stub\n"); stub.chmod(0o755)
(pb.LOCKS / "me.json").unlink()
board("me", "me", "s", now - 1000)
r = subprocess.run([sys.executable, str(ROOT / "peerboard.py"), "stop"], capture_output=True, text=True,
                   input=json.dumps({"session_id": "me", "transcript_path": str(tr), "cwd": "/w/me"}),
                   env={**os.environ, "PEERBOARD_AGY": str(stub)})
assert r.returncode == 0
for _ in range(50):
    if "stub" in pb.read_json(pb.BOARD / "me.json")["summary"]:
        break
    time.sleep(0.1)
else:
    raise AssertionError("summary not written")
assert pb.read_json(pb.BOARD / "me.json")["touched"] == []
print("ok")
