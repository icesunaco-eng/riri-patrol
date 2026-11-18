# りり見回りプログラム v1.4.3

りりが5分ごとに行う「見回り」を、Mac上の決まったプログラムで確実に回すためのツールです。
Notionの依頼を Slack で配り、完了報告を集めて、依頼の状態とログを更新します。
Python 3.11以上の標準ライブラリだけで動き、追加のインストールは要りません。

- 版ごとの変更点：[CHANGELOG.md](CHANGELOG.md)
- 運用の詳しい説明（保留の解除、STOP、Notionの同時編集、Noaなど）：[docs/operations.md](docs/operations.md)
- 要件：[docs/requirements-source.md](docs/requirements-source.md)
- 更新・復旧の手順：[docs/upgrade-v1.md](docs/upgrade-v1.md)

## 1回の見回りでやること

1. **安全確認**：STOPファイル、二重起動のロック、4分の時間制限、Slackボットが正しいアカウントかを確かめます。
2. **依頼を拾う**：Notion「りりへの依頼」の未着手の依頼を、古い順に処理します。
3. **指示を出す**：Slack「りりの指示部屋」に決まった型の文面で投稿します。
   あいす宛ての依頼は「たかむねりりあいす」にも送ります。
   投稿は1回につき指示が最大1件、通知なども合わせて最大4件です。
4. **完了報告を集める**：完了報告ページを読み、宛先全員の報告がそろったら依頼を「完了」にします。
   書き方が正しくない報告や元の依頼が変わった依頼は、その1件だけ止めて、ほかは進めます。
5. **ログを書く**：出来事をMacに保存し、3時間ごとにNotionのログ表へまとめて追記します。ログは自動で消しません。

## 安全のしくみ

- **重複投稿をしない**：投稿ごとに `rp:` という照合用の印を付けて記録します。
  投稿できたか分からないときは、再投稿せず「保留（held）」にして、人の確認を待ちます。
- **練習モードが既定**：`.env` に `RIRI_MODE=live` と書いたときだけ本番です。
- **AIに判断させない**：依頼内容・宛先・期限は固定の文面です。LLMは既定でオフです（`LLM_ENABLED=false`）。
- **状態ファイルを消さない**：消してやり直すと重複投稿の原因になります。

## よく使うコマンド

```bash
python3 patrol.py                 # 見回りを1回（既定は練習モード）
python3 patrol.py --dry-run       # 本番設定でも、この回は練習（書き込みなし）
python3 patrol.py --status        # 状態と保留の一覧（通信なし）
python3 patrol.py --flush-log     # 3時間を待たずにログを書き出す
python3 patrol.py --resolve instruction '依頼ID' posted   # 保留を手動で解除（通信なし）
```

`--resolve` で選べる種類と結果は、[docs/operations.md](docs/operations.md) の「Slack投稿が保留になったとき」を見てください。
本番の Mac では、`cd ~/riri-patrol` してから python.org 版 Python 3.14 のフルパスで実行します。

## 設定

`.env.example` を見本に、対象の Mac だけで `.env` を作ります。設定は `.env` からだけ読み、ターミナルの環境変数は使いません。
トークンなどの秘密の値は、README・チャット・GitHub に書かないでください。

## テスト

```bash
python3 scripts/test_offline.py   # 全テスト。通信を禁止して架空データで実行
python3 scripts/demo.py           # 架空データで5回の練習を実行
```

`test_bounded_projects` の6件は、Mac に入っている `miki` パッケージ（`/Users/sunako.jp/miki-AI`）を使います。
パッケージがない環境ではスキップされ、最後に理由つきで一覧表示されます。
**スキップは成功ではありません。** 「全件成功」と言えるのは、スキップが0件のときだけです。
別の場所にあるパッケージを使うときは `RIRI_TEST_MIKI_SOURCE=<フォルダ>` を指定します。

## ブランチ

`main` を最新の正式版にします。機能ごとの作業ブランチは、レビューと承認のあと `main` に取り込みます。

## 未検証の範囲

テストは架空データでの検証です。実際の Slack・Notion への投稿、Mac への配置、launchd の操作、Noa の有効化は、それぞれ本人の承認と実機での確認が別に必要です。
