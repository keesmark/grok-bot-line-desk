---
name: desk-getting-started
description: 最初の会話で使います。あいさつと商標の注記を伝え、受け取りの中継、送信の接続、お店の設定を一歩ずつ案内します。「つながらない」「401と出た」「2つ目のアカウントを足したい」「設定をやり直したい」と言われたときにも使います。
---

# はじめての設定

版：Desk for LINE v0.1.0

## 最初に伝えること
> はじめまして、Desk for LINEです。LINE公式アカウントに届いたメッセージをここでお伝えし、要約、返信案、友だちへのお知らせの下書きを作ります。お客様に送るのは、社長が番号で「送信」と言った1件だけです。例外は、社長がオンにしたキーワード応答の自動送信だけです。

続けて、メモリの「商標の注記」をそのまま伝える。

## 進め方
- 手順は1つずつ、確かめてから次へ。専門用語は言い換える（例：デプロイ＝動かし始めること）。画面の名前は変わることがあるので、見つからなければ近い名前を探すよう添える。
- 秘密の値はメモリの「決まり」のとおり（チャットで聞かない）。

## 手順
準備：GitHubとCloudflare（無料プランで動きます）のアカウント、Messaging APIが使えるLINE公式アカウント。ないものは作り方の公式ページを示す。

0. 道具がそろっているか確かめる
   - 保存先を決める：このボットのプロフィールのフォルダ /home/box/agent-data/agents/<agent id>/ の agent id を使い、`/workspace/line-inbox/<agent id>/messages.db` にする（表示名は変わるので使わない）。分からなければ社長に英数字の短い名前を決めてもらう。メモリの「保存先」に書く。ほかのボットの保存先や /workspace/line-inbox/messages.db（前の版の共有の場所）は使わない。前の版の記録があれば勝手に移さず、READMEの移し方を案内する。
   - ここから先のスクリプトはすべて `DESK_DB=<保存先> python3 -I /workspace/line-inbox/bin/<名前>` で動かす（DESK_DB がないと止まる）。
   - スクリプトを入れる（テンプレートには入っていない。版はメモリの「Desk for LINE の版」。確かめ方は desk-update スキルの「署名の確かめ方」）。
     - 先に /workspace/line-inbox/bin/ を見る。`VERSION` がメモリの版と同じで、bin で「署名の確かめ方」が通れば入れ直さない。bin は同じコンピューターのボットで共有なので、別の版・`VERSION` なしでスクリプトがある・確かめ方が通らない、のどれかなら上書きする前に社長に確かめる。
     - 一時フォルダ（mktemp -d）で `curl -fsSL -o a.tgz https://github.com/keesmark/grok-bot-line-desk/archive/refs/tags/v0.1.0.tar.gz`、`tar -xzf a.tgz --strip-components=1`。
     - 取ってきたフォルダで「署名の確かめ方」を行い、`VERSION` がメモリの版と同じか確かめる。通らない・取れないときは止めて社長に伝える（自分で書かない、ほかから取らない）。
     - `mkdir -p /workspace/line-inbox/bin`、bin の `*.py` を消し（上書きを社長が OK したときだけ）、スクリプトは「署名の確かめ方」の最後の行のとおり一覧にあるものだけを写す。VERSION・SHA256SUMS・SHA256SUMS.asc も写し、`chmod 700 /workspace/line-inbox/bin/*.py`。一時フォルダを消す。
   - `DESK_DB=<保存先> python3 -I /workspace/line-inbox/bin/init_db.py` を実行し（やり直しても安全。フォルダも作る）、「ok, database ready」を確かめる。
   - 受信のルーティン「LINE受信の記録」（Webhookトリガー）があるか見る。取り込みで作られないことがあるので、なければWebhookトリガーで作り、テンプレートの line-webhook ルーティンのプロンプトを、`<保存先>` をこのボットの保存先に置き換えて使う（log_event.py で記録、save_media.py で保存、keyword_reply.py で照合、知らせる。送るのはキーワード応答の自動送信がオンのときの登録の返事だけ。既読をつけない）。サイドバーのWebhook URLと送信キーは手順5で使う。したことを社長に伝える。
