# yobiru（ヨビル）

**あなたのエージェント同士に、会話を。**

Yobiru は **Tailscale** を使い、複数のコンピューターやクラウドワークスペースにいるパーソナルエージェントをつなぎます。メッセージ、構造化データ、ファイルを交換し、結果を返信し、相手の起動を要求できます。各エージェントの使い慣れた画面はそのままに、Yobiru が通信を担います。

<!-- README-I18N:START -->

[English](README.md) | [简体中文](README.zh-CN.md) | **日本語**

<!-- README-I18N:END -->

![AI 生成のコンセプト画像：一人のユーザーが二台のスマートフォンで Grok Bot と DOTS、ノートパソコンで Codex を使用し、それぞれの通常の会話メッセージで yobiru プラグインを通じた情報やファイルの交換を伝えている。](docs/assets/yobiru-demo.png)

*AI で生成した概念的なデモであり、実際のスクリーンショットや検証済みのエンドツーエンド連携ではありません。Grok Bot と DOTS はスマートフォン、Codex はノートパソコンに表示され、エージェントと受信プロセスは接続先のコンピューターやクラウドワークスペースで動作します。*

## なぜ Yobiru？

エージェントにはそれぞれ得意分野があり、アクセスできる情報も異なります。調査が得意なエージェント、あなたの背景を理解するエージェント、コードを扱えるエージェント。その回答やファイルを画面間でコピーし続けると、あなた自身が伝言役になってしまいます。

Yobiru は、エージェントが共有できる軽量な通信手段を提供します。

- **マシンを越えて会話：** `host/agent` で宛先を指定し、受信したメッセージに返信できます。
- **必要な情報を共有：** テキスト、JSON、レポート、ログ、その他のファイルを送れます。
- **起動は受信側のルールで：** 誰がどのエージェントを起動できるかを受信側で決め、通知を控える時間帯や頻度を設定できます。
- **今のツールを使い続ける：** 一つのプラグインに四つのスキルと Python CLI を収録し、Codex と Cursor のマニフェスト、Grok Bot の導入経路を用意しています。
- **データは自分のマシンに：** Yobiru はメッセージや添付ファイルの保管サービスを運営しません。ファイルは Tailscale 経由で送信元から取得します。

これは通信レイヤーです。新しいチャットアプリや自律的なタスク管理ツールではありません。エージェントはメッセージと返信を通じて共通の目標に取り組めますが、Yobiru は共有グループルームの UI、スケジューラー、相手の作業完了を証明する仕組みを提供しません。

## 三つのエージェントによる連携例

たとえば、**「今どんな話題が注目されていて、自分のプロジェクトに何が関係し、何を作るべきか？」** と依頼します。

| エージェント | 担当すること | 引き渡すもの |
|---|---|---|
| Grok Bot | 利用先のプラットフォームが既に提供している権限で、関連する X の情報を集めます。 | 要約、出典リンク、`signals.json`。 |
| DOTS | あなたの背景と照らし合わせ、発見をブリーフに整理します。 | `brief.md` と、コーディングエージェントへの質問。 |
| Codex | リポジトリを調べ、実現可能性を検討し、変更案や技術的な回答を用意します。 | 調査結果、ファイル、依頼元への返信。 |

これは利用イメージです。Yobiru は X へのアクセス権を付与せず、サービスの料金体系も変えず、プラットフォームにない機能も追加しません。**DOTS は条件付きの例です。** この CLI を実行し、tailnet に接続できる環境が必要であり、本リポジトリには検証済みの DOTS コネクターは含まれません。Grok Bot での完全な動作連携も、実際のアカウントでの検証が必要です。

## 仕組み

```text
You use the agent interface you already like
                    |
             Yobiru skills + CLI
                    |
       your permitted Tailscale network
                    |
     receiver -> local inbox -> optional wake adapter
                    |
       another agent reads, acts, and replies

Files: recipient fetches from the sender, then checks size + SHA-256.
```

受信プロセスは自分の Tailscale アドレスで待ち受けます。通常のポートは `tcp:7766` です。`tailscale whois` で接続元マシンの身元を確認し、ローカルの許可リストを照合してからメッセージを保存します。添付ファイルは送信元に一時保存され、宛先マシンだけが取得できる期限付きリンクで提供されます。リンクの標準有効期限は 24 時間です。

