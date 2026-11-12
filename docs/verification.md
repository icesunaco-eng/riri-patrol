# りり見回りプログラム v1.0.2 検証報告

2026-11-12 JST。基点は v1.0.1 `f577055de586123008c46cd731e0578ef2c43a2e`、tree `df49ef1a66d8dbff4232b45edc0ff713e4831d7c`。ローカル `bbe28dfac43ba62f1d8d340fc9e2c90d6afaacdf` と同じ成果から修正しました。

本人から15:08 JSTに、うさのv1.0.1レビュー（84ファイルtree一致、484件成功との報告）とY-01〜Y-03を受領。実MacやAPIの現在の状態を開発側が確認したことはありません。

## Y-01：安全な案件ページ表記で完了を止めない

- タイトルのみとNotionページメンション由来のplain_textを、エスケープした名前として表示。
- HTTPSの正確な `app.notion.com` / `notion.so` に加え **www.notion.soを許可**。名前＋URL/URLのみの既存表示は維持。
- 安全な外部・非許可・形式不明URLは「案件ページ：（Notion以外・形式不明のURLのため省略）」という固定文に置換し、報告と依頼の完了を妨げません。ページ名を外部に取得しません。
- 省略する値もraw/HTML/percent decodeで先に検査。メンション・既知の秘密・危険な制御文字・過度の多重符号化・過長は `ProjectPageUnsafe` として保留。案件ページの元値と表示は1500文字上限。報告本文全体の既存安全検査は維持。
- prepareの最初に同じ案件ページ検査を実行し、危険なら指示・Notion状態更新・自身あて通知より前に止めます。案件ページに原因がある通知は「依頼の案件ページ欄を確認してね」に分け、危険な本文を転記しません。報告本文自体の危険は元の報告を確認する通知を維持。
- 保存snapshotは変更しません。元の依頼本文を勝手に書換えず、指示本文は従来のままです。

### 既存の保留からの復帰

旧v1.0.1の保留理由は安全なURL形式違いも危険な本文も `unsafe_content` であり、原因を判別して無条件解除できません。**既存保留を削除・自動解除せず、本人が確認した訂正版の報告を新digestとして再検証**します。安全な旧snapshot（タイトル/www/外部URL）は新ルールで通るため、snapshotを変えず完了できます。危険なsnapshotは訂正版でも保留します。

元の保留履歴、seen、送信済み/送信不明/queued outboxは保持し、同じ版を再送しません。旧保留履歴は訂正版への置換後もsupersededとして残ります。これは結果不明の投稿の再送許可ではありません。

## Y-02：複製1件だけの隔離

新 `riri/relay.py` に、送信済み原本の正確な末尾markerを確認して複製する処理を分離。queue/原本検査の `UnsafeContent` だけを受け止め、複製を既存形式の `notice / held / unsafe_content` と安全な代替文で保持します。解除選択肢はcancelのみで、自動再投稿しません。

- 原本と依頼状態は変更せず、他の依頼は継続。
- 危険な原本は元記録を改変せず保持し、新しい複製記録・固定警告・ログへその内容を転記しません。
- 警告は `relay-unsafe:{ID}` の安定キーで一度だけ。保留保存後の中断は警告だけ回復。既存警告がsending/held/postedなら変更も再送もしません。
- 既存複製の全状態・本文は無変更。アカウント違い、STOP、I/O例外などは握りつぶしません。
- 原本1件/投稿最大4件、限定メンション、parse:none/link_names:false、末尾rp照合、送信不明時の重複防止は維持。

## Y-03：手順の明確化

