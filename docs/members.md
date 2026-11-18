# 名簿（config/members.json）

ヒロインズの正式な名簿です。名前・ID・別名・色・役割・完了報告ページを1か所に持ちます。

- 依頼IDのハートは「依頼した子」の色です。**1人1色で、ほかの子と同じ色は使いません**（例：みゅうは🩷、🩵はみに）。
- 色と役割の出どころは Notion の役割表です。みゅうは役割表にまだないため、本人の指示で🩷にしています。
- 変更するときは PR で `config/members.json` を直し、CI が通ることを確かめます。

## 何が確かめられるか

`tests/test_members.py`（CIで毎回）

- ID・名前・別名が重複していない、色が2人に割り当てられていない、AIの子に色がある
- りりの依頼IDの色の一覧（`riri/parse.py`）、宛先の一覧と完了報告ページ（`riri/models.py`）、
  依頼した子の別名、local-tasks の色が名簿と一致している

`scripts/check_members.py`（Macで手で実行）

```
python3 scripts/check_members.py --myu /path/to/myu-ai
```

みき（registry.json）・まむ（mamu_guard.py、mamu_relay.py）・みゅう（riri_link.py、slack_reader.py）の
名簿や色の一覧を読み、名簿と食い違うところ（不一致）と、まだ知らない子（未登録）を出します。読むだけで、何も変えません。
みゅうは別のMacにあるので、`--myu` でその場所を指定したときだけ確かめます。

いまは、ほかのプログラムはまだこの名簿を読んでいません。共通部品にまとめる段階で、各プログラムがこの名簿を直接読むようにします。
