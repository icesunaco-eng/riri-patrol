# りり経由の固定A実装とA成果物のまむ検査

りりの受付対象はこの案件の「みに→みきの固定A」「みき→まむの固定版A検査」。正式な依頼本文の「使うもの」欄に `local-run=<run ID>` を1回置く。依頼ID、依頼者、担当、案件URL、run ID、仕様・成果物の一致を確認する。本文5欄計3000文字／ヘッダー1500文字には既存composeの検査を使う。

実行時の作業ディレクトリは `/Users/sunako.jp/riri-patrol`。本機の既存launchdと同じ `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 -B -m riri.local_tasks` にJSONを標準入力する。モデルはこのホストの資格情報を受け取らない。

- `stage` は限定ジョブの準備だけを保存する。Aのモデル試験を開始できる状態になった時点で、次のように指定する。次の通常巡回が正式な依頼ブロックを読み、該当する作業を登録・起動する。早く進めたい場合の `register` は同じ項目で正式依頼を読み直して登録し、`tick` が1件を選ぶ。
- まむの依頼を登録するには、既存みきRelayに新規の正式依頼を保存し、`/Users/sunako.jp/mamu-AI/state/inspections/jobs/<run ID>/job.json` に固定した仕様、対象SHA、前段runを用意する。run IDは `mamu-a-review-` とUUIDの32桁小文字hex。巡回で依頼と固定jobの一致が取れ、前段の正式受領と解放が揃った時だけ起動する。
- `status` は `run_id`・`request_id` を指定して読む。`cancel` も同じ組で指定し、実行中は所有CLIの中断処理に渡す。`tick` は原則受付順に実行可能な1件を選び、未反映outboxは最終照合が古い順に1件を処理する。依存待ちは飛ばす。実際のheavy lockは各モデルworkerが取得する。
- `report` は `actor,request_id,run_id,artifact_sha256,result_sha256,stage,quality,summary` を受け、outboxをfsyncしてから登録済みの本人の「完了報告」へ書く。みきの `inspection_wait` は「一部完了・検査待ち」。まむの `reviewed` / `inspection_completed` は検査完了、`inspection_incomplete` は検査未了。検査の受領とコード品質は別の欄に記録する。
- `report_state=received` のreceiptには依頼ID・run・actor・成果物と実結果のSHA、正式ページのID・ブロック・本文SHA・りりの受領日時がある。りりの担当モジュールがそのページを再読し、本文が一致して初めて発行する。通常巡回によるSlack通知や利用者の閲覧は、このreceiptが立証する事項には含まれない。
- `pending` は未反映、`unknown` は送信結果不明で、同じrun・同じIDで照合する。結果不明のappendは再書込みしない。確定した未送信や読取失敗は軽い巡回で再試行する。未届の通知のためにモデルを起動し直さない。正式ページとreporterは登録済み固定値に限定する。

```json
{"op":"stage","actor":"miki","request_id":"🩵20261115-1","run_id":"gemma26-A-v1","spec_sha256":"cf4c8221919b2c066fe29fe44751efcf2e1c7591fa86ffe289c95f31770fb864","prior_run":null,"artifact_sha256":null}
```

runごとのりりの台帳とoutboxは `state/local-tasks/` の600モードのファイルで、一般の過去業務stateと独立。正式ブロックを消したり、未確定のrunを別IDでやり直したりしない。子の終了・排他の再取得を確認できない間は次のモデルへ渡さない。配備だけではAの受付を有効化しない。バックアップからの復旧対象はコードのみで、現行.env、STOP、launchd、稼働中の依頼・DB・journalの値は維持する。

コード配備用の `scripts/install-local-tasks.py <40桁commit>` が、既存releaseのHEAD、清浄性、固定リモート、変更5ファイル、fast-forward可否を確認する。`--apply` は既存patrol.lockが空いた時だけコードを反映し、全5ファイルのSHAと新プロセスのモジュール読込みを照合する。launchdの再登録は不要。

## 中断したAの残り2回を引き継ぐ場合

2026-11-15の正式依頼 🩵20261115-1 には1回だけ明示継続を登録できる。元run `gemma26-A-v1` の9回のread、応答を保存する前に中断した10番目のintent、残り2回という証拠を照合する。元の依頼ブロック・run・journal・空のactor-outputはそのまま残る。