Yobiru は既存の **tailmsg protocol v1** を使用します。MCP サーバーは同梱せず、Yobiru アカウントも不要です。ネットワークとマシンの身元確認は Tailscale が担い、暗号化された通信は直接、またはリレー経由で運ばれます。[Tailscale の接続方式](https://tailscale.com/docs/reference/connection-types)を参照してください。

## 前提条件

参加するコンピューターやクラウドワークスペースごとに、次の条件が必要です。

1. **Python 3.8 以降**と CLI の実行権限（`python3`、Windows では `py -3`）。ランタイムは Python 標準ライブラリのみを使用します。
2. **Tailscale のインストールとログイン**が完了し、許可された tailnet を通じて両方のマシンに到達できること。選んだ接続元から受信ポートへの通信がネットワークポリシーで許可されている必要があります。
3. **受信する場合：** 到達可能で継続稼働する受信プロセスがあり、そのノードの Tailscale shields-up が無効であること。ファイルを渡す場合は、取得が終わるまで送信元の受信プロセスも稼働している必要があります。
4. **自動で起動する場合：** 対象エージェントに通知できるローカルプログラムまたは webhook を受信側で設定すること。スキルをインストールしただけでは、休止中のエージェントは起動しません。

スマートフォンは操作画面だけでも構いません。実行用の Tailscale ノードである必要はありません。シェルがない、許可されたプライベートネットワーク接続がない、受信プロセスを継続稼働できないクラウドエージェントでは、そのまま利用できません。ホストのサンドボックスと承認ルールも適用されます。Yobiru は Tailscale のログイン、アクセスポリシー、shields-up の設定を変更しません。

## インストール

### Codex

このリポジトリのマーケットプレイスを登録し、プラグインを追加します。

```sh
codex plugin marketplace add chekusu/yobiru
codex plugin add yobiru@yobiru
```

新しい会話を開き、**「このマシンで Yobiru が設定済みか確認して」** と依頼してください。四つのスキルは `yobiru-setup`、`yobiru-send`、`yobiru-inbox`、`yobiru-wake` です。

これはリポジトリのマーケットプレイスからのインストールです。Codex の公開プラグインディレクトリへの掲載を意味しません。

### Cursor と Grok Bot

Cursor でローカルに試す場合は、リポジトリをクローンし、プラグインディレクトリを読み込みます。

```sh
git clone https://github.com/chekusu/yobiru.git
cd yobiru
agent --plugin-dir "$PWD/plugins/yobiru"
```

`plugins/yobiru` を `~/.cursor/plugins/local/yobiru` に配置して Cursor を再読み込みする方法もあります。チームマーケットプレイスでは **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** に `https://github.com/chekusu/yobiru` を入力し、アクセス範囲を設定します。[Cursor のプラグイン文書](https://cursor.com/docs/plugins)を参照してください。

アカウントで利用できるマーケットプレイスに Yobiru が表示されたら、Grok Bot の **Plugins** から追加します。マーケットプレイスへのアクセスは Cursor チーム管理者が制御します。[Grok Bot のプラグイン文書](https://cursor.com/help/grok-bot/connect-plugins)を参照してください。**公開 Cursor Marketplace への掲載は未申請**のため、公開マーケットプレイスで検索しても表示されない場合があります。

Grok Bot の会話が実際に使用するコンピューターにも、Tailscale と受信プロセスを設定します。データは `/workspace/.tailmsg` などの永続ディレクトリに保存し、各コマンドの前に `--home /workspace/.tailmsg` を指定してください。ホストがスキルの文章だけを読み込み、同梱スクリプトを公開しない場合は、`/workspace` 内のクローンまたは展開したプラグインを使用します。スクリプトへのアクセス、受信プロセスの継続稼働、Grok Bot の実際の起動処理は、アカウント単位での検証が必要です。[プラットフォーム別の注意点](docs/PLATFORMS.md)を参照してください。

### プラグインホストを使わず CLI を利用

上記の方法でリポジトリをクローンし、同梱ランチャーを直接呼び出します。リポジトリのルートで実行してください。

```sh
Y="$PWD/plugins/yobiru/scripts/yobiru"
sh "$Y" paths
sh "$Y" doctor
```

インストール済みプラグインの場合は、`Y` をその `scripts/yobiru` の絶対パスに置き換えます。キャッシュのパスに空白が含まれることがあるため、引用符を付けてください。Windows では `py -3 "path\to\yobiru\scripts\yobiru.py"` を使用します。`doctor` は読み取り専用です。新しいマシンでは、設定と受信プロセスがまだないことを報告します。

## クイックスタート：二つのエージェントをつなぐ

例では架空の Tailscale ホスト名 `agent-laptop` と `research-node` を使用します。**自分で選んだ接続先**に置き換えてください。各マシンで前述の方法に従って `Y` を設定します。プラグインのインストールだけでは、接続先の設定、サービスの起動、メッセージ送信は行われません。

### 1. 送信を許可する相手を決める

`agent-laptop` で、`codex` エージェントが `research-node` から受信できるようにします。

```sh
sh "$Y" setup --allow research-node --agent codex --self-agent codex --default-agent codex
sh "$Y" setup --allow research-node --agent codex --self-agent codex --default-agent codex --apply
```

`research-node` で、`grok` エージェントが `agent-laptop` から受信できるようにします。

```sh
sh "$Y" setup --allow agent-laptop --agent grok --self-agent grok --default-agent grok
sh "$Y" setup --allow agent-laptop --agent grok --self-agent grok --default-agent grok --apply
```

最初のコマンドは変更をプレビューし、二つ目が適用します。相手とエージェント名が意図どおりか確認してください。Setup は既存の項目を保持し、設定変更時にバックアップを作成します。既存の識別設定や起動アダプターを置き換える場合は、明示的な置換オプションが必要です。

### 2. 両方のマシンで受信プロセスを動かす

最初のテストでは、**それぞれのマシン**の専用ターミナルで次を実行し、開いたままにします。

```sh
sh "$Y" serve
```

継続して利用する場合は、固定パスにランタイムを配置し、サービス定義を出力します。

```sh
sh "$Y" install-runtime
sh "$Y" install-runtime --apply
sh "$Y" service-template systemd-user
```

macOS では `systemd-user` の代わりに `launchd`、Windows では `windows` を指定します。出力されたサービス定義の手順に従ってインストールし、起動してください。これらのコマンド自体はサービスを起動しません。同じポートでサービスを起動する前に、フォアグラウンドの受信プロセスを停止してください。その後、別のターミナルで `sh "$Y" doctor` を実行します。

### 3. 挨拶し、データとファイルを送る

`agent-laptop` から、許可した相手に共有する予定の内容を送信します。

```sh
sh "$Y" send research-node/grok "Hello from Codex. Please send back a short status."
sh "$Y" send research-node/grok '{"type":"research-request","topic":"agent tools"}' --kind request
sh "$Y" send research-node/grok "Please review this brief." --file ./brief.md --wake batch
```

先に `brief.md` を作成するか、共有したい既存ファイルに置き換えてください。JSON はメッセージ本文であり、実行する指示ではありません。メッセージエンベロープの標準上限は 256 KiB です。大きいデータは添付ファイルにしてください。`--file` を繰り返すと複数のファイルを添付できます。

### 4. 読んで返信する

`research-node` で、`MESSAGE_ID` を `inbox` に表示された ID に置き換えます。

```sh
sh "$Y" inbox --pending --agent grok
sh "$Y" show MESSAGE_ID
sh "$Y" fetch MESSAGE_ID
sh "$Y" reply MESSAGE_ID "Reviewed. Here is my summary."
sh "$Y" ack MESSAGE_ID
```

`fetch` は添付ファイルを取得して検証します。`reply` は元の送信エージェントに返信し、`reply_to` を設定します。`ack` は**このマシン上だけで**処理済みにします。起動アダプターがない場合、エージェントへの依頼時、またはエージェント自身のスケジュール機能が動くときに受信箱を確認します。

## 起動と配信状態

送信側は `--wake now`、`batch`、`none` を要求できますが、最終的には受信側が決定します。`yobiru-wake` スキルでローカル実行プログラムや webhook を設定してください。`wake-config` は `--apply` を付けない限りプレビューのみで、送信元・エージェントごとの上限、通知を控える時間帯、頻度制限を設定できます。Webhook URL と認証情報は、非公開ファイルや環境変数を介して参照します。

アカウントで対応していれば、Grok Bot の routine webhook をアダプターの通知先にできます。[Routine webhook の文書](https://cursor.com/help/grok-bot/routines)を参照してください。

### オプションの Codex ブリッジ（macOS デスクトップまたは Linux app-server）

`codex-bridge` は認証済みのメッセージを、窓口となる**既存の Codex セッション 1 つ**にキュー登録します。macOS では Codex デスクトップアプリ、Linux では user systemd サービスと自分で起動した Codex app-server を経由します。どちらも公式の `codex queue` コマンドを使い、重複排除と再試行を備えた永続 SQLite キューを共有します。プラグインは Codex を起動しません。Linux では Codex CLI、同じユーザーで動きローカル Unix ソケットで待ち受ける app-server、そのセッションの UUID、利用可能な `systemctl --user`、`install-runtime` で導入した受信プロセスが必要です。

```sh
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock" --dry-run
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock"
sh "$Y" codex-bridge status
```

`--dry-run` はファイルを書かず、サービスマネージャーも呼び出しません。`--no-start` はファイルの書き込みだけを行います。使うのは同一ユーザーのローカルソケットだけです（ネットワークエンドポイント、新規セッション作成、`--last` は使いません）。ブリッジは `codex_home`（`--codex-home`、なければインストール済みの値、初回は `$CODEX_HOME`、最後に `~/.codex`）を固定し、照合が app-server と同じアカウントを読むようにします。新規インストールでは自動返信はオフで、許可を得たうえで `--allow-replies` を指定します。アップグレードでは許可済みの設定が保持されます（`--no-allow-replies` でオフにできます）。`enqueued` は CLI が通知を受け付けたことを示すだけで、タスク完了ではありません。`uncertain` はキュー登録の試行が Codex に届いた可能性があることを示し、自動では決して再送されません。確認方法は `status` が示します。詳細、権限、ロールバックは [Codex ブリッジ設定](CODEX_LOCAL.md)（中国語）を参照してください。

既存の環境では、指定したメッセージ種別を窓口セッションに入れないようにできます。`codex-bridge.json` に `excluded_kinds` と `excluded_kind_adapter`（信頼できるローカルプログラムの絶対パスの argv）を設定します。これらのメッセージは Codex キューに入りません。検証済み・未 ack・実効レベル `now` のメッセージを受信側が直接起動したときだけプログラムを実行し、追加する引数は inbox JSON のパスだけです（シェルを使わず、本文も渡しません）。`excluded_kind_timeout_seconds`（既定 60 秒）を超えると停止します。スキャン、再試行、`status` では実行されません。実際の制限時間は受信側の `hook_timeout_s` が切れる 10 秒前にも終わるため、先にプログラムとそのプロセスグループを停止します。プログラムが一度起動すれば、失敗やタイムアウトでもブリッジは 0 で終了し（結果は出力・記録されます）、自動で再実行されることはありません。メッセージは inbox に残り、再試行とメッセージ ID による重複排除はプログラム側の責任です。アップグレード時、既存のローカル窓口指示はそのまま保持されます。テンプレートの変更は自分で取り込んでください。インストーラーは `--dry-run` でこれらの項目を検証し、アップグレード時には調整済みの時間設定、独自の窓口指示、記録済みの Codex CLI、許可済みの返信設定とともに保持します。

| シグナル | 確認できること |
|---|---|
| `send` が HTTP `202` を返す | 受信側がメッセージを受け付け、保存したこと。 |
| 起動アダプターの成功 | 通知または webhook が受け付けられたこと。エージェントはまだ作業中かもしれません。 |
| `ack` | 受信側がローカルで処理済みにしたこと。送信側には通知されません。 |
| 結果を記した返信 | エージェントによる結果報告。完了の確認が必要な場合は、示された根拠を確認してください。 |

Yobiru はタスクの厳密な一回実行や、メッセージの自動再配信を保証しません。起動の試行には回数制限付きの再試行があります。メッセージ ID、ローカルの確認記録、タスク完了はそれぞれ別の概念です。

## プライバシーと信頼の境界

- **データは自分のマシンに保存されます。** 受信・送信記録と一時保存した添付ファイルはデータディレクトリに置かれます。優先順位は `--home DIR`、`$TAILMSG_HOME`、`~/.tailmsg` です。Yobiru は集中ストレージを運営しませんが、利用するエージェントの提供元が、エージェントに渡された内容を処理する場合はあります。
- **ネットワークの境界は Tailscale です。** 受信側は Tailscale の身元情報とローカルの許可リストを確認します。通信の暗号化は Tailscale が提供し、Yobiru は保存時の暗号化を追加しません。ホストとファイルシステムを保護してください。
- **エージェント名はルーティング用であり、隔離ではありません。** `from_agent` は送信側が指定します。同じマシンやデータディレクトリを共有するエージェントは、独立したセキュリティ主体ではありません。必要に応じてマシンを分けるか、適切なホスト側の隔離を利用してください。
- **メッセージは依頼であり、許可ではありません。** 受信した文章やファイルによって、コマンド実行、購入、設定変更、無関係な情報の開示が許可されることはありません。エージェントはユーザーの既存の指示と権限の範囲で行動します。
- **添付ファイルは慎重に扱います。** サイズと SHA-256 の検証は、内容を安全に実行できることを意味しません。リンクが期限切れになっても、取得済みのコピーは回収されません。
- **共有するつもりのデータだけを送ります。** 認証情報や無関係な非公開ファイルをメッセージに含めないでください。Webhook の秘密情報は保護されたファイルや環境変数に保存し、コミットやスクリーンショットには含めないでください。

本プロジェクトは、正式な独立セキュリティ監査を受けていません。信頼していた接続先やホストが侵害された場合、またはエージェントに過剰な権限がある場合、アクセスが悪用される可能性があります。

## 更新とアンインストール

Codex の更新：

```sh
codex plugin marketplace upgrade yobiru
codex plugin add yobiru@yobiru
```

更新後は新しい会話を開始します。Cursor では **Customize** から更新するかローカルコピーを置き換え、Grok Bot では **Plugins** で管理します。その後 `doctor` を実行してください。別途配置した受信ランタイムを更新するには、`install-runtime --upgrade` でプレビューし、`--apply` を追加して適用してから、受信サービスを手動で再起動します。

Codex プラグインとマーケットプレイスの削除：

```sh
codex plugin remove yobiru@yobiru
codex plugin marketplace remove yobiru
```

プラグインを削除しても、データ、受信サービス、Codex ブリッジは残ります。完全に削除する場合は、先にサービスを停止・削除し、メッセージやファイルが不要であることを確認してからデータディレクトリを削除してください。

既存の tailmsg 環境との互換性は維持されます。`tailmsg.py`、protocol v1、`~/.tailmsg`、`TAILMSG_*`、既存のサービス名を引き続き使用します。

## 開発と検証

```sh
python3 tools/build_plugin.py sync
python3 tools/build_plugin.py check
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge test_install_codex_bridge_linux
python3 tools/build_plugin.py release
```

`sync` はマニフェストと、バイト単位で一致する同梱ソースを再生成します。`check` は生成ファイルの不一致やパッケージの問題を検出します。`release` は再現可能なプラグイン ZIP、マーケットプレイス ZIP、`SHA256SUMS` を `dist/` に作成します。[リリース手順](docs/RELEASING.md)と[通信プロトコル](SPEC.md)を参照してください。

| 対象 | 現在の検証範囲 |
|---|---|
| パッケージと CLI | 展開後の実行、空白を含むパス、設定の保持、隔離したテスト環境でのメッセージ・ファイル・返信・起動処理を自動検証しています。 |
| Tailscale 通信 | 0.1.0 で（0.2.x では再実行していません。コアは変更なし）、上流の `selftest.sh` は、一台の Linux マシンの実際の Tailscale アドレスで一時受信プロセスを使い、59 項目に合格しました。二台のマシン間のテストではありません。 |
| Codex | 0.1.0 で（0.2.x では未再確認）Linux CLI でのインストール、有効状態、四つのスキルの検出、プラグインキャッシュからのコマンド実行を確認済みです。0.2.x の Linux と macOS のブリッジは、偽の CLI・偽の `systemctl`・一時ソケットを使うオフラインテストと展開済みパッケージのテストでのみ確認しています。実際の app-server での動作確認は主張しません。 |
| Cursor / Grok Bot / DOTS | Cursor／Grok の導入経路はホストの文書に基づきます。実環境での受け入れ確認と、複数プラットフォーム間のエンドツーエンド連携は未検証です。DOTS コネクターは同梱していません。 |
| 公開マーケットプレイスへの掲載 | 未申請・未承認です。リポジトリからのインストールとは別です。 |

`sh selftest.sh` には Tailscale と空いている一時ポート `7866–7883` が必要です。本番用の `7766` や標準データディレクトリは使用しません。残る確認項目は[プラットフォーム別の注意点と出典](docs/PLATFORMS.md)に記載しています。

リリース時の詳しい確認記録は[検証ノート](docs/VERIFICATION.md)に記載しています。

## コントリビュートとライセンス

[chekusu/yobiru](https://github.com/chekusu/yobiru) への issue と pull request を歓迎します。OS、ホストのバージョン、最小の再現手順、機密情報を除去した出力を添えてください。診断情報を共有する前に、IP アドレス、tailnet 名、アカウント識別子、メッセージ本文、認証情報を取り除いてください。脆弱性が疑われる場合は、利用可能であれば GitHub の非公開 **Report a vulnerability** を使用し、公開 issue に秘密情報を載せないでください。

`README.md` を英語の正本とし、中国語版・日本語版も同時に更新してください。コード変更の提出前には上記のチェックを実行し、通信プロトコルを変更する際は既存の接続先との相互運用性を保ってください。

Yobiru は **Chekusu** のもとで保守され、[MIT License](LICENSE) で公開されています。tailmsg プロジェクトから発展し、CLI とプロトコルの互換性を保っています。Grok Bot、DOTS、Codex、Cursor、Tailscale はそれぞれの製品名です。本書への掲載は、各製品による推奨を意味しません。
