# Haori CUDA v1.1.0 アーキテクチャ

## 設計目標

Haori CUDAは、Yohsaiの完成状態を入力に、四角格子の材料寸法へ反復収束するプレビュー用Cloth SolverをGPU常駐化します。CPU版との頂点単位の一致ではなく、四角形材料、Seam、反復収束、Body接触という挙動を維持しながらCPU/GPU転送とPython処理を減らすことを目標にします。

## データフロー

```text
Yohsai保存状態 ──初期化H2D──> CUDA Solver
  Clothes vertices/velocity       ├─ positions / previous / velocity
  Seam / Edge / Quad / Bend       ├─ constraint colors + CUDA Graph
  Locked vertices                 ├─ Body vertices/faces + GPU BVH
  Body mesh                       └─ collision candidates + stats
                                         │
Blender evaluated Body ─姿勢ごとH2D──────┘
                                         │ GPU内で全substep/iteration
                                         ▼
整数フレーム終端 <──位置・速度・統計D2H── CUDA Solver
        │
        └─ Absolute Shape Key cache
```

服、拘束、Body Face、BVHトポロジー、候補Face、統計はSolverの寿命中GPUへ常駐します。Bodyの頂点位置だけを各中間姿勢でPinned Host Bufferから非同期転送します。服の状態はBody stepごとには読み戻さず、整数フレームの出力時だけ同期してD2Hします。

## 四角格子と反復収束

Yohsaiの材料情報から次の拘束を構築します。

| 拘束 | 保存するRest値 | 目的 |
| --- | --- | --- |
| Edge | 隣接頂点の長さ | Warp/Weft方向の寸法を戻す |
| Quad | `u·u`, `v·v`, `u·v` | 四角形の縦横寸法とShearを戻す |
| Bend | 同一軸上の前後長 | 軸方向の折れを緩和する |
| Seam | 縫い合わせ頂点ペア | 捕捉後にゼロ長へ閉じる |

拘束は頂点を共有しない集合へCPUでGreedy Coloringします。同じ色の拘束は互いの書き込み先が重ならないためGPUで並列実行でき、色は順番に処理します。反復ごとに色順を反転し、Edgeは4 sweep実行します。Solver Iterationsを増やすほど保存寸法・Quad metricの誤差が減る設計です。

CUDAは単精度かつ並列であり、CPU版と補正順序も異なります。したがって完全一致やbitwise reproducibilityは保証せず、材料の不変条件と誤差減少をネイティブテストで検証します。

## CUDA Graph

内部Substep、Seam attraction、積分、拘束色、Body contact、速度更新、統計集計からなるカーネル列を、Solver IterationsをKeyとしてCUDA GraphへCaptureします。初回だけGraphを構築・Instantiateし、以後は1回の`cudaGraphLaunch`で同じ計算列を起動します。

単一の長時間Cooperative Kernelは使いません。通常カーネル境界を全GPU同期点として利用するため、GPU全体を占有するGrid Synchronizationに依存せず、大きな格子でも起動できます。

## Body BVHと衝突

Body Faceから平衡二分BVHのトポロジーを初期化時にCPUで一度作ります。各Body姿勢では次をGPUで実行します。

1. Body頂点をPinned Bufferから非同期H2Dする。
2. Leaf AABBを更新する。
3. 親NodeをBottom-upにRefitする。
4. 各Cloth頂点から4cm以内の最近傍Faceを探索する。
5. 近傍Faceがない場合、3方向Parity Rayの多数決でBody内部を判定する。
6. Cloth頂点ごとに1 Faceを候補として接触補正する。

Body FaceとBVHトポロジーは変えません。頂点数またはFace数が変わった場合はPythonとC APIの両方で拒否します。

## Blender/Python側

Pythonが担当する処理は次の範囲です。

- Yohsai Custom PropertyとMesh Attributeの検証・読取
- Dependency GraphからアニメーションBodyを各時刻で評価
- Body最大移動量による1フレームの自動分割
- Solver作成、Body姿勢差し替え、CUDA resident advanceの呼出
- 整数フレームだけの状態取得とShape Key保存
- キャンセル、エラー復旧、Bake、UI

通常実行ではPython `BVHTree`を構築しません。CPU BVH候補APIは互換テスト用として残し、`advance_resident`が本番経路です。

## C API

API version 10はopaque handleを使用します。

- `hsc_create` / `hsc_destroy`
- `hsc_replace_state` / `hsc_replace_seam_state`
- `hsc_replace_body`
- `hsc_advance_resident`: GPUでBody候補を作り、状態を読み戻さず進める
- `hsc_advance`: 明示候補を渡す互換・単体テスト経路
- `hsc_get_state`: 整数フレーム出力時の同期D2H

入力検証失敗時は既存GPU状態へ部分反映しません。Pythonはinteger-frameの最後に保持した状態をrollback用に使います。

## メモリと同期

- CUDA RuntimeとMSVC Runtimeは配布DLLへ静的リンクします。
- Body姿勢転送は2組のPinned Buffer/Eventで再利用を管理します。
- Solverごとのnon-blocking CUDA streamへ転送とGraph実行を順序付けします。
- CUDA Graph実行中のCloth/Body中間値をCPUへ戻しません。
- `state()`、エラー確認、Solver破棄が同期点です。

Haori UIは同時に1つのRunnerだけを許可します。C API handleへの同時呼出は想定しません。

## 出力、失敗、キャンセル

計算開始時にSource Meshを複製して`<source>_HAORI`を作り、完了時に整数フレームを絶対Shape Keyへ格納します。元のYohsai Meshは編集しません。

CUDAエラー、非有限値、Bodyトポロジー変化では計算を停止します。途中出力を削除し、Source表示とScene時刻を復元します。完了した未Bake cacheは同じSourceの次回実行で置換し、Bake済みcacheは残します。

## 検証方針

- Gravity以外の隠れた外力がないこと
- Quadの剛体変換不変性
- Iterations増加で材料寸法誤差が減ること
- 32×32四角格子が複数CUDA blockでも収束すること
- GPU BVH候補でBody接触が解けること
- 複数resident stepをD2Hなしで連続できること
- Solverの生成・破棄を繰り返して安定すること
- Compute Sanitizerで無効アクセスとLeakがないこと
- BlenderでSimulation、Shape Key cache、Bake、Cancelを通すこと

## 制限と今後の候補

- Cloth自己衝突
- 複数GPUへの分割
- Body速度を伝える高度な接触モデル
- Shape Key以外の省メモリcache
- Linux用配布物
- CUDA/CPU間の定量的ベンチマークと品質指標の自動レポート
