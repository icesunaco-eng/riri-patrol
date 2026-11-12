# Noa有効化と即時OFF（v1.0レビュー後・本人承認後のみ）

これは実行前の手順書です。今回、実.env・実Noa URL・実トークンを読み取らず、Noaを起動・有効化していません。既定は `NOA_WAKE_ENABLED=false`、`LLM_ENABLED=false` のままです。

## 1. ON前の確認

- v1.0のレビューが完了し、本人がそのcommit・既存のNoa予定タスク・対象チャンネル・無害な1回の実証を承認したこと。
- 本体がNoa OFFで安定し、初回だけでなく定期実行・3時間のログ同期を確認していること。時間経過だけで合格にしない。
- `C0CFM2HRA5V`（りりの指示部屋）に新しい人の投稿として書く運用。りり自身・bot投稿を起動元にしない。
- 既存 `NOA_FIRE_URL` と `NOA_FIRE_TOKEN` が対象Macの実.envにあること。**値やfire URLを画面・ログ・チャット・GitHubへ出さない。** 環境変数による上書きは不可。
- 既存の予定タスク・認証・利用枠・料金条件を本人が確認。新しい契約、追加課金、接続権限が必要なら停止して別承認。
- 本番stateにNoaのsending/heldや通知結果不明がある場合、外部の履歴を本人が確認するまで試験しない。空stateや新しい依頼で回避しない。

### 値を出さないローカル設定確認の例

承認されたMacで、対象PROJECTとPYを確認した担当者が実施する例です。通信・設定変更をしません。例外本文も表示せず、判定だけ出します。

```python
# cd ~/riri-patrol 後、確認済みPythonで実行する説明用コード
from pathlib import Path
from riri.config import Config
from riri.noa import validate_endpoint

try:
    cfg = Config.load(Path.cwd())
    ready = bool(cfg.noa_fire_url and cfg.noa_fire_token)
    if ready:
        validate_endpoint(cfg.noa_fire_url)
        ready = all(33 <= ord(c) <= 126 for c in cfg.noa_fire_token)
    identity_present = bool(cfg.expected_bot_id.strip() and cfg.expected_user_id.strip())
    print('mode:', cfg.mode)
    print('LLM_OFF:', not cfg.llm_enabled)
    print('NOA_OFF:', not cfg.noa_wake_enabled)
    print('expected_identity_present:', identity_present)
    print('noa_config_shape_ready:', ready)
    if not (ready and identity_present and cfg.mode == 'live' and not cfg.llm_enabled):
        raise SystemExit(2)
except Exception:
    print('local_config_check_failed（値は表示しません）')
    raise SystemExit(2)
```

URLは指定の公式routine fire形式だけ許可します。この確認は**存在と形式だけ**です。トークンの有効性、実際のSlack本人確認、Noaの権限、課金やAPI互換性は証明しません。失敗時は鍵をチャットに貼らず、本人が既存設定を確認します。認証を作り直す許可ではありません。

オフライン確認は `python3 scripts/test_offline.py`。人の新規投稿、重複ts、りり・bot除外、STOP、練習、OFF、429、上限、結果不明、長い停止後の受付境界を架空データで確認します。**通常の `--dry-run` は実Slack・Notion読取を行うため、オフライン試験ではありません。**

## 2. ONにする（本人の別のOK後）

1. [更新手順](upgrade-v1.md)と同じ対象で両STOPを置く。実行中の回の自然終了を確認し、既存launchdを解除。プロセス・ロックなしを確認。
2. 最新state/logs/実.envを非共有の別名バックアップへ保存。結果不明の履歴を本人が確認済みであることを確認。
3. **OFFが本番stateで観測済みか確認**する。.envのfalseだけでは、実行中プロセスが古いtrueを保持している可能性がある。STOP中や `--status`、練習実行だけでは本番stateのOFF観測にならない。
4. OFF観測が未確認なら、本人の許可の範囲で本体をNoa OFFのまま1回正常に回し、再び停止する。この本体巡回はSlack・Notionの通常の書込みを行い得る。無断で「設定確認だけ」として実行しない。本番 `noa_wake.enabled=false` と `off_at` の記録を確認する。
5. 停止・解除中、本人が実.envの既存行 **NOA_WAKE_ENABLED=falseだけをtrueへ**変更。重複行を追加しない。URL・トークン・LLM・モードや他の設定を変えない。
6. 値を出さないローカル設定確認を再実施。mode live、LLM OFF、NoaフラグON、形式確認OK。状態は同じrootの最新本番stateを使う。
7. 両STOPを外し、確認済みの既存launchdを戻す。RunAtLoadで即動く場合、手動実行やkickstartを重ねない。
8. 最初の回が正常終了し、Noaの新しい受付境界を本番stateへ保存したことを確認。**その後に**承認済みのテスト投稿をする。境界ができる前の投稿は古い投稿として起動しない場合がある。

