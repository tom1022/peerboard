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

def board(sid, name, summary, t):
    pb.write_json(pb.BOARD / f"{sid}.json", {"sessionId": sid, "name": name, "cwd": f"/w/{name}", "summary": summary, "updatedAt": t})

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

# 非同期サマライズ (agy をスタブ): stop hook -> board 書き込み
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
print("ok")
