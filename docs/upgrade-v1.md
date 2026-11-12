# v0.8 本番から v1.0 への更新・復旧手順

## 前提と承認の境界

2026-11-12 11:43 JSTの本人からの引継ぎでは、りりのMacは同日11:40ごろから **v0.8 `5399e1a`、RIRI_MODE=live、LLM_ENABLED=false、Noa OFF** で稼働し、v0.9は未配備とのことです。これは本人の報告であり、開発側がMacや実サービスを見て確認した結果ではありません。

この手順書は準備物です。記載したMac操作・実接続確認は今回実施していません。**v1.0のレビュー完了と、たかむねの更新・再開のOKを得た担当者だけ**が、対象Macで一段階ずつ実施します。Noa ONはさらに別のOKが必要です。コードを更新しただけでNoaをONにはしません。

- v0.8の復旧先は完全なcommit `5399e1a580de27bddcdd34cade1b63ad392f8dd1`。v0.7への復旧とは異なります。
- 更新先は、今回のレビューで承認された **v1.0の完全なcommit ID** を使います。単に「最新main」や版番号だけで判断しません。
- `.env`、state、logs、既存launchd plistはMacの原本を保全。`.env.example`やテンプレートで上書きしません。
- `git reset --hard`、`git clean`、状態の初期化・キー削除・履歴の巻き戻しで問題を回避しません。
- 既存のトークン・接続・権限・課金条件を変更する手順ではありません。新しい認証・権限・支払いが必要なら別途確認して停止します。

## 0. 対象を確定する（まだ再開しない）

本人の過去報告ではプロジェクトは `~/riri-patrol`、Pythonは `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14` です。**現物と一致することを確認してから**使います。Macの既存launchdラベル・plist・実行引数・`--root`・WorkingDirectoryも確認してください。値が違う場合、推測で別のジョブを止めません。

以下の `PROJECT`、`PY`、`SERVICE`、`PLIST` は確認後に設定する作業用変数です。これはアプリ設定の上書きではなく、アプリは引き続き実.envだけを読みます。

```bash
PROJECT="$HOME/riri-patrol"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
# 次の2つは既存登録がこの値と一致する場合だけ使う
SERVICE="gui/$(id -u)/com.heroines.riri-patrol"
PLIST="$HOME/Library/LaunchAgents/com.heroines.riri-patrol.plist"
cd "$PROJECT"
git status --porcelain
git rev-parse HEAD
"$PY" --version
```

追跡ファイルに未保存変更があれば上書きせず停止して整理します。想定外のcommit、別のroot、共有されたバックアップ先、シンボリックリンク等があれば先に確認します。既存plistの実行先がこのPROJECTとPYを指すこと、別の巡回・手動実行がないことを確認。plistやlaunchctlの全環境変数をチャットへ貼らないでください。

実.envの `STOP_FILE` が指定されていれば、その**実際の停止ファイルの絶対パス**も確認して `STOP_PATH` とします。未指定なら `STOP_PATH="$PROJECT/STOP"`。デフォルトSTOPと設定STOPの両方を扱います。別のrootを使っている場合、この例をそのまま続けません。

## 1. STOP → 実行終了 → launchd解除 → 再確認

```bash
touch "$PROJECT/STOP" "$STOP_PATH"
```

デフォルトSTOPは新しいCLI実行を止めます。実行中の回は設定されたSTOP_FILEを通信前に確認するため、カスタムSTOPがある場合は両方必要です。

**実行中の回が自然終了するまで待ちます。** 通常の上限は4分ですが、経過時間だけで終了扱いにせず、対象プロセス・ロック・最後の動作記録を確認します。STOPは既に送信した通信や起動済みNoaセッションを取り消しません。

実行中のサービスを `bootout` すると途中で終了させる可能性があるため、先にSTOPと自然終了を確認してから、確認済みの対象だけを解除します。

```bash
launchctl bootout "$SERVICE"
# 戻り値を確認する。全出力の貼り付けは不要
launchctl print "$SERVICE" >/dev/null 2>&1
```

