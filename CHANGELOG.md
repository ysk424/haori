# Changelog

このプロジェクトの主な変更を記録します。

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
