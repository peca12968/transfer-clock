# Return Radius｜共享單車還車站搜尋

兩人一起還車，最近的渡口站只剩一個空位。若兩人希望在 180 公尺內停在同站，哪個站還能收兩輛車？這個 C# 命令列工具為一份站點快照建立空間索引，按所需空位、距離上限和回傳筆數找出候選站。

## 跑一次河濱廣場範例

需要 .NET 10 SDK。從專案根目錄執行以下指令。

```sh
dotnet run -c Release -- examples/riverside.json
```

| 查詢 | 回傳站點，依距離排列 |
| --- | --- |
| 一人還車，取最近兩站 | 渡口 `ferry` 約 77.84 m，市場 `market` 約 155.67 m |
| 兩人同站還車，限 180 m | 市場 `market` 約 155.67 m，有 3 個空位 |
| 四人同站還車 | 空清單，整份快照最多只有 3 個空位在同一站 |

廣場站最近，但空位為零。公園站有兩個空位，距離約 201.47 m，超過第二個查詢的上限。站點座標與空位皆是合成資料。[可修改的範例](examples/riverside.json)

## 輸入與輸出

### 四張圖磚能放進哪幾個還車站？

查詢可加上 `"offlineMap":{"maxZoom":19,"tileBudget":4}`。工具先依空位、距離與筆數找出站點，再將查詢位置及入選站點投影到 XYZ 網格，找出預算內能完整覆蓋它們的最高整數縮放層級。預算單位是矩形內的圖磚數。沒有符合條件的站點時，仍可取得查詢位置所在的一張圖磚。

執行 `dotnet bin/Release/net10.0/ReturnRadius.dll examples/dateline.json`。這次找到約 111 m 和 802 m 的兩座還車站，回傳第 16 層的 2 × 2 區塊。X 欄從 65535 接到 0，Y 列是 32767 與 32768。經度是環狀座標，取已占用欄位間最大的空隙並從另一側覆蓋，才能把換日線兩側相鄰站點放在一起。緯度投影截在 Web Mercator 的上下限，站點距離仍以原始球面座標計算。[範例輸入](examples/dateline.json) · [投影與圖磚網格](https://learn.microsoft.com/en-us/azure/azure-maps/zoom-levels-and-tile-grid)

回傳的 `map` 包含 `zoom`、起始欄列、寬高與總張數。從 `firstColumn` 向東列舉時以 `2^zoom` 取模。80 組測試用另一種演算法枚舉所有可能起始欄及縮放層級，另測南北極與 ±180°。下列跨專案示範以 Tile Satchel 從 58,162 B 的合成封存檔發出 6 次區段請求，傳回 3,594 B 本文，並逐張核對 ZIP 裡的四份 PNG。

```sh
dotnet build -c Release
git clone https://github.com/peca12968/transfer-clock.git ../tile-satchel
git -C ../tile-satchel checkout 4d272a2ac74b37c777794bd418a8c9a3cb1ad9bc
python -m pip install -r ../tile-satchel/requirements.txt
python offline_demo.py ../tile-satchel
```

### 站點與查詢欄位

輸入 JSON 包含 `stations` 與 `queries` 兩個陣列。一個程序建一次索引，依序回答整批查詢。

- 站點提供唯一的 `id`、`latitude`、`longitude` 和非負整數 `freeDocks`。經緯度單位為度，緯度範圍為 −90 到 90，經度為 −180 到 180。
- 查詢提供經緯度、至少需要的空位數 `needed` 與最多回傳幾站 `limit`，兩者皆為正整數。可選的 `maxMeters` 為含邊界的距離上限，省略表示不限。上限為零時只取同一位置。
- 每個結果包含 `matches` 與走訪節點數 `visited`。候選站回傳 ID、快照中的空位數和球面距離。算出的距離相同時依 ID 的 ordinal 順序排列。無可用站會得到空陣列。輸入無效時程序傳回非零結束碼，錯誤寫至 stderr。

距離使用半徑 6,371,000 m 的球面模型。實際騎乘路線、地形與抵達時的空位需另行取得，查詢本身不保留車位。

## 搜尋與驗證

將經緯度投影到三維單位球，建立平衡 k-d 樹。每個子樹記錄其中最大的可用空位數，足以在走訪前排除整個容量不足的區域。搜尋先走較近一側，再用切割平面的距離下界判斷另一側是否可能落在半徑內或擠進前幾名。候選距離由向量外積和內積求圓心角，保留近乎對跖位置的微小差異。[索引實作](StationIndex.cs)

測試需要 Python 3.12 以上，只使用標準函式庫。先建置，再執行整合測試。

```sh
dotnet build -c Release
python test_search.py
```

固定種子產生 4,096 個站點和 64 個查詢。Python 逐站篩選容量，以 haversine 公式獨立排序，再逐項核對 C# 的站點 ID、空位與距離。這組資料完整掃描共檢查 262,144 站次，樹索引合計走訪 2,681 個節點。這是走訪量比較，未計入建樹成本或量測執行時間。

換日線案例從東經 179.99 度尋找至少有兩個空位的站，西經 179.96 度的站約 5.56 km，比同側東經 179.7 度的站更近。其餘案例包含極點同址排序、距離上限的精確邊界，以及接近對跖點、相差約 1.1 公分的兩個合成站點。[跨語言整合測試](test_search.py) · [雙平台 CI](../../.github/workflows/system.yml)

本作品使用 AI 輔助開發。
