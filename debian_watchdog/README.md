# Cube J1 watchdog

Debian 上で常駐し、MQTT の `cubej/cubej1/power` を購読します。Cube には通常時アクセスしません。ブローカーの retained メッセージは生存確認に使いません。

コンテナは Debian ホストの ADB サーバーを共有するため、`network_mode: host` を使用します。新しいポートは公開しません。

- 最後の生きた更新から10分間更新がない場合、Cube の `mqtt_ha_bridge` サービスを ADB で再起動します。
- さらに7分間更新がない場合、Cube 本体を ADB で再起動します。
- 本体再起動後10分間更新がなければ1時間休止します。24時間あたりブリッジ再起動4回、本体再起動2回が上限です。
- MQTT ブローカーとの接続が切れている間はCubeを操作しません。再接続後は判定の猶予を取り直します。

Cube の ADB が応答しない場合は、公開鍵とホスト鍵を固定した SSH 接続で同じ操作を試します。両方失敗した場合は記録して15分休止します。

監視ログは `data/events.jsonl`（10 MB × 最大6ファイル）、状態は `data/state.json` に残ります。Docker のログにも同じイベントが出ます。

## 操作

```sh
cd /home/ryuya/docker/cube-j1-watchdog
docker compose ps
docker compose logs -f --tail=50
tail -f data/events.jsonl
docker compose stop watchdog
docker compose start watchdog
```

設定値は `compose.yaml` の環境変数で変更できます。`secrets/mqtt.json` は MQTT 購読用認証情報、`secrets/cube_ssh_key` は Cube 専用の秘密鍵、`secrets/cube_known_hosts` は検証済みのホスト鍵です。これらは GitHub に含めません。Cube の B ルート認証情報は保存しません。
