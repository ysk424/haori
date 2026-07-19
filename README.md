# Haori

Haoriは、[Yohsai](https://github.com/ysk424/yohsai)で着付けと縫い合わせを完了した服を、アニメーションするBodyへ高速に追従させるBlender Extensionです。

最終品質の計算をZOZOへ渡す前に、ポーズ、貫通、布の動きの傾向を短時間で確認するための簡易シミュレーターです。レンダリングにおけるEEVEEとCyclesの関係のように、普段の反復確認にはHAORI、最終計算にはZOZOを使うことを想定しています。

## v0.2.0でできること

- Blender再起動後の保存済みYohsai服、縫い合わせ、速度、マテリアル格子情報を直接読み取る
- Armatureで変形したBodyをサブフレーム単位で評価する
- 1フレーム間のBody頂点移動量をワールド座標で測定する
- 最大移動量が指定値以下になるよう、Gravity計算回数を自動決定する
- 各Body中間姿勢でYohsaiのNormal Gravity 1回相当を実行する
- 整数フレームの結果を絶対Shape Keyへ保存する
- 元のYohsai服を変更せず、別CollectionにHAORI結果を作る
- CPU性能と目的に合わせてFast、Standard、Quality、Customを選択する
- Body Step、Contact Clearance、Solver Iterationsを個別調整する
- 完成キャッシュをBakeし、以後の再計算から切り離す

## 必要環境

- Blender 5.1以降
- Windows x64
- YohsaiでGravity完了済みの服
- フレーム範囲を通してトポロジーが変わらないBodyメッシュ

v0.2.0はBlender 5.2 LTSで検証しています。配布ZIPにはネイティブソルバーとMicrosoft OpenMP Runtimeが含まれます。

## インストール

1. [Releases](https://github.com/ysk424/haori/releases)から`haori-0.2.0-windows_x64.zip`を取得します。
2. Blenderの`Edit > Preferences > Extensions`を開きます。
3. メニューから`Install from Disk`を選び、ZIPを指定します。
4. Haoriを有効にします。

ZIPを展開する必要はありません。

## 使い方

1. Yohsaiで着付け、縫い合わせ、Gravityを完了し、`.blend`を保存します。
2. BodyのArmatureアニメーションを用意します。
3. 3D Viewportのサイドバーから`Haori`タブを開きます。
4. `Yohsai Clothes`と`Body`を確認します。自動検出されない場合は手動で指定します。
5. `Start Frame`と`End Frame`を指定します。
6. `Maximum Body Step (cm)`を指定します。初期値は`1.0 cm`です。
7. Performance Preset、Contact Clearance、Solver Iterationsを確認します。
8. `Simulate Animation`を実行します。
9. 結果を残す場合は`Bake HAORI Result`を実行します。

実行中は`Esc`でキャンセルできます。

## 速度と品質

| Preset | Body Step | Contact Clearance | Iterations | 用途 |
| --- | ---: | ---: | ---: | --- |
| Fast | 2.0cm | 0.75cm | 10 | CPU負荷を抑えた動きの確認 |
| Standard | 1.0cm | 0.5cm | 20 | 標準的なプレビュー |
| Quality | 0.5cm | 0.5cm | 30 | 貫通と収束を優先した確認 |
| Custom | 任意 | 任意 | 任意 | 服とBodyに合わせた調整 |

Body Stepを大きくするとBody中間姿勢が減り、Iterationsを小さくすると各姿勢の拘束計算が減ります。Contact Clearanceを大きくすると貫通への余裕は増えますが、服がBodyから浮きます。衝突候補の内部探索距離4cmは変更しません。

N-panelには、Body中間姿勢1つ当たりの接触計算回数も表示します。内部Substepsは物理時間へ影響するため8回で固定しています。

## Bodyの自動分割

フレーム`f`から`f+1`の間で、同じBody頂点がワールド空間をどれだけ移動したか測定します。最大移動量を`d`、設定値を`L`とすると、最初の分割数は次の値です。

```text
steps = max(1, ceil(d / L))
```

各中間姿勢を実際に評価し、隣接姿勢間の移動が`L`を超えた場合は分割数を増やします。各姿勢につきNormal Gravity 1回相当を計算します。内部Substepsは8回固定、Solver IterationsはN-panelの設定値（初期値20回）を使用します。

## 出力

結果は`<Yohsai Clothes名>_HAORI` Collectionへ作成されます。

- 元のYohsaiオブジェクトは変更せず、計算完了後は非表示にする
- HAORI側へメッシュを複製する
- 各整数フレームを`HAORI_####` Shape Keyとして保存する
- Shape Keyの`eval_time`をフレームへ追従させるDriverを設定する
- 計算範囲、最大分割数、最大Body移動量、最大移動頂点番号をCollectionへ記録する

同じYohsai Clothesで再実行すると、以前の未Bake HAORI出力は新しい計算開始時に削除されます。Bake済み出力は残ります。

### Bake

`Bake HAORI Result`は、完成したShape Keyキャッシュを`<Yohsai Clothes名>_HAORI_BAKED`へ確定します。Baked Collectionは通常のBlender Shape Keyアニメーションとして動作し、以後のHAORI再計算では削除されません。複数案を残す場合は、各Simulationの完了後にBakeしてください。

## Yohsaiの下流での位置付け

Yohsaiは服、縫い合わせ、初期Gravity状態を作ります。その下流では、用途と計算環境に応じて複数の方法があります。

| 方法 | 特徴 |
| --- | --- |
| Body追従 | 服をBodyと一緒に動かし、服と重なるBody領域を非表示または削除する。高速だが、Rig、Weight、Body編集の知識が必要 |
| Blender XPBD | Blender標準のCloth/XPBDを構成する。柔軟だが、Collision、Quality、Pin、Cacheの調整知識が必要 |
| HAORI | Yohsai完成状態をそのまま使い、CPUで簡易アニメーションを作る。ZOZOを実行できない環境向け |
| ZOZO | CUDA GPUで高品質な最終シミュレーションを行う |

VRキャラクター制作では、視聴者や共同制作者のPCでZOZOを実行できない前提が現実的です。HAORIは、専門的なBody加工やBlender XPBD設定を必須にせず、Yohsaiから直接進める3番目の選択肢として配置します。ZOZOを使える制作環境では、HAORIで日常的に確認し、最終結果だけZOZOで計算できます。

## 現在の制限

HAORIは高速な確認用ソルバーで、ZOZOの代替となる最終シミュレーターではありません。

- 布同士の自己衝突は計算しない
- Body接触は安定性優先の簡易接触で、Bodyの運動量を完全には布へ伝えない
- BodyはArmature変形のように頂点数と面数が一定である必要がある
- 1フレーム内で大きく動いて元の位置付近へ戻る特殊な補間は、端点移動量だけでは十分に分割されない場合がある
- 整数フレームごとに全頂点をShape Keyへ保存するため、長い範囲や高密度の服ではメモリ使用量が増える
- 1フレーム当たりのBody分割数は安全のため最大512回
- 再実行開始時に以前の未Bake HAORIキャッシュを置換するため、必要な結果は先にBakeする必要がある
- v0.2.0のHAORIソルバーは明示的な実行時並列化を行わず、Body分割を順番に計算する
- v0.2.0の配布バイナリはWindows x64のみ

詳細は[アーキテクチャ資料](docs/ARCHITECTURE.md)を参照してください。

## ソースからのビルド

Visual Studio 2022とCMake 3.24以降を使用します。

```powershell
.\build_native.ps1 -Configuration Release
```

macOS/Linux用のビルドスクリプトもありますが、v0.2.0のマニフェストと配布物はWindows x64のみを対象にしています。

## テスト

ネイティブテストはビルドスクリプト内で実行されます。Blender統合テストにはBlender実行ファイルが必要です。

```powershell
blender --background --factory-startup --python tests\blender_simulation_check.py
```

保存済みYohsaiファイルを使う検証スクリプトも`tests`に含まれますが、テスト対象の`.blend`はリポジトリに含まれません。

## ライセンス

HaoriはGPL-3.0-or-laterです。配布バイナリに含まれるMicrosoft OpenMP Runtimeについては[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。
