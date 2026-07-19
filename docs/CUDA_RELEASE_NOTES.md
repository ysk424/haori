# CUDA v1.1.0 開発・検証記録

この文書は、会話履歴に依存せずCUDA版の設計判断と現在位置を復元するための引継ぎ記録です。

## Branchとworktree

- CPU比較基準: `C:\Users\azoo\git\haori` / `main`
- CUDA開発版: `C:\Users\azoo\git\haori-accelerated` / `accelerated`
- CUDA実装コミット: `0b2ee6e` (`Add CUDA-resident cloth solver`)
- CPU側worktreeはCUDA実装中に変更していない

## 維持する物理的特徴

1. Yohsaiの服をEdge、Quad、軸方向Bendからなる四角格子として扱う。
2. Solver Iterationsを増やすほど、隣接長とQuad metricが保存寸法へ近づく。
3. CPU版との頂点単位の一致は要求しない。CUDAの単精度演算、Graph Coloring、並列実行順による差を許容する。

## Resident境界

- Solver作成時に服、速度、固定状態、Seam、Edge、Quad、Bend、Body Face、BVHをGPUへ置く。
- Body中間姿勢ごとに転送するのはBody頂点位置だけ。
- GPUでBVH refit、最近傍Face探索、Parity Ray内部判定、接触候補生成を行う。
- 1フレーム内の複数Body stepではCloth stateをD2Hしない。
- 整数フレーム終端で位置、速度、統計を1回だけD2Hし、Blender Shape Keyへ保存する。
- Solverごとのnon-blocking CUDA stream上で転送とCUDA Graphを順序付ける。

## 重要な安定化修正

初期実装ではpageable host memoryからLegacy Default Streamへ候補Faceをコピーし、その直後にnon-blocking Solver StreamのGraphが同じBufferを読む競合があった。数十プロセスに1回、`illegal memory access`として再現した。

修正後の規則:

- 初期化時のDefault Stream upload完了を、最初のSolver kernelより前に明示する。
- 実行中のH2DはSolver専用Streamへ統一する。
- 明示候補用Host BufferはSolverの寿命中保持し、非同期転送元を早期解放しない。
- Rollback stateとBody Face更新は、呼出元Host Bufferの寿命が切れる前に同期する。
- CUDA GraphはInstantiate後に`cudaGraphUpload`してから再利用する。
- Solver破棄時だけdevice boundaryを同期してGraphとallocationを安全に解放する。

この規則を崩すと、単発試験では通っても長時間・複数プロセス試験で再発する可能性がある。

## 確定した版

- Extension: `1.1.0`
- C API: `HSC_API_VERSION 10`
- CUDA Toolkit: `12.9`
- CUDA architectures: SM `75, 80, 86, 89, 90, 100, 120`
- Runtime: CUDA RuntimeとMSVC Runtimeを静的リンク
- 配布DLL依存: `KERNEL32.dll`のみ

## 完了した検証

- Native testを100個の独立プロセスで連続実行し、100/100成功
- 各Native testは32×32四角格子のSolver生成・破棄を64回繰り返す
- 単一プロセスでSolver生成・破棄10,000回の診断試験に成功
- Compute Sanitizer memcheck: `0 errors`, `0 bytes leaked`
- Compute Sanitizer racecheck: `0 hazards`
- Blender 5.2 LTS合成試験: Simulation、整数Shape Key、Bake、Cancelに成功
- Blender Extension公式builder/validatorに成功
- 隔離したUser Extension repositoryへZIPをinstallし、DLL load、register、unregisterに成功

主要コマンド:

```powershell
.\build_native.ps1 -Configuration Release
ctest --test-dir build -C Release --output-on-failure --repeat until-fail:100
compute-sanitizer --tool memcheck --leak-check full build\bin\Release\haori_cosserat_tests.exe
compute-sanitizer --tool racecheck build\bin\Release\haori_cosserat_tests.exe
blender --background --factory-startup --python tests\blender_simulation_check.py
blender --command extension build --source-dir . --output-filepath dist\haori_cuda-1.1.0-windows-x64.zip
blender --command extension validate dist\haori_cuda-1.1.0-windows-x64.zip
```

## 残っている実データ試験

ユーザーが開いている`Lumi-1-zozo-test2.blend`ではテスト対象を`CLOTHES.001`へ切り替えたが、その状態は未保存だった。ディスク上の保存済み`.blend`にはまだ`CLOTHES.001`が存在しないため、実ファイル試験だけ未実施である。元のBlender sessionには外部から変更を加えていない。

保存後に次を実行する:

```powershell
$env:HAORI_TEST_CLOTHES = "CLOTHES.001"
blender --background <saved-file.blend> --python tests\blender_saved_state_check.py
blender --background <saved-file.blend> --python tests\blender_saved_simulation_check.py
```

`blender_saved_simulation_check.py`は保存せず1フレーム区間を計算する。ただし同じSourceの既存未Bake HAORI outputを置換する仕様なので、重要な未Bake結果がある実ファイルではコピーを使う。
