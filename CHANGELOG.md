# Changelog

このプロジェクトの主な変更を記録します。

## [1.1.0] - 2026-07-20

### Added

- 衣服、拘束、Body、BVH、衝突候補、統計を保持するCUDA resident solver
- Solver Iterations別に再利用するCUDA Graph実行経路
- GPU上のBody BVH refit、最近傍Face探索、Parity Ray内部判定
- 32×32四角格子、反復収束、resident連続step、Solver lifecycleの回帰テスト

### Changed

- 1フレーム内はBody頂点だけをH2Dし、服の位置・速度は整数フレーム終端までD2Hしない構成へ変更
- 頂点共有のない拘束をGraph Coloringし、四角格子の性質を保ったままCUDA並列化
- CUDA RuntimeとMSVC RuntimeをDLLへ静的リンク
- Extension名、UI、出力metadataをCUDA版として更新

### Removed

- accelerated worktreeからCPU solver実装とMicrosoft OpenMP Runtimeの配布を削除

## [1.0.0] - 2026-07-19

### Changed

- StandardのMaximum Body StepとContact Clearanceを両方`1.0 cm`へ統一
- PythonとネイティブソルバーのContact Clearance初期値を`1.0 cm`へ統一
- 初期リリースとして機能と配布形式を確定

## [0.2.1] - 2026-07-19

### Fixed

- Blender Extension登録中の`_RestrictData`からSceneへアクセスして有効化に失敗する問題
- Sceneの初期範囲とYohsai入力検出を登録完了後へ安全に遅延

## [0.2.0] - 2026-07-19

### Added

- Fast、Standard、Quality、CustomのPerformance Preset
- Contact ClearanceとSolver IterationsのN-panel設定
- フレーム当たりContact Passの表示とメタデータ
- 完成Shape KeyキャッシュのDriverをActionへ変換し、再計算から切り離すBake操作
- Yohsai下流におけるBody追従、Blender XPBD、HAORI、ZOZOの選択指針

## [0.1.0] - 2026-07-19

### Added

- 保存済みYohsai Gravity状態の再起動後読み取り
- Armature変形Bodyのサブフレーム評価
- ワールド座標の最大Body頂点移動量による自動分割
- Body姿勢ごとのNormal Gravity相当計算
- ネイティブソルバーのBody姿勢差し替えAPI
- 絶対Shape KeyとDriverによる整数フレームキャッシュ
- 最小構成のHaori N-panel
- 合成データと実保存Yohsaiデータを使ったBlender統合テスト

### Limitations

- Windows x64のみ
- 布の自己衝突なし
- Bodyトポロジーはフレーム範囲内で固定
- 再実行開始時に以前のHAORI出力を置換
