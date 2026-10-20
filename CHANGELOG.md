# 変更の記録

## v0.1.0（2026-10-20）
最初の公開版です。Grok Bot のテンプレート「Desk for LINE」が使うスクリプト・スキル・ルーティンをまとめました。

- スクリプト 14 本（Python 3 の標準ライブラリだけ）：init_db、log_event、list_unreplied、set_status、mark_read、quota_check、stats、purge_old、profile_cache、canned、save_media、broadcast_prep、keyword_reply、account。
- 記録はボットごとの保存先（`DESK_DB` が必須。例 `/workspace/line-inbox/<agent id>/messages.db`）。`DESK_DB` がないとスクリプトは止まります。
- Webhook の `destination` を保存し、最初に届いたものをこのボットのアカウントとして覚えます。違う宛先は記録だけにして知らせます（`account.py`）。
- 届いたもののお知らせと表示名、未返信の一覧と要約、返信案と送信の記録、送信エラーの記録、定型文、画像・動画・ファイルの保存、キーワード応答（自動送信は初期オフ）、ボットからの一斉配信の確認、残り通数、週報の数字。
- スキル 8 つ：desk-getting-started、desk-reply、desk-announce-draft、desk-keyword-reply、desk-inquiry-summary、desk-away-mode、desk-weekly-report、desk-update。
- ルーティン：line-webhook（LINE受信の記録）。
- スクリプトは送信しません。送るのはボットが line-oa の道具で、社長のOKのあとだけです（例外は社長がオンにしたキーワード応答の自動送信）。
