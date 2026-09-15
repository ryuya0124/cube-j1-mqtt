# Cube J1 watchdog

Debian 上で常駐し、MQTT の `cubej/cubej1/power` を購読します。Cube には通常時アクセスしません。ブローカーの retained メッセージは生存確認に使いません。

コンテナは Debian ホストの ADB サーバーを共有するため、`network_mode: host` を使用します。新しいポートは公開しません。

- 最後の生きた更新から10分間更新がない場合、Cube の `mqtt_ha_bridge` サービスの再起動を検討します。
- Cubeの状態MQTTが届いていれば、まず最大30分間はCube本体内の自己復旧に任せます。Wi-SUN UARTの最終応答時刻も確認し、モジュールが応答しながら再接続中なら最大2回だけ15分延期します。延期回数には上限があるため、Cube側の状態通知だけに依存して復旧が止まることはありません。
- 状態MQTTが届かない場合はSSHでログを確認します。状態MQTTとSSHの両方が使えなくても、電力MQTTの停止を根拠にADB、続いてSSHで復旧命令を試します。
- 本体再起動後10分間更新がなく、Cube専用P110Mが明示的に有効化されていれば、最後の手段として15秒間だけ物理電源を切って戻します。電源再投入は最短6時間間隔、24時間あたり4回が上限です。P110Mが未設定または無効なら電源操作せず1時間休止します。
- 物理電源再投入後は最大6回ONを試し、ON確認が取れない場合も実施済みとして記録して6時間以内の再試行を防ぎます。
- 24時間あたりブリッジ再起動4回、本体再起動2回が上限です。
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

## Cube専用 Tapo P110M（任意）

P110M制御は初期状態では無効です。IPアドレスや表示名だけでは対象を決めません。設定したIPへ接続した実機が `P110M` であり、MACアドレスとTP-Link device IDの両方が登録値と一致した場合だけOFFを許可します。既存P110MのMACは `forbidden_macs` に登録し、その値を対象として設定しても有効化できないようにします。

到着後は次の順で設定します。

1. Tapoアプリで新しいP110Mだけを追加し、表示名を `Cube J1 power` にします。スケジュール、留守番モード、自動OFFは設定しません。停電復帰時の状態を選べるファームウェアなら `ON` にします。
2. DHCPでこの新規P110MのIPを固定します。
3. `tapo.json.example` を `secrets/tapo.json` にコピーし、まず `enabled: false` のまま、新規P110MのIPとTP-Linkアカウントのユーザー名・パスワードだけを記入します。
4. 次の読み取り専用コマンドで識別情報を取得します。このコマンドはON/OFFを変更しません。

   ```sh
   cd /home/ryuya/docker/cube-j1-watchdog
   docker compose run --rm watchdog python3 /app/tapo_power.py probe /run/secrets/tapo.json
   ```

5. 出力とTapoアプリの新規プラグのMACを照合し、`expected_mac`、`expected_device_id`、`expected_alias`を記入します。既存P110MのMACをすべて `forbidden_macs` に記入します。拒否リストが空の場合や対象MACが拒否リストに含まれる場合は有効化できません。
6. `allow_power_off` を `ALLOW_CUBE_J1_POWER_CYCLE`、`enabled` を `true` にして `docker compose up -d --build` を実行します。起動ログの `smart_plug_armed` が `true` になったことを確認します。

認証情報と実機識別情報を含む `secrets/tapo.json` はGitHubに含めません。Home AssistantへTP-Link Smart Home統合として追加する場合も、復旧エージェントはHome Assistantを経由せずLAN内で新規P110Mを直接制御します。
