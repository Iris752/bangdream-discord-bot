# バンドリ公式情報 Discord BOT (v0.2 試験版)

## 対象
BanG Dream! 関連の全バンド（Poppin'Party、Afterglow、Pastel＊Palettes、Roselia、ハロー、ハッピーワールド！、Morfonica、RAISE A SUILEN、MyGO!!!!!、Ave Mujica、夢限大みゅーたいぷ、millsage、一家Dumb Rock!）と、プロジェクト全体の公式発表。声優個人の独立した活動は対象外。

**重要:** 12バンドを対象にした設計ですが、12バンドすべての個別情報源が検証済みという意味ではありません。初期登録8系統のうち5系統を有効にしています。各サイトのHTML仕様は実運用で検証が必要です。公式X限定・ゲーム内限定情報はこのBOTでは取得できません。

## 使い方
1. GitHubで **新しい別リポジトリ** `bangdream-discord-bot` を作成。公開リポジトリ推奨。既存の `anime-discord-bot` は変更しない。
2. ZIPを展開し、**中のファイルをリポジトリ直下**にアップロード。`.github/workflows/bangdream.yml` の階層を維持。
3. DiscordチャンネルのWebhook URLを作り、GitHub `Settings > Secrets and variables > Actions > New repository secret` に名前 `BANGDREAM_DISCORD_WEBHOOK_URL` で保存。URLはファイルやスクリーンショットに貼らない。
4. `Settings > Actions > General > Workflow permissions` で `Read and write permissions` を許可（state.json の保存用）。
5. `Actions > BanG Dream Official News > Run workflow` を実行。初回は過去記事を既読登録するため原則通知なし。ログで各サイトの `baseline created` を確認。
6. 以後30分ごとを目安に新着をチェック（GitHub Actionsの遅延あり）。

## 監視サイトを追加する
`sources.json` に `{ "name": "サイト名", "url": "https://example.com/news/", "article_pattern": "/news/[^/]+/?$", "enabled": true }` を追加。`article_pattern` はURLパスに対する正規表現。ニュース一覧からリンクが抽出できなければActionsは失敗するので、ログを見て調整する。`filter_related: true` は一般企業サイトなど、バンドリ無関係の記事が混在するサイト用。**初回有効化は過去記事を通知しない。**

## 通知仕様と制約
- Discord: タイトル・本文から抜粋した詳細要約・公式URL。要約は生成AIによる文章生成ではなく、記事内の重要部分を抽出する方式。長文は省略し、公式URLへ誘導。
- 初回の既読登録、記事URLの重複防止、既知記事の更新検知（直近10記事）、失敗時の次回再試行。
- 更新検知は初回既読登録した記事では実行しない。既存記事の全件再走査もしない。
- HTML構造の変更、JavaScript描画、サイト側制限、未登録ページなどによって取りこぼしが起こり得る。**全件取得の保証はない。**
- 公開サイトへの過度なアクセスを避ける。利用規約とrobots.txtを確認し、許可された範囲で利用する。
- Discord通知は全文転載ではない。サイトの本文の長い抜粋を投稿する場合も権利者の利用条件に従う。
- GitHub Actionsの無料利用条件・上限はリポジトリの公開/非公開やプランで異なる。

## トラブルシューティング
- `No article URLs discovered` : URL/記事パスのパターン不一致、サイト構造変更、JS描画など。sources.jsonの対象を一時的に無効化して原因を調査。
- `403/404` : サイトがアクセスを拒否、URLが変更された可能性。
- `Missing ... WEBHOOK` : Secrets名の入力ミス。
- `Permission denied` on git push : Actionsの書き込み権限を確認。
- 通知されない : 初回実行は既読登録のみ。2回目以降の新規記事が必要。

