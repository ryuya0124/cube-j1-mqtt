# Cube J1 watchdog

Debian 上で常駐し、MQTT の `cubej/cubej1/power` を購読します。Cube には通常時アクセスしません。ブローカーの retained メッセージは生存確認に使いません。

コンテナは Debian ホストの ADB サーバーを共有するため、`network_mode: host` を使用します。新しいポートは公開しません。

- 最後の生きた更新から10分間更新がない場合、Cube の `mqtt_ha_bridge` サービスの再起動を検討します。
- Cubeの状態MQTTが届いていれば、まず最大30分間はCube本体内の自己復旧に任せます。Wi-SUN UARTの最終応答時刻も確認し、モジュールが応答しながら再接続中なら最大2回だけ15分延期します。延期回数には上限があるため、Cube側の状態通知だけに依存して復旧が止まることはありません。
- 状態MQTTが届かない場合はSSHでログを確認します。状態MQTTとSSHの両方が使えなくても、電力MQTTの停止を根拠にADB、続いてSSHで復旧命令を試します。
- 本体再起動後10分間更新がなければ1時間休止します。24時間あたりブリッジ再起動4回、本体再起動2回が上限です。
- MQTT ブローカーとの接続が切れている間はCubeを操作しません。再接続後は判定の猶予を取り直します。

Cube の ADB が応答しない場合は、公開鍵とホスト鍵を固定した SSH 接続で同じ操作を試します。両方失敗した場合は記録して15分休止します。到達しなかった命令は日次の実施回数に数えません。

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
