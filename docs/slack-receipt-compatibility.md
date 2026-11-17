# Slack配送確認の互換修正 v1.4.1

v1.4.0のdual-v2経路で、本人名義の投稿が成功しても、Slackの返却表記を完全一致できず保留になる問題を修正する。送信済みと確認できない場合の自動再投稿禁止は維持する。

## 確認した原因と変更

- 実際の `auth.test` は `app_id` を返していなかった。新経路は必要時だけ既存のグループ履歴を1ページ・最大100件読み、認証済みbotのuser／bot_id／workspaceと一致するbot_profileからアプリを確認する。候補の本人投稿や表示名から自己証明しない。証拠がない、または競合する場合は新規投稿前に待機する。dry-runには追加読取を行わない。
- Slackは短いtext投稿にもrich blocksを返し、Unicode絵文字を `:name:` 表記へ変換する。新しい受領比較では、返却されたemojiのnameと検証済みUnicodeの組を使い、全文の位置を保って両表記を照合する。文字として書かれたcolon名を一括置換しない。
- 先頭の公式user、本文、改行・空白、Unicodeの各scalar、末尾markerを保持する。利用できるtextとblocksの両方を照合し、異なるfallback、追加メンション、能動的要素、未知の要素／属性は拒否する。長文の完全なblocks要件を維持する。
- 本人ID、部屋、app、workspace、tsの検査は残す。本人トークンによる投稿とbotトークン認証ではbot_idが異なることがあるため、同一アプリの証拠を使う。トークンの追加権限は要求しない。legacyの厳格なreader・投稿経路は変更しない。
- app確認の読取失敗は投稿前の待機として扱い、送信枠を消費しない。STOP／期限到達は未送信の証拠を保って伝播する。

## 検証と復旧

実際の障害を再現する通信なしの試験を先に失敗させ、修正後に成功することを確認する。試験fixtureは架空データのみ。取得済みの実2件は非共有のMac証拠をメモリ上で照合し、リポジトリへ本文を複製しない。

保留済み項目はこの修正だけで自動再送しない。運用復旧ではSTOP・自然終了・lock確保・最新データ保全の後、既存投稿の本人／app／宛名／全文／marker／一意な実tsを読取専用で照合する。全条件が一致した項目だけ既存acknowledge処理で保存する。未送信の2通目は通常の原本再確認と既存の予定に従う。古いstateへの復元やtsの捏造は行わない。

本人の受領・実着手、新しい完了報告の両部屋到達、長期の定期成功は別の確認範囲。新規試験投稿はたかむねが行う。

最終通信禁止試験は `TMPDIR=/private/tmp python3 scripts/test_offline.py` で全1067件成功（失敗・error・skip 0）。既存1028件と新規39件。18件のアプリ証拠、21件の正規化には両部屋の実client結合と偽メンション・entity欠落・STOP／期限の境界を含む。Python116本の構文確認と空白検査も成功。Standards／Spec独立レビューの指摘は修正後0件。通常TMPDIRのsymlink差で既存Noa試験が失敗するため、従来と同じ実path条件を使った。3000字fixtureのbot認証には架空app_idを明示し、アプリ証拠が存在するサービスを表現した。検査assertionを緩めていない。

Slack公式資料：[auth.test](https://docs.slack.dev/reference/methods/auth.test/)、[投稿の返却message](https://docs.slack.dev/reference/methods/chat.postMessage/)、[取得時の絵文字表記](https://docs.slack.dev/messaging/formatting-message-text/)。
