# のあをMacのClaude Codeへ直接届ける（NOA_TRANSPORT=local、未配備）

依頼：たかむね（2026-11-13、案1「りり（Mac）から直接のあを起こす」）。基準 main `b172207`。版番号・`NOA_WAKE_ENABLED` の既定 false・既定の届け方（routine）は変更していません。

## 何が変わるか

`.env` で `NOA_TRANSPORT=local` にしたときだけ、のあの起動先がクラウドのroutineではなく、このMacのClaude Codeになります。対象は既存の2経路（りりが作った指示の @のあ、人間の @のあ）の両方。回数制限・一度きり（sending を先に保存）・STOP・練習モード・秘密検査・held/rejected の扱いは既存のまま共通です。

届け先の選び方：
1. `~/.claude/projects/*/*.jsonl` を更新が新しい順に見て、記録された作業フォルダ（cwd）が `NOA_LOCAL_DIR` と完全一致するものだけを候補にする（別フォルダの履歴は数に入れない）
2. 依頼文の依頼ID・関連ID（絵文字つきで照合。💛と💚の同じ番号は別物、数字の続きは別番号）または「案件ページ：」のNotion ID を含む候補があれば、いちばん新しいものを `--resume` で続ける。Notion ID は 32桁／8-4-4-4-12 のどちらで書かれていても同じものとして照合する（大文字小文字も同一視）
3. 最後まで探して一致がなければ、`NOA_LOCAL_DIR` で新しいセッションを始める
4. 探索が最後までできなかったとき（候補 400件・全体 5000件の上限超え、読めない履歴、一覧取得の失敗）は「一致なし」と区別し、新しいセッションも始めずに届けない（rejected `local_search_incomplete`、自動再送なし、専用の通知）

起動方法：`claude -p --permission-mode <指定> [--resume <id>]` をシェルなしで実行。本文は標準入力で渡し、りりは完了を待ちません（別プロセスグループ）。子プロセスには HOME/PATH/LANG 等の最小限の環境変数だけを渡し、りりのSlack/Notionトークンは渡しません。本文と出力は `logs/noa-local/` に 0600 で保存します。起動の直前に、その試行が local だったことと安全な参照（`logs/noa-local/<時刻-乱数>`）を state に保存します（routine の試行は routine と保存）。起動に成功した時点で「届けた」とし、証拠は `local:resume:<セッションID>` または `local:new:pid<番号>` です。起動前の失敗は rejected（自動再送なし）。

## 設定（.env、本番のMacで承認後に）

```
NOA_TRANSPORT=local
NOA_LOCAL_DIR=/Users/sunako.jp/のあ
NOA_CLAUDE_BIN=（which claude の絶対パス）
NOA_LOCAL_PERMISSION_MODE=bypassPermissions（たかむね決定。default / acceptEdits / plan も可。空なら届けずに止まる）
```

たかむねの決定（2026-11-13）により `bypassPermissions`（確認なしで全操作を許可）を使います。してはいけないことは各依頼文の「やってはいけないこと」で指定する運用です。ツール側の止め役はないので、禁止事項を守るかはのあ本体と依頼文に依存します。上の4つ以外（dontAsk 等）は受け付けません。

## 互換・限界

- 結果不明（held）の通知は、試行時に保存した経路で案内先を変えます。local なら Mac の Claude Code（「のあ」フォルダ）の会話と上の参照、routine なら従来どおりの文面。経路が記録されていない古い記録は、local 設定中なら両方を確認するよう案内し、どちらとも断定しません。routine 設定だけで使っている場合の文面は従来と同じです。
- `riri/noa.py` は過去版互換試験でハッシュ固定のため変更していません。人間 @のあ 経路の通知は拡張側（`InstructionNoaWake.notices`）で同じ処理を持ち、held と local 固有の文面だけ差し替えています。人間 @のあ 経路の証拠確認は、local 実行中の1巡回だけ `local_session` 結果を受け付けるよう差し替え、終了時に元に戻します（routine 時は差し替えなし）。
- 「概要が同じようなもの」の判定は依頼ID・関連ID・案件ページIDの一致だけです。文章の意味の近さは見ていません。
- Claude Code のセッション保存形式（jsonl の `cwd`/`sessionId`）に依存します。形式が変わると「新しいセッション」側に倒れます。
- Macで Claude Code にログインしていること、`NOA_LOCAL_DIR` が存在することが前提です。のあの作業結果や完了報告は、のあ本体が書きます。りりは確認しません。
- 実機のClaude Codeでの起動・再開はこの環境では未確認です（試験は偽の起動関数で実施）。

## 試験

`python3 scripts/test_offline.py`（通信禁止）の結果は PR の報告を参照（修正ごとに更新）。追加試験は `tests/test_local_noa.py`（初版）と `tests/test_local_noa_review.py`（みにのレビュー L-01〜L-03 の反例、両受付経路の STOP 直前・練習・OFF・秘密拒否・sending 中断→held・自動再送なし・routine 既定）。

自己試験です。レビュー・実機確認・.env変更・launchd再登録・mainへの反映は未実施。
