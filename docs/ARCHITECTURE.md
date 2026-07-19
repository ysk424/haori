# Haori v0.2.1 アーキテクチャ

## 目的

Haoriは、Yohsaiで完成した服の状態を初期値として、アニメーションするBody上で短時間の反復確認を行うための簡易シミュレーターです。

服の作成、型紙読込、縫い合わせ編集、ZOZOへの受け渡しは担当しません。Yohsaiの成果物を読み取り、元データを変更せず、HAORI専用のアニメーションキャッシュを作ることだけを担当します。

## コンポーネント

```text
Yohsai保存状態
  ├─ Clothes Collection
  ├─ Part Meshes
  ├─ Sewing Pairs / Seam State
  └─ Material Lattice Data
           │
           ▼
simulation.py
  ├─ 入力検証
  ├─ Bodyサブフレーム評価
  ├─ 自動分割
  ├─ 衝突候補作成
  └─ フレームキャッシュ
           │
           ▼
cosserat_native.py
  └─ ctypes / versioned C API
           │
           ▼
haori_cosserat.dll
  ├─ Gravity積分
  ├─ Seam拘束
  ├─ Material格子拘束
  └─ Body接触
           │
           ▼
HAORI Collection
  └─ Absolute Shape Keys + Driver
```

## Yohsai入力契約

HaoriはJSONの書き出しを必要としません。Yohsaiが`.blend`へ保存したCollection、Object、Mesh Attributeの状態を直接読み取ります。そのため、Blenderを再起動した後でも必要な情報を復元できます。

### Clothes Collection

主に次のCustom Propertyを使用します。

| Property | 用途 |
| --- | --- |
| `yohsai_role` | `clothes`であることを確認 |
| `yohsai_kitsuke_parts` | グローバル頂点番号を再構築するための順序付きPart名 |
| `yohsai_kitsuke_seams` | グローバル頂点番号による縫い合わせペア |
| `yohsai_kitsuke_seam_rest` | 保存済みSeam状態 |
| `yohsai_kitsuke_revision` | Gravity完了状態のRevision |
| `yohsai_kitsuke_backend` | `STABLE_COSSERAT`互換性の確認 |

### Part Object / Mesh

| Property / Attribute | 用途 |
| --- | --- |
| `yohsai_gravity_state` | PartごとのGravity完了確認 |
| `yohsai_kitsuke_locked` | 固定Partの指定 |
| `yohsai_kitsuke_matrix` | 保存後にObject変換が変わっていないか確認 |
| `yohsai_kitsuke_velocity` | 保存済み頂点速度 |
| `yohsai_pattern_position` | 型紙空間の頂点位置 |
| `yohsai_pattern_edge_rest` | マテリアルEdgeのRest Length |
| `yohsai_grainline_family` | Warp/Weft/Proxy分類 |
| `yohsai_grainline_quad` | Shear拘束用の格子グループ |

保存済みObject変換と現在のObject変換が一致しない場合、Haoriは安全のため保存速度をゼロへ戻します。位置は現在のObject World Matrixでワールド座標へ変換します。

## Body評価と分割

BodyはDependency Graphから評価済みMeshとして取得します。Armature Modifierの結果だけでなく、その時刻におけるModifier Stack全体の最終形状が対象です。

フレーム区間`[f, f+1]`ごとに次を行います。

1. `f`と`f+1`の評価済みBody頂点をワールド座標で取得する。
2. 同じ頂点番号同士の距離を計算する。
3. 最大距離`d_max`と、その頂点番号を記録する。
4. 設定値`limit`から`ceil(d_max / limit)`を初期分割数とする。
5. `f + i / steps`でBodyを実際に評価する。
6. 隣接する評価姿勢の最大頂点移動量が`limit`を超える場合、分割数を増やして再評価する。
7. 各中間Body姿勢でGravity計算を1回進める。

Bodyの頂点数または三角形数が変わった場合は計算を中止します。ネイティブ側でも同じCountを検証し、無効なBody差し替えを既存状態へ部分反映しないようにしています。

## 1回のGravity計算

Body中間姿勢1つにつき、ネイティブソルバーを1回進めます。

- Gravity: `9.81 m/s²`
- 内部Substep: `8`
- Solver Iteration: N-panelの設定値（初期値`20`）
- Seam: 保存済みペアをゼロ長へ収束
- Material: Warp/Weft Edge、Quad Shear、軸方向Bend
- Collision: Pythonで近傍Body Face候補を作り、ネイティブ側で接触補正

接触時の速度保持率はゼロです。Body接触が布を発射することを避け、Gravityで再び落ち着かせる安定性重視の設計です。このため、Body運動から布への完全な運動量伝達は行いません。

## 速度・品質パラメータ

HAORIは内部Substepsを8回に固定し、次の3値で速度と品質を調整します。

| Parameter | 影響 |
| --- | --- |
| `Maximum Body Step` | 大きいほどBody中間姿勢が減って高速になるが、移動途中の衝突を見落としやすい |
| `Contact Clearance` | Body表面から維持する距離。大きいほど貫通の余裕が増えるが、服が浮く |
| `Solver Iterations` | 各内部SubstepのMaterial/Contact反復。小さいほど高速だが収束が弱い |