再ONの前にはOFFの本番観測を挟みます。15分以内にOFF未観測のままONへ戻すと、OFF期間の投稿を完全には区別できません。長い空白では安全側に古い受付を取りやめますが、それを短い切替の代用にしません。

## 3. 最初の本番確認は人から1件だけ

本人が承認した人のアカウントから、指示部屋へ新しい通常メッセージで、機密を含まない `@のあ` の確認依頼を1件投稿します。新しい一意の合言葉を使い、必要なら**既存と重ならない正規形式の依頼ID**を選びます。`:cat:...-T`のような形式は使いません。報告を書かせる場合も、対象の報告ページ・正規形式を指定します。

テストは受領確認のみ。ファイル変更・無関係な仕事・別の外部連絡は依頼しません。元メッセージの正確なチャンネルとtsを控えます。返信や編集を追加の起動テストに使いません。

本人が次を照合します：

- 元のtsの処理状態、試行回数、確認済みsession URL、Noa側の実セッション履歴。
- 同じ元メッセージによる**実セッションは1つ**で、次の定期回でも増えていないこと。
- 正常なら `done`、通常1試行。429では明確な拒否後の再試行が最大1回なので試行数は2になり得るが、成功セッションを2つ作ってよいという意味ではない。
- 返答・指定報告まで必要な試験なら、その実際の内容も確認。HTTP応答、ローカルのdone、モデルの自己申告だけで全工程成功とはしない。
- 本体の通常処理とログ同期が継続し、認証エラー・時間切れ・結果不明・余分な通知がないこと。

1回1起動・直近1時間10試行の上限、待ち行列、429待機があるため、返答が遅いだけで失敗と決めて**再投稿しません**。queued/retryなら元のtsを追跡し、sending/heldや応答不明なら直ちに保留。5xx・通信断・結果不明は自動再送しない設計です。新しい合言葉やIDで重複起動を回避したつもりにしないでください。

りり自身・bot・STOP・練習モードで起動しないことはオフライン回帰試験の対象です。実機で追加の負例投稿や設定切替を検証する場合は、その操作も本人の承認を受け、対象・ts・実セッション数で照合します。bot名を本文に書くだけではbot除外の実証になりません。今回これらの実接続試験は未実施です。

## 4. すぐOFFへ戻す

1. **最初に両STOPを置く。** これ以降の通信前ガードで止める。既に送られたPOSTや開始済みNoaセッションは取り消せない。
2. 実行中の回が自然終了したことを確認し、同じlaunchdを解除。終了不明ならSTOPを維持し、結果不明として扱う。強制終了・再起動で成功/失敗を推測しない。
3. 最新state/logsを保全し、実.envの既存行を `NOA_WAKE_ENABLED=false` に戻す。他の設定やNoa URL・トークンを変更・削除しない。
4. 本体も止めておく場合はSTOPとlaunchd解除を維持する。この時点でNoaのOFF観測がstateへ保存済みとは限らない。
5. 本体だけ再開する本人のOKがある場合は、同じ最新stateを使い、Noa OFF・LLM OFFで再開。本番の1回で `noa_wake.enabled=false` と `off_at` が記録されたことを確認する。古いqueued/retryは起動せず取りやめ、sending/held/done・重複防止・試行数は残る。
6. 再びONにするには原因確認と本人の新たなOKが必要。OFFの観測を確認してからON手順へ戻る。

**実.envをfalseに編集するだけでは、既に動いているプロセスの設定は変わりません。** OFFのためにstateを削除・巻き戻したり、Noaの予定タスクを削除したり、トークンを失効させたりしません。外部セッションの停止が必要なら本人が別途判断します。

## 5. 保留・失敗時

`--status`はローカルの保存済み状態だけを表示します。設定・stateのエラー時は動作ログへ追記する場合がありますが、実履歴の取得やstate修復はしません。Noaの実履歴は本人が確認してください。`--resolve noa ... reviewed` は確認記録を残す機能で、成功やキャンセルを証明せず、再試行も許可しません。単に警告を消すためには使いません。

実APIのpreviewヘッダー互換性、実認証、料金・アカウント上限、Macの実行環境はこの開発作業では未確認です。API拒否を別URLや別トークンで迂回せず、原因・完了済みの範囲・不明な結果を報告して止めます。