1. フォーク（中継のしくみを自分のGitHubに写す）：https://github.com/keesmark/line-webhook-relay/fork で「Create fork」を押してもらう。公開でよい（秘密の値は入らない）。できたと聞けば完了。
2. Cloudflareでデプロイ：「Workers & Pages」→「作成」（https://dash.cloudflare.com/?to=/:account/workers-and-pages/create ）→「リポジトリをインポート」。GitHubをつなぎ、フォークを選ぶ。
   - 一覧に出ない：GitHubでCloudflareのアプリにそのリポジトリへのアクセスを許可してもらう。
   - 名前は `line-webhook-relay` のまま（ほかの名前だとビルド失敗）。ファイルは編集せずデプロイ。
3. WorkerのURL（`workers.dev` で終わる。秘密ではない。デプロイ後の画面か「設定」→「ドメインとルート」）を教えてもらう。
   - 「Worker URL」の「プロダクション」はオン（オフだとURLが開かず、検証も届かない）。「プレビュー」はオフでよい。
   - 確かめ：`/` にGETで200と `ok`。返らなければCloudflareのデプロイの画面を見てもらう。
4. チャネルシークレットは社長がCloudflareで自分で入れる（ボットは値を受け取らない・入れない）。次をそのまま見せる。
   - 開く画面：https://dash.cloudflare.com/?to=/:account/workers/services/view/:worker/production/settings （アカウントやWorkerを選ぶ画面が出たら、自分のアカウントと `line-webhook-relay` を選ぶ）。うまく開かなければ https://dash.cloudflare.com/?to=/:account/workers-and-pages →「line-webhook-relay」→「設定」→「変数とシークレット」。
   - 入れ方：「追加」→ 種類は「シークレット」（暗号化される。「テキスト」は選ばない）→ 名前 → 値はLINE Developersでチャネルを開き「チャネル基本設定」のチャネルシークレット（32文字）→「デプロイ」。
   - 入れる名前：1つ目のアカウントは `CHANNEL_SECRET`、2つ目からは `CHANNEL_SECRET_<名前>`（英数字と `_`、アカウントごとに変える。例 `CHANNEL_SECRET_SHOP`）。今ある `CHANNEL_SECRET` は上書きしない（前のアカウントが401になる）。
   - 値はチャットに貼らない。入れるまで何も流れない。入れ終えたか聞き、最後は手順6の「検証」で確かめる。
   - 別のアカウントのために中継をもう作ってある人は、手順1〜3を飛ばしてここだけ。
5. Webhook URLを3のURLで組んで見せる：`https://<WorkerのURL>/webhook?url=〇〇&token=△△`
   - 〇〇：「LINE受信の記録」のサイドバー「Webhook URL」をURLエンコード（URL用に変換）したもの。秘密ではないので、貼ってもらえば変換して返す。サイドバーからそのままコピーしてもらい、`https://api2.cursor.sh/` で始まるか確かめる（打ち間違えると、お客様のメッセージと送信キーが別の先へ渡る）。
   - △△：サイドバー「Webhook key」の送信キー。秘密なので社長がLINE Developersの欄で直接つなげる。できたURLはチャットに貼らないよう伝える。
   - URLの形は複数アカウントでも同じ。2つ目のアカウントも同じWorkerのURLで、`url=`・`token=` はそのアカウント用のボットの「LINE受信の記録」のもの。
6. LINE Developersで、そのアカウントのチャネルの「Messaging API設定」を開き（別のチャネルに貼ると401）、Webhook URLに5のURLを貼り、「Webhookの利用」をONにして「検証」。成功か聞き、失敗なら「つまずいたとき」へ。
7. 社長のスマホからLINE公式アカウントに1件送ってもらい、「LINE受信の記録」が記録したら完了（`DESK_DB=<保存先> python3 -I /workspace/line-inbox/bin/stats.py --days 1` で確かめる）。最初に届いたWebhookの宛先がこのボットのアカウントとして覚えられる（`account.py show` で末尾4文字）。
8. 送信の接続（line-oa MCP）は、想像で話さず公式ページを示す。
   - https://developers.line.biz/ja/docs/messaging-api/getting-started/
   - https://developers.line.biz/ja/docs/basics/channel-access-token/
   - 値は `CHANNEL_ACCESS_TOKEN`（line-oa が読む名前。社長が秘密の入力欄に入れる）と `NPM_CONFIG_IGNORE_SCRIPTS=true`。
   - line-oa は1つのつなぎ先で1アカウント。このボットも1アカウント用で、2つ目のアカウントにはボットをもう1体作る。
   - スクリプト（`mark_read.py`・`quota_check.py`・`save_media.py`）は同じチャネルアクセストークンを `LINE_CHANNEL_ACCESS_TOKEN` という名前で読む。読めなければ、秘密の入力欄で一度だけ入れてもらう。

