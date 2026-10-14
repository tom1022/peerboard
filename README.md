# peerboard

同一マシン上で動く複数の Claude Code セッションが、「今やっていること」の要約を自動で共有するプラグイン。関連しそうな他セッションがあれば、組み込みの SendMessage で直接問い合わせられる。

## 仕組み

- Stop フック: 前回の要約から一定間隔が空き、かつ transcript が更新されていれば、デタッチしたバックグラウンドプロセスで `agy` に要約させ `~/.claude/board/<sessionId>.json` に保存する。要約は「目的 / 現状 / 共有」の3行 (各80字以内の日本語) で、別リポジトリのセッションにも何の作業か分かる粒度にする。agy にはセッション冒頭の user 発言 (目的の把握用) と直近のやり取りを渡す。要約とは別に、LLM を使わず transcript 全体 (subagent の transcript を含む) から「触った場所」を機械的に抽出して `touched` に保存する。対象は Edit/Write/MultiEdit/NotebookEdit の対象ディレクトリ (`<リポジトリ名>/<リポジトリ内の親ディレクトリ>`、git 外は `~` 表記)、書き込み系の MCP ツール呼び出し (`<サーバー>:<対象>`)、`terraform apply` / `kubectl apply` / `git push` などの Bash 副作用 (`cmd:<コマンド>@<リポジトリ名>`、正規表現による簡易判定)。新しい順・重複なしで最大8件。注入時は各行末に ` / 触った場所: ...` として付く。Claude のトークンは消費しない。
- UserPromptSubmit フック: 生存中の他セッションのうち、前回見た時より更新されたものだけを additionalContext として注入する。変化がなければ何も出力しない。
- SessionStart フック: 生存中の全ピアの現状を一度注入する。
- 生存判定は `~/.claude/sessions/<pid>.json` と pid の存在確認。死んだセッションのボードは自動削除される。
- `python3 peerboard.py list` で現在のボードを表示する。

## インストール

```
/plugin marketplace add tom1022/peerboard
/plugin install peerboard@peerboard-local
```

## 環境変数

| 変数 | 既定値 | 内容 |
|---|---|---|
| `PEERBOARD_INTERVAL_SEC` | `300` | 要約の最小間隔 (秒) |
| `PEERBOARD_AGY_MODEL` | `gemini-3.8-flash-low` | `agy --model` に渡すモデル |
| `PEERBOARD_STALE_SEC` | `21600` | SessionStart で、更新からこの秒数を超えたセッションを注入対象から外す |
| `PEERBOARD_HOME` | `~/.claude/board` | ボードの保存先 |
| `PEERBOARD_CLAUDE_DIR` | `~/.claude` | セッションレジストリを探すディレクトリ |
| `PEERBOARD_AGY` | `agy` | agy 実行ファイル |

バージョン (`.claude-plugin/plugin.json`) は変更のたびに上げる。インストール済みキャッシュはバージョンをキーにするため、上げないと更新が届かない。

エラーはフックの出力ではなく `~/.claude/board/.log` に記録される。