最後のprintが「未登録」による失敗となることを確認します。権限不足など別の失敗を「停止できた」と解釈しません。対象patrol.pyのプロセスがないことを再確認し、同じrootの `state/patrol.lock` が他プロセスに保持されていないことも確認します。ロックファイルを削除して空いているように見せません。終了・解除が不明なら更新せずSTOPを維持します。

途中終了・通信結果不明があった場合は、その事実と最新state/logsを保全。再送や新しいテスト投稿で確かめず、履歴照合が済むまで再開しません。

## 2. 最新の状態と設定を原本のままバックアップ

停止後に、同期・共有対象外で本人だけが読める保管先へ、**stateディレクトリ全体、logs全体、実.env、既存plist、現在のcommit**を別名で保存します。秘密を含むバックアップはGitHubやチャットに送らないでください。

例（保管先の非共有・容量・アクセスを確認後。コピー前にstate/logsが実在することを確認）：

```bash
umask 077
# この親フォルダが本人専用・非共有であることを先に確認
BACKUP="$HOME/riri-patrol-backups/$(date +%Y%m%d-%H%M%S)-before-v1"
mkdir "$BACKUP" || exit 1
cp -pR state logs "$BACKUP/"
cp -p .env "$BACKUP/.env"
cp -p "$PLIST" "$BACKUP/launchd.plist"
git rev-parse HEAD > "$BACKUP/code-commit.txt"
```

コピーがエラーなく終わり、全ファイルの内容・件数が一致することをローカルで照合します。比較結果は成功/失敗だけを扱い、設定やログ本文を外へ出しません。コピーしただけで確認済みとはしません。

**本番の `state/state.json` がない、破損、読めない場合は停止。** `--status`はファイルがないと初期状態を表示し得るため、「0件」「エラーなし」を既存状態の健全性の証拠にしません。本番と練習用stateを取り違えないでください。

## 3. 承認されたコードだけを更新する

STOPとlaunchd解除を維持します。GitHubから取得する操作であり、巡回のSlack・Notion・Noa APIは呼びません。新たなログインや権限追加を要求されたら別途確認します。

`APPROVED_COMMIT`には、レビュー済みv1.0の完全なcommit IDを本人が指定してください。既存ブランチと追跡先が確認済みmainでない場合は、推測で切り替えず停止します。

```bash
git fetch origin main
test "$(git rev-parse FETCH_HEAD)" = "$APPROVED_COMMIT" || exit 1
git pull --ff-only origin main
test "$(git rev-parse HEAD)" = "$APPROVED_COMMIT" || exit 1
git status --porcelain
"$PY" -c 'import riri; print(riri.__version__)'
"$PY" scripts/test_offline.py
```

fetchからpullの間にmainが変わった場合も、後段の照合で止めます。承認commitと不一致、fast-forward不可、dirty、試験失敗なら実行しません。テストは架空データ・通信禁止で、Mac上の実stateを対象にしません。版番号1.0.0だけではcommit照合を省略できません。

## 4. 設定・互換性・読み取り確認

更新時は **RIRI_MODE=live、LLM_ENABLED=false、NOA_WAKE_ENABLED=false** を維持します。既存トークン、本人確認ID、ページ・部屋、root、STOP、Python、plistを差し替えません。値を表示せず有無・フラグだけ確認します。

STOPを置いたまま、通信しない状態表示を確認します。

```bash
"$PY" patrol.py --root "$PROJECT" --status
"$PY" patrol.py --root "$PROJECT" --dry-run --status
```

前者は本番、後者は練習の保存済み状態です。件数、保留、最終観測とバックアップを照合。過去のsending/held・重複防止・未反映ログ等が消えていないことを確認します。`--status`は実サービスの現在状態を確認するコマンドではありません。

正常時はローカル状態の読取だけですが、設定・stateが不正なときのエラー処理はローカル動作ログへ追記することがあります。常に全ファイル無変更とは扱いません。state自体を修復・初期化する機能ではありません。

### `--dry-run` の注意

`--dry-run`は**オフラインではなく**Slack・Notionを読み取ります。外部書込みは行いませんが、練習用stateとローカルログは更新します。STOPを置いたままでは即終了するため、それを接続確認成功と数えません。