新版みきの正式配備と元runの解放を確認した担当者が、りりの上記JSON入口へ `op=continue`、`request_id=🩵20261115-1`、`prior_run=gemma26-A-v1`、`run_id=gemma26-A-v1-cont1` と、現在の `/Users/sunako.jp/miki-AI/install-manifest.json` の実測SHA-256を `actor_manifest_sha256` として渡す。同じ登録を読み返しても再実行されない。登録後、通常のtickが新runを選択する。新runのstatusで元run・確定した10/12回・残り2回・元証拠と配備manifestのSHAを確認できる。通常巡回やstageはこの継続を作らない。起動前に原本または配備が変更された場合は照合で保留する。

実装候補があるときは `inspection_wait` と候補のSHA、検査前の品質区分を付ける。実装未了で候補がない場合は `implementation_incomplete` と `failed` または `unverified` を記録し、固定した対象のSHAと実際に保存した診断のSHAを届ける。この段階の受領はまむ検査への依存を満たさない。継続で候補を得たとき、まむの固定jobの `prior_run_id` およびりりの `prior_run` は継続runを指す。

## 初回の独立検査を受領した後の同一依頼訂正

この経路は今回のAの第2版1回に限る。みきとまむの初版の報告本文・受領・成果物SHAは保存する。第2版も各自の初版と同じ正式依頼ID、同じNotion報告ブロックを使い、版ごとの独立したローカル受領を残す。

1. `prepare-amendment` は `request_id=🩵20261115-1`、`prior_run=gemma26-A-v1-cont1`、`run_id=gemma26-A-v1-rev1`、`review_run=<初版まむrun>`、`diagnostic_sha256=<A-frozen-owner-diagnostics.jsonの実SHA>` を受ける。固定したホスト追試の仕様SHA・入力SHA・まむ実結果SHA・不一致ケース、初版の双方の正式受領と現在のNotion本文SHAを照合する。りりの `state/local-tasks/gemma26-A-v1-rev1.amend-intent.json` に、第2版の要否の根拠を `post_model_host_assessment` として、まむの原意見と分けて保存する。状態 `awaiting_new_budget` は巡回・tickの実行対象外。何度読んでもモデルやNotionの更新は発生しない。
2. 元のAの監視時間が満了した後、実機の訂正生成には、新たに承認された監視時間と対応するみき側の実行入口が必要。りりの訂正準備・配備は新予算を発行しない。現在の実装は、将来の実行で作成された証拠の取込みから担当する。
3. 将来のactor側は `miki-AI/data/development/gemma26/judgments/gemma26-A-v1-rev1.json` に `request_id, run_id, prior_run, review_run, diagnostic_sha256, spec_sha256, prior_artifact_sha256, prior_result_sha256, review_result_sha256` と、`decision=revision_required, reason, new_budget_approved=true, budget_start, budget_deadline`（timezoneつき）を残す。終了後の通常の `jobs/<run>.json` は `revision_judgment_sha256` でこの原本と結ぶ。別の `gemma26-development/copies/A-rev1/qbench/input_validation.py`、同じモデルrevision、実際の生成と終了、journalの適用記録と新SHAの照合が必要。単独のホスト文や既存成果の再表示では受け付けない。
4. みきの `amend` は1の `request_id,run_id,prior_run,review_run` に `expected_report_sha256=<みき初版の受領本文SHA>, judgment_sha256=<新しい正式判断ファイルのSHA>` を加える。りりは完了した実結果を照合してfsyncしたoutboxを作成する。新runを伴う第2版の報告には、旧run・旧報告SHA・訂正理由が入る。同じ旧本文、ID、編集版を送信直前に再読し、唯一の既存ブロックを更新する。更新後の本文全体が同じブロックで確認できた時だけ第2版receiptを発行する。送信結果不明なら未解決のまま再読に限定する。NotionのGETとPATCHの間にはAPI側の原子的な条件更新はない。直前と直後の照合を記録する。
5. 第2版の成果物をまむへ渡すときは、まむの制御フォルダに従来同様の別runの固定jobを作成し、`op=review-version`、`request_id=<まむ初版の同じ正式依頼ID>`、`run_id=<新しいmamu-a-review-UUID>`、`prior_run=gemma26-A-v1-rev1`、`review_run=<初版まむrun>`、`expected_report_sha256=<まむ初版の受領本文SHA>`、`judgment_sha256=<みき第2版と同じSHA>` を渡す。これは通常の巡回から実行可能な**検査の登録**。実行許可と準備が揃った時にだけ使う。新SHAと固定jobを照合し、届いた検査結果も同じまむ正式ブロックの第2版として再読する。第2版の `status` に `revision` と版番号付きの `receipt` があり、元のrunのstatusと照合できる。

