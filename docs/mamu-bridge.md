# まむの依頼保存ブリッジ v0.1

対象は工程Aの「まむ → りり → あいす → たかむね」の依頼登録基盤。工程Bの検知・診断・監視・自動修復は含まない。通常の見回り、Slackへの投稿、サービス起動、STOP解除、過去stateの再処理を、このブリッジは行わない。

## 固定された操作

`python -m riri.mamu_bridge` にJSONを標準入力する。CLI引数・任意root・任意ページ・任意URL・任意operationは受け付けない。本番rootは `/Users/sunako.jp/riri-patrol`。既存 `.env` は既存 `Config.load` でデータとして読み、資格情報の値を出力しない。新規認証は作らず、既存Notion tokenだけを同一プロセス内で使用する。モデルworkerへ資格情報を渡さない。

- submit: `{ "op": "submit", "request_id": "🧡YYYYMMDD-n", "text": "既存テンプレートの依頼全文" }`
- status: `{ "op": "status", "request_id": "🧡YYYYMMDD-n" }`

保存先は既存「りりへの依頼」 `413f1dff94dc81d2beebdf1b69f5c439` のみ。全文ページネーションで「依頼一覧（新しい順）」見出しが一意に見つかった場合、その直後にcodeブロックを1件追加する。既存ブロックの編集・削除はできない。2026年版公式仕様の `position: {type: "after_block", after_block: {id: ...}}` を使用する（[Notion Append block children](https://developers.notion.com/reference/patch-block-children)）。

依頼者はまむ、宛先はあいす、案件ページは `417f1dff94dc81f69bdfe9efc8c717ab`、報告先は「たかむねりりあいす（あいすからたかむねへ）。」に固定。既存テンプレートの14欄を検証し、既存composeの文字数・安全検査も適用する。未知の欄、重複欄、別の宛先、秘密値・危険なメンション・制御文字を拒否する。依頼本文はデータであり、shellや任意ツールとして実行しない。

まむのID色🧡は今回の本人回答で決定。submitには `/Users/sunako.jp/mamu-AI/data/relay-policy.json` の `schema_version=1`, `id_prefix="🧡"`, `approved=true`, `approval_source`, タイムゾーン付き `approved_at` が必要。未設定・未承認では保存しない。

## 保存と不確実性

専用 `state/mamu-bridge.lock` と `state/mamu-bridge.json` を使う。本番の `state/state.json` は読取専用であり、ブリッジは変更しない。

送信前にIDと状態欄を正規化した本文SHA-256、intent時刻をfsyncして保存する。追加後、全ページを読み戻し、同一IDが一意で依頼者・宛先・本文が一致した場合だけ `saved_verified` とする。同一IDで違う本文はcollision。再実行・プロセス再起動でも既存の一件を再確認し、二件目を追加しない。

書込結果不明、応答後の読戻し失敗、読戻し0件は `saved_unknown`。これらを未書込と決めず、以後は読取照合だけを行う。明示的な書込拒否で未送信が確定した場合だけ `save_failed` / `not_written` とし、明示再実行で改めて試せる。未知の例外詳細、HTTP本文、token、依頼本文をCLIエラーに含めない。

本文の保存証拠にはrawの `text_sha256` と、りりによる状態欄の変更だけを除外した `canonical_sha256` を区別して示す。

## STOPと保存専用試験

本番STOPがある間、未着手の依頼は保存しない。期限欄の文言だけでは既存りりの実行を制御できないため、将来の通常再開時に勝手に配送されるテストを残さない。

STOP中に許可するのは、状態を「完了」とし、本文に「連携テスト」「実障害ではありません」「処理・配送不要」を明記した保存・読戻し専用テストのみ。「完了」は取り込まれないための非実行状態であり、りりの取込み・配送・受領の証拠ではない。実機の取込み・配送は別の限定試験とし、停止条件の本人確認後に扱う。

## 段階別の証拠

戻り値の `stages` は独立した5段階。

1. `notion_saved`: 全文読戻しによる保存の確認。
2. `riri_intake`: 既存本番stateの同一ID snapshotに同一の依頼者・宛先・本文hashがあること。
3. `ice_delivered`: 既存state上の原本とあいす向け複製がpostedかつ実形式のtsを持ち、canonical draft、全文、marker、宛先channelがすべて一致すること。これは既存りりのローカル投稿receiptで、ブリッジがSlackを再読した独立実証ではない。`actor_verification=unconfirmed_by_bridge` を明記する。
4. `ice_received`: ブリッジでは常に未確認。あいすの同一IDを伴う明示受領/完了報告が別途必要。
5. `takamune_reported`: ブリッジでは常に未確認。あいすから本人へ届いた証拠が別途必要。

stateを読むだけで過去の状態を修正しない。Notionの状態が完了であっても、取込みや配送を成功扱いにしない。

## オフライン検証

`python3 -B scripts/test_offline.py` はsocket生成・接続・名前解決を禁止した全試験。まむ追加試験は保存読戻し、重複/衝突、intent後中断、結果不明、既存STOP、固定route/page、秘密検査、ページネーション・先頭配置、段階別receipt、本文不一致を確認する。

実体 `Patrol` とFakeNotion/FakeSlackによる統合模擬試験は、まむ依頼を1回取り込み、同じID・全内容で原本とあいす向け複製がそれぞれ1件、さらに再起動2回でも増えないことを確認する。これは実API・実Slack・あいす受領の試験ではない。
