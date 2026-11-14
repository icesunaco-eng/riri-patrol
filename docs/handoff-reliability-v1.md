# りり→のあ受け渡し修正 v1.2.1

正本：[要件定義書](https://app.notion.com/p/414f1dff94dc81f78662e5a652096b73)／[案件・実施結果](https://app.notion.com/p/414f1dff94dc81f7a9b4c25e951e11d4)。2026-11-14の直接指示で、起動後の結果未観測・未知ID報告による全ページ抑止・内容重複を修正し、作業した会話URLの記録と転送を追加した。旧履歴は保持する。

## 配送、実行、依頼完了を分ける

- 旧配送 `done/session_verified` のstatus・理由・証拠は変更しない。receiptのない旧実行はstatus表示で `legacy_unobserved` とする。過去ログから自動的に成功・失敗へ移行しない。
- 新規local配送は `done/local_spawned`。ここでのdoneは起動受付の意味で、応答確認や案件完成ではない。元入力の `run_id/input_sha256/local_ref` を外部効果の前にstateへ固定する。
- 独立workerが最小環境・shellなしでClaudeを1回起動し、専用stdout JSON・stderr・終了コード・時刻をprivate receiptへ原子的に保存する。巡回本体は子の完了を待たない。
- `cli_exited` はCLIの正常な終了。`rate_limited`、`authentication_failed`、`cli_failed`、`cli_spawn_failed`、`stopped_before_start`、`unknown` を別表示する。途中成果や後からの正式報告は別の軸で共存する。
- worker起動後にもSTOPを確認し、CLI起動前のSTOPは実行前停止として残す。途中の既存Claudeや無関係なプロセスをkillしない。
- `--status` は通信も保存もしない。run ID・stateの入力hash・manifest/receiptの入力hash・専用入出力hash・schema・権限・日時・exitの整合を確認する。欠落・不正・他run・symlinkは不明とし、最新の共通会話ログで補完しない。
- 次の巡回で新runの結果を保存し、未完了の依頼に中断・不明がある時は要確認とする。通知はrun単位の既存outbox経路を使う。過去の依頼をqueuedへ戻さず、自動再送・利用上限後の再試行はしない。

## 未知ID報告と内容重複

識別不能な報告は `quarantined_reports` に `isolated/needs_review` としてblock ID・digest・ページ本人・理由を保存する。本文はNotionに残し、stateや通知へ複写しない。判別可能な候補ID、報告者不一致、矛盾する欄はそのIDと報告者の組だけ自動完了を抑止する。安全でない本文への従来のheldは保持する。

同対象（案件ページまたは具体的な使用対象）・宛先・実行欄が一致する活動中の別IDを比較する。保存済みactive/postedの証拠をページ位置より優先する。後発の未配送依頼だけを `held/content_duplicate` とし、元IDと理由を記録する。送信直前にも元ページを再読して検査する。対象のない定型文・一般的な「なし/未確認/架空データのみ」、別対象・別作業・別宛先・終端済み過去依頼は自動的に重複としない。

既postedの履歴は変更も再送もしない。重複保留は元依頼が終わっても自動解除しない。`--resolve instruction ... posted|not_posted` は結果不明の投稿を手動確認する入口なので、既知未配送の内容重複の解除には使えない。原本と成果を確認して、別途再開対象を決める。

## 作業場所URL

完了報告の標準欄：

```text
案件ページ：
作業場所URL：
作業場所URLが取得できない理由：
結果：完了／一部完了／できなかった
```

URLは実際に業務を行った会話・実行セッションを示し、成果物やNotion案件のURLでは代用しない。有効な主作業場所1件を報告結果と一緒に、たかむねりりあいす向け要約へ無省略で転記する。複数場所の関係は元報告に残す。URL型が安全であることと、そこで当該業務を実施したことの実証は別である。

全報告ページの最初の読取成功後に `state.work_url_policy` のlegacy baselineをblock ID+digestで固定する。旧報告は未記載でも旧報告と明示して処理する。その後の新規・更新報告の不足・不正・未対応URLは、当該IDと本人だけを追記待ちとする。結果報告は失わずDMへ不足を表示する。同じblockへの追記も、先頭へ追加された有効な訂正版も、新しい結果として処理する。古い不足履歴はsupersededとして保持する。

対応形式は `chatgpt.com` の既存会話、`claude.ai/chat/UUID`、実際に取得された `claude.ai/code/cse_...`、`grok.com/c/UUID`、公式の `codex://threads/UUID`。危険scheme・userinfo・port・query・fragment・成果物・新規composerは許容しない。値の一部を削って別URLに変えない。対応外は原本に保持し、追記待ちへ分ける。形式だけで実在・到達・閲覧権限を主張しない。

Codexの公式リンク形式は[OpenAIのCommands](https://learn.chatgpt.com/docs/reference/commands)に記載され、appから取得した技術的thread IDを使う。local ClaudeのUUIDからWeb URLを捏造しない。[Claudeのsession一覧は環境ごとに分かれる](https://code.claude.com/docs/en/sessions)。[claude-cli deep link](https://code.claude.com/docs/en/deep-links)は新規ターミナル起動用なので、過去の実作業会話の証拠にはしない。URL取得のために公開share・Remote Control・権限変更を自動で行わない。

## 試験と適用

v1.2.0の初回巡回では旧報告との照合で🤍20261114-1〜5が完了へ進み、URL baseline41版が有効になった。一方、URL固定案内が本文の1500字予算を食う回帰により、新しい依頼1件が未準備・未送信のまま保留された。v1.2.1では旧材料と従来固定文の1500字を先に検証し、固定案内71字＋改行1字だけを別予算にする。総上限1572字、LLMのcanonical一致、危険文字の拒否、本文を切り捨てない境界を保持する。

その実依頼は、元Notion本文の更新が検知され、14:33の既存の訂正処理で配送へ進んだ。開発側で原本の文面や状態を変えたり、再送したりはしていない。追加の手動解除機能は配備せず、既存の保留条件と原本訂正後の全安全検査を保持する。

全オフライン試験はsocket生成・接続・名前解決を禁止する。worker試験の子は隔離fake executableのみで、Claude実体や実APIは呼ばない。Macの `/var` と `/private/var` のfixture差はTMPDIRを実パスへ固定して整合させ、製品コードの検査や試験を緩めない。

適用は稼働版3df1e113とcleanを確認し、STOPで新しい巡回を止め、lock解放後に元の追跡ソース・最新state/logsを保全する。コードは既存履歴の子孫へff-onlyで更新し、.env・plist・秘密値・stateの既存項目は書き換えない。通信なしstatusと版を確認して同じ既存300秒巡回へ戻す。過去のstateへ巻き戻さない。試験依頼やSlackのテスト投稿は追加しない。

旧v1.0.2のresolveソースは互換fixtureへ元のhashのまま保全した。新resolveの内容重複ガードは現行の回帰試験で別に検証する。状態形式互換と、旧版が新機能を実行できることは別である。

## 残る確認範囲

新しい実依頼での連続成功、全ヒロインズによる次のURL記入、DMからの実クリック、同一sessionへの別run並行resumeを抑えるsingle-flightは今回のオフライン成功と区別する。run専用出力は結果の混同を防ぐが、同じ会話への並行作業自体を制限する機能ではない。CLI正常終了と本人の完成認定も別である。