本人がこの実接続確認を承認し、launchd解除・他プロセスなし・Noa OFFを再確認してから、STOPをこの単発確認の間だけ外します。終了・失敗・割込みのいずれでも直後に両STOPを戻します。SIGKILLや端末障害では自動復帰できないため、その場合は手動でSTOPを戻すまで進めません。

```bash
(
  restore_stops() {
    touch "$PROJECT/STOP" "$STOP_PATH" &&
      test -e "$PROJECT/STOP" && test -e "$STOP_PATH"
  }
  trap 'restore_stops || printf "%s\n" "STOP復帰失敗。再開せず手動確認" >&2' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  rm -f -- "$PROJECT/STOP" "$STOP_PATH" || exit 2
  test ! -e "$PROJECT/STOP" && test ! -e "$STOP_PATH" || exit 2
  "$PY" patrol.py --root "$PROJECT" --dry-run
)
DRY_RUN_STATUS=$?
test -e "$PROJECT/STOP" && test -e "$STOP_PATH" || exit 1
test "$DRY_RUN_STATUS" -eq 0 || exit 1
```

対象が自分で確認した停止マーカーであることが前提です。他のファイルを消すコマンドとして使いません。`status=ok`、本人確認、読取、保留内容を確認し、実書込みなし・本番state不変を照合。`stopped`や`locked`、認証不一致、結果不明は合格ではありません。異常時はSTOP維持で復旧判断へ。

練習で作った依頼を本番未着手のまま残さず、既存の練習後処理手順に従って本人がNotionで確認します。練習stateを本番へコピーしません。

## 5. 本体のみ再開する（Noa OFF）

更新先commit・試験・本番state・設定・保留の確認が済み、本人の再開OKがある場合だけ再開します。

1. 既存plistを再確認。新しいテンプレートへの置換や二重登録はしない。
2. 両STOPを外す。まだlaunchdが未登録であることを確認。
3. `launchctl bootstrap "gui/$(id -u)" "$PLIST"` で同じジョブを戻す。
4. 既存plistのRunAtLoadがtrueなら、その時点で即時実行される。追加の手動巡回やkickstartは重ねない。
5. 初回と次の定期回を確認。live・LLM OFF・Noa OFF、last_run_atの更新、予定外の再送なし、本人確認・保留・ログ追記を確認する。3時間以上の観測にはログ同期の実行結果も含める。

観測時間が過ぎただけでは合格にしません。本番で動いた具体的な結果を本人が確認し、異常なら直ちに停止します。今回の開発作業ではこの再開・観測を実施していません。

## 6. v0.8へ戻す場合

### 基本は「コードだけ戻し、最新state/logsを保持」

v0.8とv1.0の状態形式・読取検証は変更していません。[互換性の検証記録](state-compatibility.json)では元のv0.8 readerと現行readerを使い、両方向・本番/練習の4組合せの読み書き、6回の交互保存、22通りの不正入力拒否を確認しています。ただし、**このMacの実stateの健全性や、外部送信の成否を確認した証明ではありません。**

1. 両STOP → 自然終了 → 対象launchd解除 → プロセス・ロックなしを再確認。
2. **失敗時点の最新state/logs/.envとcommitを、新しい別名バックアップへ保存**。更新前バックアップを上書きしない。
3. 追跡ファイルがcleanで、保存済み復旧commitが確定していることを確認。未保存変更があれば止める。
4. `git switch --detach 5399e1a580de27bddcdd34cade1b63ad392f8dd1` でコードだけを戻す。実.env・state・logs・plistは保持する。
5. `git rev-parse HEAD`、版番号0.8.0、v0.8のオフライン402試験、STOP中の本番・練習`--status`で**最新state**が読めることを確認。
6. 本人が実接続確認を承認した場合だけ、上記と同じ制御下でNoa OFFの単発dry-runを実施。STOPを戻して結果確認。
7. 本人の再開OKで、本体だけ再開。Noa OFF・LLM OFFを維持する。

detached HEADでは通常のpullを続けません。再び更新する場合は、レビュー済みブランチとcommitを確認して別途手順を決めます。

### 更新前stateを安易に復元しない

