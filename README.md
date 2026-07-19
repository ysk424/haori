# Haori CUDA

Haori CUDAは、[Yohsai](https://github.com/ysk424/yohsai)で着付けと縫い合わせを完了した服を、アニメーションするBodyへ高速に追従させるBlender Extensionです。

CPU版Haoriを比較用の基準として残し、この`accelerated`版では1フレームの物理計算中に服・速度・拘束・Body・BVH・衝突候補をGPUメモリへ常駐させます。CPU版と浮動小数点の計算順序は異なるため頂点位置の完全一致は目標にせず、次の性質を維持します。

- 服の材料構造を四角格子（QuadのWarp/Weft/Shearと軸方向Bend）として扱う
- Solver Iterationsを重ねるほど、隣接頂点とQuadが保存済みの寸法・形状へ収束する
- YohsaiのSeamを閉じ、アニメーションBodyとの接触を解く

## CUDA版の実行モデル

- 衣服の位置・速度・固定状態・Seam・Edge・Quad・BendをSolverの寿命中GPUへ常駐
- BodyのBVHトポロジーを作成時に一度構築し、中間姿勢ごとにGPU上でAABBをRefit
- 最近傍Face探索とBody内外判定をCUDAで行い、PythonのBVH候補作成を通常経路から除外
- 頂点を共有しない拘束へGraph Coloringを行い、色単位のCUDAカーネルをCUDA Graphへ記録
- 1フレーム内のBody中間姿勢ではH2DをBody頂点更新だけに限定し、服の状態は整数フレーム終端までD2Hしない
- 整数フレームで1回だけ服の位置・速度・統計をBlenderへ戻し、Shape Keyキャッシュを作成

CUDA GraphはSolver Iterationsごとに初回だけ構築され、以降のBody stepでは再利用されます。CPU側ではBlender Dependency GraphによるBody評価、フレーム分割、整数フレームのShape Key保存を行います。

## 必要環境

- Blender 5.1以降（Blender 5.2 LTSで検証）
- Windows x64
- NVIDIA CUDA GPU（Turing以降。配布DLLはSM 75/80/86/89/90/100/120を収録）
- CUDA 12.9互換のNVIDIA Driver
- YohsaiでGravity完了済みの服
- フレーム範囲を通して頂点数と面数が変わらないBodyメッシュ

CUDA RuntimeとC/C++ RuntimeはDLLへ静的リンクするため、配布ZIPの利用者はCUDA ToolkitやVisual C++ Runtime DLLを別途配置する必要がありません。GPU Driverは必要です。

## インストール

1. `haori_cuda-1.1.0-windows-x64.zip`を用意します。
2. Blenderの`Edit > Preferences > Extensions`を開きます。
3. メニューから`Install from Disk`を選び、ZIPを指定します。
4. `Haori CUDA`を有効にします。

ZIPを展開する必要はありません。

## 使い方

1. Yohsaiで着付け、縫い合わせ、Gravityを完了します。
2. BodyのArmatureアニメーションを用意します。
3. 3D Viewportのサイドバーから`Haori`タブを開きます。
4. `Yohsai Clothes`と`Body`を指定します。
5. `Start Frame`、`End Frame`、Performanceを設定します。
6. `Simulate Animation`を実行します。
7. 結果を残す場合は`Bake HAORI Result`を実行します。

実行中は`Esc`でキャンセルできます。元のYohsai服は変更せず、結果を`<Yohsai Clothes名>_HAORI` Collectionへ作成します。

## 速度と品質

| Preset | Body Step | Contact Clearance | Iterations | 用途 |
| --- | ---: | ---: | ---: | --- |
| Fast | 2.0cm | 0.75cm | 10 | 素早い動きの確認 |
| Standard | 1.0cm | 1.0cm | 20 | 標準プレビュー |
| Quality | 0.5cm | 0.5cm | 30 | 接触と寸法収束を優先 |
| Custom | 任意 | 任意 | 任意 | 服とBodyに合わせた調整 |

Body Stepを大きくするとBody中間姿勢が減ります。Iterationsを増やすほど四角格子は保存寸法へ強く戻ります。Contact Clearanceを大きくすると貫通への余裕は増えますが、服がBodyから浮きます。衝突候補の内部探索距離は4cm固定です。

フレーム`f`から`f+1`で同じBody頂点が動く最大距離を`d`、Maximum Body Stepを`L`とすると、初期分割数は`max(1, ceil(d / L))`です。各中間姿勢を実際に評価し、隣接姿勢間の移動が`L`を超える場合は分割数を増やします。

## 出力とBake

- 各整数フレームを絶対Shape Key `HAORI_####`として保存
- Shape Keyの`eval_time`をScene Frameへ追従させるDriverを設定
- 計算範囲、最大Body分割数、最大移動量、Solver設定、`CUDA_RESIDENT` BackendをCollectionへ記録
- 同じ服で再実行すると以前の未Bake出力を置換
- Bake済み出力はDriverを通常のActionへ変換し、以後の再計算から保護

## 現在の制限

- CUDA GPUが必須で、CPU fallbackはありません。比較用CPU版は別worktree/branchに残します。
- 布同士の自己衝突は計算しません。
- Body接触はプレビュー安定性を優先し、Bodyの運動量を完全には布へ伝えません。
- Bodyはフレーム範囲内で同一トポロジーである必要があります。
- 整数フレームごとに全頂点をShape Keyへ保存するため、長い範囲ではCPUメモリと`.blend`容量が増えます。
- 1フレーム当たりのBody分割数は最大512です。
- CUDAとCPUでは並列化、単精度演算、拘束順序が異なり、見た目は近くても頂点単位では一致しません。

## ソースからのビルド

Visual Studio 2022、CMake 3.24以降、CUDA Toolkit 12.9を使用します。

```powershell
.\build_native.ps1 -Configuration Release
```

このスクリプトはDLLをビルドし、ネイティブテストを実行して`bin/haori_cosserat.dll`へインストールします。

Blender統合テスト:

```powershell
blender --background --factory-startup --python tests\blender_simulation_check.py
```

保存済みファイルの特定Collectionを検証する場合は`HAORI_TEST_CLOTHES`を設定します。

```powershell
$env:HAORI_TEST_CLOTHES = "CLOTHES.001"
blender --background scene.blend --python tests\blender_saved_simulation_check.py
```

詳細は[アーキテクチャ資料](docs/ARCHITECTURE.md)と
[CUDA開発・検証記録](docs/CUDA_RELEASE_NOTES.md)を参照してください。

## ライセンス

HaoriはGPL-3.0-or-laterです。静的リンクしたCUDA Runtimeについては[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。
