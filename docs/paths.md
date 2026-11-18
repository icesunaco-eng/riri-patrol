# Macの場所の設定（riri/paths.py）

りりが使うMac固有の絶対パスは、すべて `riri/paths.py` にまとめています。
ほかのファイルに `/Users/...` や Codex の作業フォルダを直接書かないでください（`tests/test_paths.py` が検査します）。

## 決まり方

それぞれのパスは、次の順に決まります。

1. 環境変数（下の表）
2. 共有ファイル `~/.heroines/paths.json`（りり・みき・まむが同じファイルを読みます。`HEROINES_PATHS` で別の場所も指定できます）
3. 既定値（v1.4.3 までと同じ場所）

どちらも設定しなければ動作は変わりません。

## 共有ファイルの例

```json
{
  "_about": "このMacの場所。_ で始まる項目はメモとして無視されます",
  "riri_root": "/Users/<名前>/riri-patrol",
  "miki_root": "/Users/<名前>/miki-AI",
  "mamu_root": "/Users/<名前>/mamu-AI",
  "riri_workspace": "/Users/<名前>/heroines-work/riri"
}
```

値は絶対パスで書きます。書式が壊れていたり相対パスがあったりすると、推測せずに止まります。
みきは `riri_root`・`mamu_root` を、まむは `riri_root` を読みます。

## 変数と既定値

共有ファイルでの名前は、表の「共有ファイル」の列です。

| 環境変数 | 共有ファイル | 使う場所 | 既定値（ホーム以下） |
|---|---|---|---|
| `RIRI_ROOT` | `riri_root` | りり本体の配置先（local_tasks、mamu_bridge、install-local-tasks） | `riri-patrol` |
| `RIRI_MIKI_ROOT` | `miki_root` | みきの配置先 | `miki-AI` |
| `RIRI_MAMU_ROOT` | `mamu_root` | まむの配置先 | `mamu-AI` |
| `RIRI_MAMU_RELAY_POLICY` | `mamu_relay_policy` | まむの連携ポリシー | `RIRI_MAMU_ROOT` 以下の `data/relay-policy.json` |
| `RIRI_WORKSPACE` | `riri_workspace` | A改訂の固定証拠（local_revisions） | `Documents/Codex/2026-11-14/files-pasted-by-the-user-notion` |
| `RIRI_PROJECT_OPS_AREA` | `riri_project_ops_area` | phase56 の作業場所（project_ops） | `RIRI_WORKSPACE` 以下の `work/phase56-ops` |

値は絶対パスで書きます。相対パスは起動時に拒否します。空欄は未設定と同じです。
環境変数を使う場合、launchd で動かすときは plist の `EnvironmentVariables` に書きます。`.env` には書きません（`.env` はりりの接続設定だけを読みます）。ふつうは共有ファイルのほうが簡単です。

## Codexの作業フォルダを移すとき

`Documents/Codex/2026-11-14/...` は、2026-11-14 の Codex セッションで作った一時的な作業場所です。
りりの local_revisions と project_ops がここを読むので、**移す前に消さないでください**。

phase56 の manifest には、このフォルダの絶対パス（`work_root` など）が書かれ、ハッシュで固定されています。
そのため、進行中の phase56 の仕事があるうちはフォルダを動かせません。移す手順は次のとおりです。

1. phase56 と A改訂で進行中の仕事がないことを確かめる
2. 見回りを STOP で止める
3. フォルダを丸ごとコピーする（例：`~/heroines-work/riri`）。元はまだ消さない
4. 共有ファイルの `riri_workspace`（または plist の `RIRI_WORKSPACE`）を新しい場所にし、見回りを再開する
5. 数回の見回りでエラーがないことを確かめてから、元のフォルダを片付ける

新しく始める phase56 の仕事は、新しい場所の絶対パスで manifest を作ります。