v1.0が1回でも動いた後に更新前stateへ戻すと、既に送った指示・通知、Noa起動、429試行、既読報告、ログの記録を忘れ、**二重実行**につながります。外部書込み前でも、途中のローカル記録を失うことがあります。

したがって、互換な最新stateを保持するコードロールバックが原則です。破損等で旧バックアップを検討する場合も、送信・試行・保留・ログの差分を本人が履歴と照合するまでSTOPを維持。自動復元、空state作成、キー除去、ログの削除、held解除による回避はしません。内容を確定できなければ、うさへ確認して停止を続けます。

v0.7は復旧対象ではありません。v0.8以降の状態をv0.7が読めるという保証はしていません。

## NoaをONにする前の別段階

[Noaの有効化・即時OFF手順](noa-activation-v1.md)へ。本体更新・再開の承認だけでは、NoaをONにしたりテスト投稿したりしません。

## v1.0 → v1.0.1 の追加手順

2026-11-12 14:05 JSTの本人からの引継ぎは、v1.0 `35e00f8dae9ddf744705c6e11012dd70f1407baa` が本番で稼働中という内容です。開発側の実機確認ではありません。上のv0.8→v1.0は履歴として保持し、今回の更新対象・復旧先を取り違えません。X-01のSTOP手順・Noa ON手順は今回変更していません。

1. 本人の配置OKと、うさのv1.0.1レビューを確認。停止・自然終了・launchd解除・最新state/logsと設定の保全は上記の原則を守り、対象実機の値を確認してから実施します。
2. v1.0.1のレビュー済み完全commitへfast-forward更新し、`"$PY" scripts/test_offline.py` の484件成功を確認。実.envを見本で上書きしません。
3. **たかむねが実.envへ `ICE_MENTION_USER_ID` を追加**します。値はチャット・Gitに出しません。Uから始まる英大文字・数字のIDのみ。たかむね本人・りり自身は不可。未設定・不正なら認証前に停止します。指示先と報告先は別の既存チャンネルを維持。
4. `NOA_WAKE_ENABLED=false`、`LLM_ENABLED=false` を維持。stateスキーマは不変、必須キー追加なし。既存outboxは本文・宛先・rp印をそのまま保持し、新規投稿だけ新形式にします。既存の投稿済み指示の一斉複製は行いません。
5. `--status` はICE_MENTION_USER_IDの本人確認を行わず、その不備は分かりません。設定確認は、本人が実接続確認を許可した上で `"$PY" patrol.py --dry-run` の `status=ok` を確認します。STOPでのstoppedやheld/errorを合格にしません。dry-runは実APIの読取と練習state/logsの更新を伴うため、今回の開発試験では実施しません。本番再開・正式メンションの受信試験も本人が実施範囲を決めます。botのメンションからdotが起動することは保証していません。結果不明なら二重投稿しません。
6. 戻す前に、報告先あてのqueued / sendingが無いことを確認します。あればSTOPを維持し、実際の送信結果を本人が確認してから判断します。v1.0で正式メンション付きqueued文面が安全検査によりheld/unsafe_contentになると、解除選択肢はcancelのみです。sendingは送信結果不明の可能性があるため、未送信と決めつけてcancel/再送しません。戻す場合のコードは **v1.0 `35e00f8dae9ddf744705c6e11012dd70f1407baa`**。最新state/logsを保ち、古いバックアップへ巻き戻しません。新設定の値も秘密のまま保持し、v1.0は未使用設定として無視します。

### 復旧で保証できる範囲

v1.0とv1.0.1で、状態reader/スキーマはバイト単位で同一です。双方向の読書きで既存の送信結果・重複防止・Noa回数・新しい複製noticeを保持できることを模擬試験しました。ただし、**v1.0は正式メンションを安全検査で拒否するため、v1.0.1で新規に作った正式メンション付き未送信文面は、コードを戻すと保留になり得ます。** 形式互換は、旧コードで新文面を送れる保証ではありません。

