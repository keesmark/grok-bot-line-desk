# grok-bot-line-desk

[English](README.en.md) | 日本語

Grok Bot のテンプレート「Desk for LINE」で使うスクリプト・スキル・ルーティン・ひな形です。LINE公式アカウントに届いたお客様や取引先からのメッセージを、ボットが記録してお知らせし、返信案を作ります。お客様に送るのは、ボットを使う人（担当者）が確認した後だけです。

テンプレートにはスクリプトを入れられないため、ここに置いてバージョンごとに配ります。LINE の Webhook をボットに届ける中継は [line-webhook-relay](https://github.com/keesmark/line-webhook-relay) です。

> 個人が作った非公式のテンプレートです。xAI（現 SpaceXAI）や LINEヤフー株式会社とは関係がなく、提携や推奨も受けていません。Grok は SpaceXAI の商標です。LINE は LINEヤフー株式会社の登録商標または商標です。

## 中身
- `scripts/`：Python 3 の標準ライブラリだけのスクリプト 14 本。送信はしません。
- `skills/`：テンプレートと同じスキル 8 つ。
- `routines/line-webhook.md`：受信のルーティン「LINE受信の記録」のプロンプト。
- `template/`：テンプレートのメモリ（`memory.md`）とプロフィール（`profile.md`）のひな形。desk-update スキルが新旧を比べ、メモリの変更点を示します。
- `VERSION`：バージョン。タグは `v` と VERSION をつなげたものです（`0.1.0` → `v0.1.0`）。
- `CHANGELOG.md`：バージョンごとの変更。
- `MIGRATIONS.md`：バージョンを上げるときに手で行う作業。desk-update スキルが読みます。
- `SHA256SUMS`：ボットに入るファイル（scripts・skills・routines・template・VERSION・CHANGELOG・MIGRATIONS）の照合値。`SHA256SUMS.asc` はその署名です。
- `keys/release.asc`：署名の公開鍵（鍵の番号 `F136 EFC6 1A9D 8292 7C32  43EE 0FAD 4225 1453 DA94`）。desk-update スキルの中にも同じ鍵があります。
- `tools/`、`tests/`：このリポジトリを直す人のためのもの。ボットには入れません。

## 入れ方と確かめ方

`SHA256SUMS` は作者（keesmark）の GPG の鍵で署名してあり、ボットは署名を確かめてから入れます。確かめに使う公開鍵と鍵の番号は、テンプレートの desk-update スキルの中にあります。このリポジトリが乗っ取られても、作者の秘密鍵がなければ署名は作れないので、偽のバージョンは入りません。

```mermaid
flowchart LR
  K[テンプレートの desk-update スキル<br>公開鍵と鍵の番号] --> B[ボット]
  R[GitHub のタグ<br>v + VERSION] -->|tar.gz| B
  B -->|鍵の番号が同じ・署名が正しい<br>scripts が SHA256SUMS と同じ| W["/workspace/line-inbox/bin/"]
  B -.->|どれかが違う| S[止めて担当者に伝える]
```

ふつうはテンプレートの desk-getting-started スキルが、ボットに次のことをさせます。手で入れるときは、公開鍵をこのリポジトリではなくテンプレートの desk-update スキルから取るか、鍵の番号が `F136 EFC6 1A9D 8292 7C32  43EE 0FAD 4225 1453 DA94` であることを確かめてください。

```sh
tmp=$(mktemp -d) && cd "$tmp"
curl -fsSL -o a.tgz https://github.com/keesmark/grok-bot-line-desk/archive/refs/tags/v0.1.1.tar.gz
tar -xzf a.tgz --strip-components=1
g=$(mktemp -d)
GNUPGHOME="$g" gpg --batch --quiet --import keys/release.asc
GNUPGHOME="$g" gpg --batch --with-colons --list-keys | awk -F: '/^fpr/{print $10; exit}'   # F136EFC61A9D82927C3243EE0FAD42251453DA94 でなければ止める
GNUPGHOME="$g" gpg --batch --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS 2>/dev/null | grep -cE '^\[GNUPG:\] (GOODSIG |VALIDSIG F136EFC61A9D82927C3243EE0FAD42251453DA94 )'   # 2 でなければ止める
sha256sum -c SHA256SUMS   # 全部 OK でなければ止める
diff <(find scripts skills routines template -type f | LC_ALL=C sort) <(awk '{print $2}' SHA256SUMS | grep -E '^(scripts|skills|routines|template)/' | LC_ALL=C sort)   # 同じでなければ止める（署名にないファイルが混ざっている）
find . ! -type f ! -type d   # 何か出たら止める（リンクが混ざっている）
mkdir -p /workspace/line-inbox/bin
sed -n 's#^[0-9a-f]\{64\}  scripts/\([a-z_]*\.py\)$#\1#p' SHA256SUMS | while read -r f; do cp "scripts/$f" /workspace/line-inbox/bin/; done
cp VERSION SHA256SUMS SHA256SUMS.asc /workspace/line-inbox/bin/
chmod 700 /workspace/line-inbox/bin/*.py
rm -rf "$g"
```

`/workspace/line-inbox/bin/` は同じコンピューターのボットで共有です。すでに別のバージョンが入っていたら、上書きする前に確かめてください。スクリプトはいつも `DESK_DB=<このボットの保存先> python3 -I /workspace/line-inbox/bin/<名前>` で動かします。

## バージョンの確認と更新
- 入っているバージョン：`cat /workspace/line-inbox/bin/VERSION`。中身が変わっていないか：`cd /workspace/line-inbox/bin && { sed -n 's#  scripts/#  #p' SHA256SUMS; grep '  VERSION$' SHA256SUMS; } | sha256sum -c`。
- 新しいバージョン：最新の [Release](https://github.com/keesmark/grok-bot-line-desk/releases/latest)。main の `VERSION` は Release の前に上がることがあるので見ません。ボットに「更新はある？」と聞くと、desk-update スキルが確かめます。
- 更新は自動では入りません。desk-update スキルが変更点を見せ、担当者が「更新して」と言ったときだけ入れ替えます。

## 直すとき（このリポジトリを直す人へ）

テストは Python 3 の標準ライブラリの unittest で、カバレッジは [coverage](https://pypi.org/project/coverage/) で測ります。

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/coverage run -m unittest discover -s tests
.venv/bin/coverage report
```

スクリプトやスキルを直したら、`python3 tools/sync.py` で `SHA256SUMS`、スキル・ルーティン・ひな形の「バージョン：」の行、README とスキルのダウンロード URL のタグを、`VERSION` にそろえます。そろっていないと CI（`tools/sync.py --check`）が落ちます。`SHA256SUMS` が変わったら署名もし直す必要があり、署名が合っていないと CI（`tools/release.py --verify`）が落ちます。

バージョンを出す手順：

1. `VERSION` を上げ、`CHANGELOG.md` と `MIGRATIONS.md` に `## v<バージョン>` の節を足す（手作業がなければ「なし」）。
2. 作者の PC で、そのブランチを取り込んでから `python3 tools/release.py` を実行する。そろえ直し、署名、`SHA256SUMS`・`SHA256SUMS.asc` のコミットとプッシュをまとめて行います（gpg がパスフレーズを聞きます。ほかのファイルに未コミットの変更があると止まります。プッシュしないときは `--no-push`）。PR にしてマージする。
3. マージしたコミットにタグ `v<バージョン>` を付け、Release を作る。
4. テンプレートのメモリとプロフィールを `template/` の内容に合わせ、テンプレートを出し直す（すでに使っている人は desk-update スキルで更新するので、出し直さなくても動きます）。

## 参考にしたもの

L Harness（MIT ライセンス）と、Lステップ・エルメ・プロラインフリー・Liny の公開されている機能紹介ページから、考え方だけを参考にしました。コードはコピーしていません。

## ライセンス

[MIT](LICENSE)