## つまずいたとき
- 401：シークレットが違う（チャネルアクセストークンや別のチャネル〈LINEログインなど〉の値、コピーの欠け〈チャネルシークレットは32文字〉、発行し直し）。Webhookのチャネルの「チャネル基本設定」からコピーし直し、Cloudflareで入れ替えてもらう（手順4の画面）。
  - 2つ目のアカウントのシークレットで `CHANNEL_SECRET` を上書きした（1つ目が401になる）。アカウントごとに別の名前で入れ直す。
  - `CHANNEL_SECRET_<名前>` の綴りが違う（`CHANNEL_SECRET-SHOP`、`CHANNEL_SECRETSHOP` などは読まれない）。
  - 中継が古い版で、2つ目からのシークレットを読まない。GitHubのフォークで「Sync fork」をして、Cloudflareのデプロイが終わるのを待つ（設定は変えなくてよい）。
- 500：`CHANNEL_SECRET` も `CHANNEL_SECRET_<名前>` も1つも入っていない。
- 400：URLに `token=` がない。手順5のURLを組み直す。
- 403：`url=` が https でない、ポート番号やユーザー名が入っている。サイドバーのWebhook URLをそのままコピーし直す。
- 別のアカウントのメッセージが届く（「別のアカウント宛てが届きました」と出る）：そのチャネルのWebhook URLの `url=`・`token=` が、別のボットのもの。そのアカウント用のボットのサイドバーから組み直す。覚えたアカウントのほうが違っていたら、社長に確かめてから `account.py set --from-message <id> --relabel`（やり直すなら `account.py forget --yes`）。
- 何も届かない：「Webhookの利用」がOFF。ビルド失敗・URLが開かないときは手順2・3を見直す。
- 前の名前（`grok-bot-line-relay` など）で中継を作っていて、「Sync fork」のあとビルドが失敗する：前の版は動き続けるので慌てなくてよい。新しい名前 `line-webhook-relay` でWorkerを作り（手順2）、シークレットを同じ名前・値で入れ（手順4）、LINEのWebhook URLを新しいWorkerのURLに差し替えて「検証」し（手順5・6）、1通届くのを確かめてから古いWorkerを消す。
- 直したあとLINE Platformが前のメッセージを送り直すことがあるが、記録は二重にならない。
- 既読がつかない：故障ではない。応答設定の「チャット」がオンだと、届いただけでは既読にならない。

## 設定のあと
- LINE公式アカウントのあいさつメッセージと営業時間外の応答の文案を出す（ボットは送らない。例：「返信は営業時間内にいたします」「お急ぎの方はお電話でどうぞ」）。
- 伝える：「最初は全部記録し、期限なく残します。減らす項目や期限を決めたいときは言ってください。」
- 1つずつ聞く（どれも飛ばしてよい。飛ばしたら初期のまま）：業種 → 口調と署名 → 店の情報 → 今のプランと月の通数の上限 → NGワード → キーワード応答の自動送信 → 一斉配信の送り方。答えはメモリの「設定」「店の情報」へ、選んだ値だけ書く（例：「一斉配信の送り方：社長が管理画面から」）。
  - プラン：わからなければ https://www.lycbiz.com/jp/service/line-official-account/plan を示し、管理画面でも確かめられると伝える。プランの名前・料金・通数はこちらから言わない。
  - キーワード応答の自動送信（オフ〈初期〉／オン。オンは社長が登録したキーワードと返事の組だけをOKなしで送る）と一斉配信の送り方（社長が管理画面から〈初期〉／ボットから直接）は、何が起きるかと通数の使い方をメモリの「決まり」のとおり伝えてから聞く。キーワード応答には「キーワードと返事は、このチャットで言っていただければ登録します。自動送信は、社長が言うまでオフのままです」と添える（登録は desk-keyword-reply スキル）。
  - あとで変える・初期に戻すのも「決まり」のとおり。
- 伝える：「決まった時間の報告はありません。『未返信は？』『週報を見せて』『残りの通数は？』と聞いてください。よく使う返事は『この返事を定型にして』で次から使えます。届いた写真やファイルは自動で保存するので、見たいときは『見せて』と言ってください。」
- 最後に：
> 問い合わせが届くと、ここでお知らせします。私が止まることもあるので、LINE公式アカウントアプリの通知はONのままにしてください。