### 第2版の受領とworker解放の順序

みきは生成原本、採否、対象、検証結果を固定turnのoutboxに確定後、既存の `amend` へ `phase=before_release, model_run_id, actor_outbox_sha256` を付けて送る。りりは実ファイル、期限内の生成時刻、個別のCLI/workerの親子関係と排他、成果物SHA、みきが適用した記録を照合する。りりのoutboxはNotionアクセス前にfsyncする。初版SHA・同一正式ブロックを読み直して第2版を受領しても、状態は `awaiting_actor_release / owned`。受領時に同じworkerを観測したかは `receipt.worker_alive_at_readback` に記録する。モデルが実際に解放前にACKを受けたことはworker自身のhandoff記録でも検証する。

通信不能はpending、送信結果不明はunknownとして同じ版・同じブロックを照合する。不明なPATCHを再試行しない。みき終了後は同じ `amend`（phase省略）がタスクと生成結果のSHA、worker終了、CLI終了を確認する。元CLIがまだ終了中なら `awaiting_cli_exit` が残り、軽量の巡回で終了を再確認する。期限後に行えるのは保存済み結果の受領・読戻し・解放照合。確定失敗の診断または保存済み生成も正式報告できる。結果不明を成功や確定失敗に振り替えない。

まむの `review-version` は、みきの第2版受領と解放に加え、案件の `A-quality.json` に同一run・対象・判断SHA、固定37/37、追加7/7、対象範囲合格があることを登録時と起動直前に確認する。まむの固定jobの版・判断SHA・絶対期限も照合する。まむは実際の検査結果とoutboxを保存してから同じ正式ブロックへ報告し、続けてworkerを解放する。期限直前や送信結果不明時は解放を先に完了してから同じ版を照合できる。

### Aの2回目の訂正とまむの再検査

`gemma26-A-v1-rev2` は同じ依頼の前版 `gemma26-A-v1-rev1` を参照する。既存の `prepare-amendment` に新run、前run、正式依頼ID、初回まむrun、前版の `A-quality.json` のSHAを渡す。前版みきの受領・解放と、別SHAを検査した初回まむの原本をそれぞれ照合する。新しいみき判断には第3版と旧正式報告SHAを指定する。対象の別コピーは `A-rev2`。みきの受領は従来の `amend` で同じブロックの第3版となる。

まむの `review-version` はみきの第3版を `prior_run` に指定し、まむ自身の初回意見を前版にする。登録・起動前に今回の `outputs/A_rev2_20261115/A-quality.json` を照合する。対象と判断のSHA、固定37件、従来の診断7件と空連番の計8件、対象範囲、新予算の期限がすべて合格した場合のみ起動できる。まむ自身の正式報告は同じ元ブロックの第2版となる。

### 通常の正式依頼で受ける限定Python案件

許可済みの仕様・検証器・変更可能ファイル・返却先をプロジェクトmanifestへ固定し、正規の依頼者が使用する新しい正式IDと実行runを `ops-stage` で登録する。正式依頼の「使うもの」に `local-run`、`manifest-sha256`、`project-id` を記す。名簿の名前・色・宛先、案件、GitHub欄、資料、既存の文字数上限が一致すると、通常巡回が同じ正式ブロックを読み、既存待ち行列へ渡す。受付、報告、返却通知にはモデルを使わない。

`status` は正式IDとrunを指定し、受付元ブロック、実行状態、資源状態、内容品質、正式結果、返却経路を確認する。取消しは同じ組の `cancel`。未起動の場合は起動を止め、実行中は既存監視が所有者へ終了を通知する。資源の事前確認が不成立でworkerを起動していない場合のみ、5分以上の間隔で最大3回を確認する。生成開始が不明な場合は再ロードせず保存物を照合する。未送信の確定時のみ送信でき、送信成否不明は同じ正式ID・同本文の読戻しを待つ。

初回案件は隔離作業コピーの指定Pythonファイル1個。みきの固定19件合格でまむへ正式な子依頼を出す。まむに修正根拠と対象SHAに合致する客観的な再現記録があれば、新しい子依頼でみきへ戻す。予算は親の45分、合計4run・修正1回、各worker180秒まで。返却のチャンネル、報告の正式block、投稿時刻、本人が受け取ったことの確認を分ける。みに宛ては既存のあいすかたかむねによる手渡しを明記する。別案件を追加する前に仕様・許可範囲・受入試験とactorの対応能力を固定し、両者が対応していない仕事を汎用コマンドとして登録しない。
