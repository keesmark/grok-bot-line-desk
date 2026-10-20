# Desk for LINE

Grok Bot のテンプレート「Desk for LINE」で使うスクリプト・スキル・ルーティンです。LINE公式アカウントに届いた問い合わせを、ボットが記録し、知らせ、返信案を作ります。お客様に送るのは社長のOKのあとだけです。

テンプレートにはスクリプトを入れられないため、ここに置いて版ごとに配ります。

## 中身
- `scripts/`：Python 3 の標準ライブラリだけのスクリプト 14 本。送信はしません。
- `skills/`：テンプレートと同じスキル 8 つ。
- `routines/line-webhook.md`：受信のルーティン「LINE受信の記録」のプロンプト。
- `VERSION`：版（例 `0.1.0` → タグ `v0.1`）。
- `CHANGELOG.md`：版ごとの変更。
- `SHA256SUMS`：`scripts/` の照合値。

## 入れ方
ふつうはテンプレートの getting-started スキルが、ボットに次のことをさせます。手で入れるときも同じです。

```sh
tmp=$(mktemp -d) && cd "$tmp"
curl -fsSL -o a.tgz https://github.com/keesmark/desk-for-line/archive/refs/tags/v0.1.tar.gz
tar -xzf a.tgz --strip-components=1
(cd scripts && sha256sum -c ../SHA256SUMS)   # 全部 OK でなければ止める
mkdir -p /workspace/line-inbox/bin
cp scripts/*.py VERSION SHA256SUMS /workspace/line-inbox/bin/
chmod 700 /workspace/line-inbox/bin/*.py
```

`/workspace/line-inbox/bin/` は同じコンピューターのボットで共有です。すでに別の版が入っていたら、上書きする前に確かめてください。スクリプトはいつも `DESK_DB=<このボットの保存先> python3 /workspace/line-inbox/bin/<名前>` で動かします。

## 版の確かめ方
- 入っている版：`cat /workspace/line-inbox/bin/VERSION`。中身が変わっていないか：`cd /workspace/line-inbox/bin && sha256sum -c SHA256SUMS`。
- 新しい版：`https://raw.githubusercontent.com/keesmark/desk-for-line/main/VERSION` と `CHANGELOG.md`。ボットに「更新はある？」と聞くと、update スキルが確かめます。
- テンプレートのメモリには、入れる版と `SHA256SUMS` 自体の照合値が書いてあります。

## 表記
LINEはLINEヤフー株式会社の商標または登録商標です。Desk for LINEはLINEヤフー株式会社の公式製品ではなく、同社が提供・承認するものではありません。