STOPを維持し、未送信・sending・heldの実状とSlack履歴を本人が確認してから復旧・再開を決めてください。メンションやrpを削って旧コードに通したり、state/outboxを消したりしません。新形式の未送信分がある場合は、修正版で安全に続ける選択も本人が検討します。実Macの状態・実送信結果・復旧成功は今回未検証です。


## v1.0.1 → v1.0.2 の追加手順

対象は v1.0.1 `f577055de586123008c46cd731e0578ef2c43a2e`。今回のレビュー済みv1.0.2完全commitを確定してから、既存のSTOP・自然終了・保全・fast-forward更新手順を守ります。状態スキーマ・Noa・.env項目は追加変更しません。`NOA_WAKE_ENABLED=false` と `LLM_ENABLED=false` を維持します。

- 試験コマンドは確認済みの `"$PY" scripts/test_offline.py`、530件成功。Mac標準python3が古い可能性があるため、PATH上のpython3へ置き換えません。
- --statusは保存状態の確認だけで、ICE_MENTION_USER_IDの妥当性確認ではありません。必要な実接続確認を本人が許可した場合だけ `"$PY" patrol.py --dry-run` のstatus=okで確かめます。実API/練習状態への影響とSTOP復帰手順は上記の通り。
- 既存の危険内容・結果不明・保留記録は削除も自動解除もしません。v1.0.1で案件ページ形式のため保留した報告は、本人が内容を確認して訂正版を作ると、新digestとして再検証します。安全なsnapshotはそのまま使え、危険なsnapshotは保留のままです。
- v1.0.1へのコード復旧も最新state/logsを保持。ただし旧版ではタイトルのみ・www URL・外部URLのY-01問題が再発し得ます。v1.0まで戻す場合はさらに正式メンションの制約があるため、前節の報告先queued/sending確認を省略しません。状態形式の互換は旧版の動作上の問題が消える保証ではありません。
- 配置・launchd再登録・実.env変更はたかむねが行います。今回実施したのはコード修正・オフライン試験・許可されたprivate GitHub保存までで、実機の稼働やdot起動を検証したものではありません。


## v1.0.2 → v1.0.3 の追加手順

基点は v1.0.2 `5d384fd764f763e2ec521ba0985bb6c93de1689e`。本人からの本番ログ障害報告を受けた修正です。開発側は実機・実APIを操作していません。今回の完全commitとうさのレビュー結果を確認し、配置・launchd・実.envの操作はたかむねが行います。

1. 上のSTOP・自然終了・保全・fast-forwardの原則を使用。最新state/logsと実設定を保持し、コードだけ更新します。NOA_WAKE_ENABLED=false・LLM_ENABLED=falseを維持。schema、既存outbox、Noa、設定項目は変えません。
2. 確認済みの `"$PY" scripts/test_offline.py` で 573 件成功を確認します。実APIへの確認を許可していない段階で通常patrol/dry-run/demoを実行しません。
3. 保留 `log_batch` は削除/手動解除せず保持します。承認後の次の巡回では表を全読取りして照合のみ。全IDがあればbatchを解消して時刻を更新し、その回は新規追記しません。一部/0件/読取失敗は保持したまま、通常の依頼・報告処理は継続し、status=heldで区別します。
4. **0件は未書込の証拠ではありません。** 反映遅延や不完全な読取を除外し、本人が実表・出来事印・batch対象を照合して未反映分を確定した場合だけ、従来の `"$PY" patrol.py --resolve log バッチID not_written` を使います。この手動操作は今回実施しません。再開後も既存IDを除き、不足分だけを追記します。全件確認済みの場合の `written` も従来どおりです。判定できなければ保留を維持します。
5. 新しい確認失敗ではエラーイベントを増やしません。古いエラーイベントは削除せず、保留解消後の通常同期で残します。`logs/patrol.log` の診断は形情報のみ。全件照合後でもSlack通知自体の送信不明など別の保留は残る場合があります。
6. 戻す場合のコードはv1.0.2。最新state/logsと既存の送信不明・出来事印を保持。読み書き形式は互換ですが、**旧版では応答形依存の障害とエラー連鎖が再発する可能性**があります。履歴を空にする、batchを削る、古いバックアップへ戻す、未確定を未送信と決めつける方法では復旧しません。本人がSTOP維持で履歴と保留を確認し、再開を判断します。