フレーム当たりの最大Contact Passは`Body Steps × 8 × Solver Iterations`としてCollectionへ保存します。Contact ClearanceはネイティブSolver作成時の`contact_thickness`へ渡します。衝突候補探索距離4cmは、候補数とCPU負荷を不用意に増やさないため固定です。

## 衝突候補

各布頂点について、BodyのAABB付近にある頂点だけを候補とします。BVHで4cm以内の最近傍Faceを探し、見つからない場合でもBody内部と判定された頂点は最近傍Faceを取得します。

内部判定は3方向へのRay Castの多数決です。これは閉じたBodyを前提としています。穴、重複面、反転面、非多様体形状では期待どおりにならない場合があります。

## 出力とキャッシュ

計算開始時に入力Partを複製し、`<source>_HAORI` Collectionへ入れます。

- Source Meshは変更しない
- Modifier、Constraint、Animation Dataは出力コピーから除く
- 各整数フレームのワールド頂点位置をメモリへ保存
- 完了時にObjectローカル座標へ戻して絶対Shape Keyへ格納
- `eval_time` DriverでScene FrameとShape Keyを対応させる

Collectionには次のHAORIメタデータを保存します。

| Property | 内容 |
| --- | --- |
| `haori_cache_ready` | キャッシュ完成フラグ |
| `haori_start_frame` / `haori_end_frame` | 計算範囲 |
| `haori_maximum_body_step_cm` | 指定された最大Body Step |
| `haori_maximum_substeps` | 実際に必要だった最大分割数 |
| `haori_maximum_body_movement_cm` | フレーム端点間で観測した最大移動量 |
| `haori_maximum_body_vertex` | 最大移動を記録したBody頂点番号 |
| `haori_body_object` | 使用したBody Object名 |
| `haori_contact_clearance_cm` | 使用した接触距離 |
| `haori_solver_iterations` | 使用したSolver反復数 |
| `haori_internal_substeps` | 固定内部Substep数 |
| `haori_maximum_contact_passes_per_frame` | 最大Body分割数を含むフレーム当たりのContact Pass |

## Bake

Simulation完了時点で出力はすでに絶対Shape KeyとFrame Driverを持ちます。Bakeは頂点を再計算せず、`eval_time`を各整数フレームへLINEARキーフレーム化してDriverを除去し、完成キャッシュを永続資産として確定します。

- Collection Roleを`simulation`から`baked`へ変更する
- Part Roleを`simulation_part`から`baked_part`へ変更する
- CollectionとPartを`HAORI_BAKED`名へ変更する
- `haori_baked`フラグを保存する
- Shape Keyは維持し、Frame Driverを通常のBlender Actionへ変換する

再計算時に削除されるのは`simulation` Roleだけです。したがってBaked CollectionはHAORI Extensionがなくても再生でき、別のHAORI案と共存できます。

## 再実行、失敗、キャンセル

- 同じSource Collectionの既存HAORI出力は、新しいRunner作成時に削除する。
- 正常完了時はSource Partを非表示のままにし、HAORI出力を表示する。
- 初回計算をキャンセルした場合は、Sourceの元の表示状態とScene Frameを復元する。
- 既存キャッシュを置換する再実行が失敗またはキャンセルされた場合は、Sourceを表示して部分出力を削除する。
- ネイティブRuntimeは完了、失敗、キャンセル、Extension解除時に解放する。

再実行はトランザクションとして旧Simulationキャッシュを保持しません。重要な結果は、再実行前にBakeしてください。

## 安全上限

1フレームの分割数は最大512です。設定値が小さすぎる場合やBody移動が極端に大きい場合は、無制限に計算せずエラーで停止します。

## 実行モデルと並列化

v0.2.1はPythonだけで実装されていません。Blenderとの統合、Body評価、自動分割、衝突候補、Shape Key保存はPythonで行い、Gravity、Seam、Material拘束、Body接触はC++の`haori_cosserat.dll`で行います。

フレーム区間、Body中間姿勢、ネイティブGravity Callは順番に処理します。C++ソルバーの拘束ループにも、v0.2.1では明示的なOpenMP並列領域を設けていません。Gauss-Seidel型の補正順序が計算結果へ影響するためです。

ビルドスクリプトの`--parallel`は複数のコンパイル処理を並列化する指定であり、実行時シミュレーションの並列化ではありません。CMakeはOpenMPツールチェーンとRuntimeを構成していますが、v0.2.1のソルバーコード自体は明示的に使用していません。NumPyまたはBlender内部の処理が、それぞれの実装によって並列化される場合はあります。

## 今後検討する課題

- 非線形補間の中間点を初期分割数の決定にも利用する
- 既存キャッシュを維持したまま再計算し、成功時だけ置換する
- Shape Key以外の省メモリキャッシュ形式
- 開いたBodyや非多様体Bodyに対する内部判定の改善
- 衣服自己衝突
- Body速度を考慮したプレビュー向け接触モデル
- 結果の再現性を維持できる拘束単位の安全な並列化
- macOS/Linux配布バイナリ
