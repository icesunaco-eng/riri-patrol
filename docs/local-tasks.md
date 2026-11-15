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