[更新・復旧手順](upgrade-v1.md)に以下を反映しました。
- 確認済みの `"$PY" scripts/test_offline.py` を使用。Mac標準python3へ戻さない。
- --statusではICE_MENTION_USER_IDの不備は検出されません。本人が実接続確認を許可した段階で `"$PY" patrol.py --dry-run` のstatus=okを確認。STOPで終了したstoppedは合格ではありません。実API読取と練習state/logsへの書込を伴うため、今回実施していません。
- v1.0へ戻す前に報告先のqueued/sendingが無いことを確認。あればSTOP維持で本人が実送信結果を照合します。旧版で正式メンション付きqueuedがheld/unsafe_contentになるとcancelのみ。sendingを未送信と決めつけてcancel/再送しません。
- 最新state/logsを保持するコード復旧のみ。v1.0.1へ戻せばY-01の形式問題も戻るため、状態形式互換と動作上の問題の不存在は別です。

## オフライン試験

**530件成功、失敗0・エラー0・スキップ0。** 既存484件のIDは維持。Y-01の仕様変更に合わせて既存の期待値・危険fixtureだけを更新し、46件を追加しました。

内訳：compose 14、relay 11、統合flow 18、v1.0.1互換3。

```bash
python3 scripts/test_offline.py
```

開発環境のPython 3.12で、socket生成・接続・名前解決を禁止して実行。実トークン・実.env・実stateは使いません。単体試験内のlive/practice分岐は架空の依存先だけの模擬試験です。通常のpatrol/dry-run/demo実行はしていません。

重点検証：
- 名前のみ、www.notion.so、外部URL、ページメンション相当のタイトルの各ケースで完了報告1通、依頼状態done、その後の巡回でも再送なし。
- エンコードした危険値・秘密・制御文字・過長は指示前に専用通知で隔離。危険なsnapshotと報告本文は自動完了しない。
- 旧保留を維持し、訂正版で安全なsnapshotをそのまま使って完了。危険なsnapshotは訂正版でも保留。
- 複製例外が他の依頼を止めない、固定警告1回、未知結果を再送しない、原本・既存outbox無変更。
- stateの実装・Noa・errors・Config・正式メンション検査・Slack transportが基点とバイト一致。
- 双方向の状態読書きで、複製保留/送信中警告/旧保留/全outbox状態/Noa回数/ログを保持。

独立レビューでも530件を再実行して成功。62 PythonファイルのPython 3.11構文/compile、plist、diff checkと、既存484件の試験ID維持・重複なしを確認済み。重大な未解決指摘はありません。全試験名と結果は [test-results.txt](test-results.txt)。

## 互換性の根拠

[基点manifest](../tests/fixtures/v101_state/manifest.json)にv1.0.1 commit/treeと6つの不変ソースのGit blob/SHA256を固定。v1.0.1のStateStoreは同梱のv0.8原本とバイト同一なので、その実際のreaderを別モジュールで実行し、v1.0.2と双方向保存・読取を検証します。必須stateキー・schema version・Noa状態は変更していません。Gitやネットから過去コードを試験時に取得しません。

新しい安全な複製保留も既存notice形式のため読めますが、**形式互換は旧コードが新しい動作を再現する保証ではありません**。v1.0への復旧時の正式メンション制約は前版から継続、v1.0.1ではY-01問題が再発し得ます。実機の状態健全性・復旧成功は未検証です。

## 制約・共有範囲

- 実API、Mac配置、launchd、実.env変更、Noa ONは実施していません。配備・設定はたかむねが行います。
- NOA_WAKE_ENABLED=false・LLM_ENABLED=false、既存の厳密なメンション範囲を維持。Y-04〜Y-08への追加変更は行いません。
- botの正式メンションからdotが自動起動するかは未検証。
- 本人が許可した同一private GitHubへコードと検証資料を保存。既に承認されたSlack/Notion識別子と運用手順を含みますが、**新しい実ID・実トークン・実.env・実fire URL・実state/logs・バックアップは含みません**。追加試験は架空値です。
- 反映後はcommitとmainを読戻し、試験済みローカル成果とのtree一致を確認して報告します。

参照： [本人のY-01〜Y-03依頼](https://w1790864994-fie712152.slack.com/archives/D0CBN5T67V2/p1794463725243209) ／ [v1.0.1検証資料](https://github.com/icesunaco-eng/riri-patrol/blob/f577055de586123008c46cd731e0578ef2c43a2e/docs/verification.md)
