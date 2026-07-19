# Changelog

このプロジェクトの主な変更を記録します。

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
