# tldv議事録ボット(セルフサービス版)

自分が参加した会議の議事録を、tldvの文字起こしから自動生成して、**自分のChatworkマイチャット**に毎時届けるボットです。
セットアップは1回30分程度。以降は全自動で動きます(GitHub Actions・無料枠内)。

届く議事録の形式: ■本日の主な内容 / ■貴社対応事項 / ■弊社対応事項(A4 1枚程度、録画リンク付き)

---

## 事前に用意するもの(4つ)

1. **GitHubアカウント**(無料) — https://github.com/signup
2. **tldv APIキー** — https://tldv.io/app/settings/personal-settings/api-keys で「Create API Key」
   ※tldvのProプラン以上の席が必要です。画面が出ない場合は管理者に確認してください
3. **Chatwork APIトークン** — Chatwork右上の自分のアイコン →「サービス連携」→「API Token」→ パスワードを入れて表示
4. **OpenAI APIキー** — 会社から共有されたものを使ってください(管理者に確認)

## セットアップ手順

### 1. このテンプレートから自分のリポジトリを作る

1. このページ右上の緑色の「**Use this template**」→「Create a new repository」をクリック
2. Repository name: `tldv-minutes`(なんでも可)
3. **必ず「Private」を選択**(重要)
4. 「Create repository」をクリック

### 2. 秘密情報(Secrets)を登録する

作成した自分のリポジトリで: **Settings** タブ → 左メニュー **Secrets and variables** → **Actions** → 「**New repository secret**」

以下の5つを1つずつ登録します(Name は半角・大文字そのまま):

| Name | Secret(値) |
|---|---|
| `TLDV_API_KEY` | tldvのAPIキー |
| `CHATWORK_TOKEN` | ChatworkのAPIトークン |
| `OPENAI_API_KEY` | OpenAIのAPIキー(会社共有) |
| `MY_EMAILS` | 会議で使うメールアドレス。複数はカンマ区切り(例: `taro@stock-sun.com,taro@gmail.com`) |
| `MY_NAMES` | tldvの文字起こしに出る自分の名前表記。複数はカンマ区切り(例: `山田太郎,太郎 山田`) |

※自分の名前表記は、tldvで過去の会議を開いて文字起こしの話者名を見るのが確実です。

### 3. Actionsを有効化して試運転

1. 自分のリポジトリの「**Actions**」タブ → 「I understand my workflows, go ahead and enable them」が出たらクリック
2. 左の「**tldv minutes to Chatwork**」→ 右側の「**Run workflow**」→ 緑のボタンをクリック
3. 1〜3分待って、実行が緑のチェックになればOK
4. **直近26時間に参加した会議があれば**、Chatworkのマイチャットに議事録が届きます(未読で入ります)

以降は毎時0分(日本時間7時〜23時)に自動実行されます。何もしなくてOKです。

---

## 仕組み

1. tldv APIで直近26時間の会議を取得(自分のキーで見える範囲)
2. **自分が参加した会議だけ**を対象(主催者/招待者にメール一致 or 文字起こしの話者に名前一致)
3. 文字起こしをGPT(gpt-5-mini)で議事録に要約
4. 自分のマイチャットへ投稿(投稿は未読で入るので気付けます)
5. 処理済みは `state.json` に記録され二重投稿しません
6. 文字起こしが取得できない会議は3回リトライ後、「議事録未作成」のお知らせが1回だけ届きます

## カスタマイズ(任意・Secretsに追加するだけ)

| Name | 効果 |
|---|---|
| `CHATWORK_ROOM_ID` | 届け先をマイチャット以外のルームIDに変更 |
| `NOTIFY_ROOM_ID` | ボット自体が失敗したときの通知先ルームID(通常はマイチャットのルームIDを推奨) |
| `OPENAI_MODEL` | 要約モデル変更(既定: gpt-5-mini) |

マイチャットのルームIDは、ブラウザでマイチャットを開いたときのURL `#!rid〇〇〇〇` の数字です。

## うまく動かないとき

- **Actionsが赤い×** → 実行結果を開いて「Run minutes bot」のログを見る。「認証情報がありません」ならSecretsの名前・値を再確認
- **議事録が届かない(実行は緑)** → 直近26時間に参加した会議があるか、`MY_EMAILS`/`MY_NAMES`の表記が合っているかを確認
- **「議事録未作成」の通知が届く** → その会議の文字起こしをAPIから取得できない状態(主催者のプラン等)。tldv上では見られます
