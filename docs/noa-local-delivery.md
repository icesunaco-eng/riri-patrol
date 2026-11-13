# のあをMacのClaude Codeへ直接届ける（NOA_TRANSPORT=local、未配備）

依頼：たかむね（2026-11-13、案1「りり（Mac）から直接のあを起こす」）。基準 main `b172207`。版番号・`NOA_WAKE_ENABLED` の既定 false・既定の届け方（routine）は変更していません。

## 何が変わるか

`.env` で `NOA_TRANSPORT=local` にしたときだけ、のあの起動先がクラウドのroutineではなく、このMacのClaude Codeになります。対象は既存の2経路（りりが作った指示の @のあ、人間の @のあ）の両方。回数制限・一度きり（sending を先に保存）・STOP・練習モード・秘密検査・held/rejected の扱いは既存のまま共通です。

届け先の選び方：
1. `~/.claude/projects/*/*.jsonl` のうち、記録された作業フォルダが `NOA_LOCAL_DIR` と完全一致するセッションだけを見る
2. 依頼文の依頼ID・関連ID（絵文字つきで照合。💛と💚の同じ番号は別物）または「案件ページ：」のNotion IDを含むものがあれば、いちばん新しく更新されたものを `--resume` で続ける
3. なければ `NOA_LOCAL_DIR` で新しいセッションを始める

起動方法：`claude -p --permission-mode <指定> [--resume <id>]` をシェルなしで実行。本文は標準入力で渡し、りりは完了を待ちません（別プロセスグループ）。子プロセスには HOME/PATH/LANG 等の最小限の環境変数だけを渡し、りりのSlack/Notionトークンは渡しません。本文と出力は `logs/noa-local/` に 0600 で保存します。起動に成功した時点で「届けた」とし、証拠は `local:resume:<セッションID>` または `local:new:pid<番号>` です。起動前の失敗は rejected（自動再送なし）。

## 設定（.env、本番のMacで承認後に）

```
NOA_TRANSPORT=local
NOA_LOCAL_DIR=/Users/sunako.jp/のあ
NOA_CLAUDE_BIN=（which claude の絶対パス）
NOA_LOCAL_PERMISSION_MODE=default / acceptEdits / plan のどれか（空なら届けずに止まる）
```

`bypassPermissions` 等の全許可モードは受け付けません。`default` だと、確認が必要な操作（ファイル編集など）は -p 実行では拒否されるので、のあが作業できる範囲が狭くなります。どれにするかは、たかむねが決めてください。

## 互換・限界

- `riri/noa.py` は過去版互換試験でハッシュ固定のため変更していません。人間 @のあ 経路の証拠確認は、local 実行中の1巡回だけ `local_session` 結果を受け付けるよう差し替え、終了時に元に戻します（routine 時は差し替えなし）。
- 「概要が同じようなもの」の判定は依頼ID・関連ID・案件ページIDの一致だけです。文章の意味の近さは見ていません。
- Claude Code のセッション保存形式（jsonl の `cwd`/`sessionId`）に依存します。形式が変わると「新しいセッション」側に倒れます。
- Macで Claude Code にログインしていること、`NOA_LOCAL_DIR` が存在することが前提です。のあの作業結果や完了報告は、のあ本体が書きます。りりは確認しません。
- 実機のClaude Codeでの起動・再開はこの環境では未確認です（試験は偽の起動関数で実施）。

## 試験

`python3 scripts/test_offline.py`（通信禁止）：既存657件＋追加14件、計671件成功。追加は届け先選択（同フォルダ・関連ID・案件ページ・絵文字違い・別フォルダ除外・最新優先）、引数と標準入力、環境変数に秘密なし、ログ権限、設定不備・全許可モード拒否、STOP時未送信、起動失敗＝rejected、証拠の検証、routine側の証拠確認が元に戻ること、指示経路・人間経路それぞれの1回だけの配達。

自己試験です。レビュー・実機確認・.env変更・launchd再登録・mainへの反映は未実施。