実機での動作確認・復旧成功は未検証です。--statusは保存状態の表示であり、API疎通や本人IDの確認ではありません。必要な実接続試験は前節と同じく別の許可の上で行います。

## v1.0.3 → v1.0.4 の追加手順

本版はログ通知2点と本人承認の限定代理投稿です。NoaのOFF、LLMのOFF、stateの形は変えません。実配置・実.env・launchd・アプリ権限は開発側で変更していません。
本人が既存のSTOP・実行終了確認・launchd停止・最新state/logsバックアップ手順を守り、レビュー済みコードへ更新して "$PY" scripts/test_offline.py の598件成功を確認します。未確認のコードで本番を再開しません。

### 任意の代理投稿を使う場合（本人操作のみ）
1. Slack公式のアプリ管理 https://api.slack.com/apps で対象アプリを選び、OAuth & Permissions の **User Token Scopes** に **chat:write** だけを指定します。Bot Token Scopesは今回変更しません。
2. ユーザー本人のアカウントでワークスペースへのインストール/再認可内容を読み、許可範囲を確認します。管理者承認が必要なら待ちます。既に別のUser権限があるアプリなら、それを勝手に削除せず専用アプリなどを本人が検討してください。
3. 発行されたUser OAuth Token（xoxp）を本人がローカル.envの SLACK_REPORT_USER_TOKEN に保存します。値をチャット、Git、スクリーンショット、コマンド引数に出しません。SLACK_BOT_TOKEN（xoxb）は従来のままです。
4. 本版は **C0CFL6A8QJHへのchat.postMessageだけ** にxoxpを使用します。本人確認auth.testだけは例外です。本人ID U0C5V11S37X・ボットと同じworkspace以外なら、報告先も指示先も何も投稿しません。宛先を別チャンネルに変えて使うことはできません。
5. --status は通信せず期待するユーザーIDとトークン設定有無を表示するだけです。実アカウントを検証した意味ではありません。本人が実API読み取りを許可したうえで --dry-run を行い、status と verified_slack_accounts の確認済みIDを見ます（ユーザーID以外の秘密は表示しません）。停止/未完走/認証失敗では確認済みと扱いません。既存のSTOP手順に従います。
6. 本番再開後は本人が報告先・投稿者・自動通知のラベル・先頭メンション・末尾rpを確認します。dotの即時起動は未検証であり、このコードだけでは保証しません。失敗時に自動再送で試さないでください。

**重要**：chat:writeはSlack側では他の送信/編集等にも使える権限です。「この1チャンネルのみ」は本プログラムでの制限で、トークン自体がSlack側で1チャンネル限定になるわけではありません。秘密を厳重に保管してください。実トークン作成・再認可・権限変更は本人の操作です。
送信には「りり自動通知・本人承認の代理投稿／本文は本人の直接指示ではありません」を表示します。りり・外部AI・Notionの本文をたかむねの新規直接指示とみなしてはいけません。受信側の承認や権限を拡張しません。
公式一次資料（2026-11-12確認）：https://docs.slack.dev/authentication/tokens/ 、https://docs.slack.dev/reference/scopes/chat.write/ 。

### 戻す場合
STOP・実行停止を確認後、本人が SLACK_REPORT_USER_TOKEN を空にする（または変数行を除く）と新しい報告投稿はkonokaririへ戻ります。最新state/logsは保持します。未確定の本人投稿もxoxbの履歴読み取りでrp照合し、見つからなくても自動再送しません。
アプリのトークン失効は別の本人操作です。環境変数を空にしてもSlack上の認可が取り消されるわけではありません。
v1.0.3コードへの切戻し前には、本人代理投稿のsending/heldを本版で照合しておく必要があります。旧版は本人投稿を発見できません。解消できないときは切戻し/再送をせず確認を待ちます。
state必須項目は変更なし。既存queue本文は書き換えず、送信時だけ代理投稿ラベルを付けます。ログ同期通知の新エピソードを旧版は判定できないため、切戻し中は本人が同期状況を別途確認してください。古いstateへの巻戻しは二重送信の原因になります。
