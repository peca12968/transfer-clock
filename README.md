# Transfer Clock｜夜班轉乘查詢

晚班結束後，23:49 從 Studio 出發，直達車隔日 00:31 到 Depot。若接受一次換車，留兩分鐘在 Market 轉乘可以提早 19 分鐘抵達。把轉乘時間加長到四分鐘，工具會列出另一班接駁車，以及仍然可搭的直達車。

## 重現查詢

使用 Python 3.12 標準函式庫，在專案根目錄執行。

```sh
python router.py examples/night.csv Studio Depot 23:49:00 --change-seconds 120
python router.py examples/night.csv Studio Depot 23:49:00 --change-seconds 240
python -m unittest -v
```

| 在 Market 換車所需時間 | 直達到站 | 換一次車的最早到站 | 換乘班次 |
| --- | --- | --- | --- |
| 120 秒 | 24:31 | 24:12 | 24:05 的 express |
| 240 秒 | 24:31 | 24:23 | 24:09 的 shuttle |

班表還有一班 24:10 到站的 staff，但該班在 Market 禁止上車。輸出 JSON 保留每段的班次、上下車站、停靠序號與時間。無可行行程時回傳空陣列。可用 `--max-transfers 0` 單獨查詢直達行程。[合成班表](examples/night.csv)

## 已知到站期限，最晚幾點出發？

隔日 00:31 前要到 Depot。加上 `--arrive-by` 後，輸入時間表示最晚到站時間，工具會列出直達，以及多換車但能更晚出發的行程。

```sh
python router.py examples/night.csv Studio Depot 24:31:00 --arrive-by
python router.py examples/night.csv Studio Depot 24:22:59 --arrive-by
```

| 到站期限 | 換車次數 | 最晚從 Studio 出發 | 實際到站 | 班次 |
| --- | ---: | --- | --- | --- |
| 24:31:00 | 0 | 23:50 | 24:31 | local |
| 24:31:00 | 1 | 23:57 | 24:23 | feeder → shuttle |
| 24:22:59 | 1 | 23:50 | 24:12 | local → express |

第二條路在 Market 的到站時間是 24:07，能趕上 24:09 的接駁車，剛好符合兩分鐘的轉乘要求。若到站期限比接駁車的 24:23 提早一秒，就必須提早七分鐘從 Studio 搭車。結果包含實際出發和抵達時間。同一換車次數若有多條路同樣晚出發，回傳其中一條可行路線。

## 把臨時延誤與停駛納入轉乘查詢

```sh
python router.py examples/night.csv Studio Depot 23:49:00 --service-updates examples/night-disruption.json
python router.py examples/night.csv Studio Depot 24:15:00 --arrive-by --service-updates examples/night-disruption.json
```

Market 的慢車延誤 3 分鐘後在 24:06 抵達，快車也延誤至 24:08 發車，仍保有整整 2 分鐘的轉乘時間，24:15 到 Depot。接駁車停駛時，若轉乘至少需 121 秒，就只剩 24:33 到的直達慢車。查詢 24:15 到站的最晚出發時間會得到 23:50，若到站期限提前一秒則無車可達。

異動 JSON 以 `trip_id` 對應同一服務日的班表。一班車可標記 `cancelled: true`，或以非負整數 `delay_seconds` 延後整班。也可依停靠序號給定 `stop_delays`，自該站開始以新延誤秒數調整抵達與出發，直到下一個有指定的停靠站。延誤可減少，但每站抵達必須仍晚於或等於前站出發。停駛班次不參與查詢，站名仍可查詢。相同參數可用在 `dock_trip.py` 的單車接駁正查及到站期限查詢。[範例異動資料](examples/night-disruption.json) · [作品站與端對端測試共用的四組查詢結果](examples/transfer-demo.json)

## 依月台或站點設定轉乘時間

```sh
python router.py examples/platform-night.csv Studio Depot 23:49:00 --transfer-rules examples/platform-transfer.json
python router.py examples/platform-night.csv Studio Depot 24:12:00 --arrive-by --transfer-rules examples/platform-transfer.json
```

市場站的慢車在 `Market` 下車，快車從 `MarketEast` 開出。單向步行 120 秒時，正查能趕上 24:05 快車，且沒有多餘秒數。若需要 121 秒，最快改搭 24:09 接駁車，24:23 抵達。以 `null` 關閉這條轉乘連接後，可留在慢車上直達，24:31 抵達。反查 24:23 的到站期限時，步行多一秒會讓最晚出發時間從 23:57 提早到 23:50。

規則檔每筆指定 `from_stop`、`to_stop` 和 `minimum_seconds`，秒數涵蓋下車到下一段可上車的完整路徑。兩個方向分別設定。同站預設使用 `--change-seconds`，也可各站覆寫。跨站換車需有對應規則，第一段上車與最後一段下車各自使用查詢指定站點。啟用規則時，每條行程另列出 `connections`，包含下車、步行完成、下一班發車時間與剩餘秒數。單車接駁及服務延誤可和規則一起使用。[規則範例](examples/platform-transfer.json) · [三種實際輸出](examples/platform-demo.json) · [正反查及單向驗證](test_connections.py)

## 查詢已確認的無階梯行程

```sh
python router.py examples/platform-night.csv Studio Depot 23:49:00 --step-free examples/step-free-profile.json
python router.py examples/platform-night.csv Studio Depot 24:23:00 --arrive-by --step-free examples/step-free-profile.json
python router.py examples/platform-night.csv Studio Depot 23:49:00 --step-free examples/step-free-profile.json --transfer-rules examples/platform-closure.json
```

`--step-free` 讀取同一服務日的確認清單。`stops` 列出可無階梯上下車的站點，`trips` 列出已確認的車班，`transfers` 列出定向無階梯轉乘所需秒數。跨月台與同站換車都要有確認路徑，轉乘時間取自明列規則。續乘同一班車可以經過未確認的中途站。原班表的禁止上下車、延誤與停駛仍適用。

同時傳入 `--transfer-rules` 時，關閉的連接會從確認清單移除，兩份檔案各有時間時採較長的一個。模式使用呼叫端提供的確認資料，範例使用合成班表與電梯連接狀態。[確認清單](examples/step-free-profile.json) · [連接關閉範例](examples/platform-closure.json)

| 條件 | 正查最快到站 | 24:23 前最晚出發 |
| --- | --- | --- |
| 一般路由 | 24:12，慢車 → 快車 | 23:57，接駁車轉 shuttle |
| 確認無階梯路由 | 24:23，慢車 → shuttle | 23:57，接駁車轉 shuttle |
| 確認無階梯路由，轉乘連接關閉 | 24:31，直達慢車 | 無符合期限的行程 |

快車未列入確認清單。無階梯模式保留另一班已確認的 shuttle。可用 `python -m unittest test_connections` 從實際命令列重跑這兩個方向。[正反查實際結果](examples/step-free-demo.json) · [驗證](test_connections.py)

## 用步行時間比較門到門的行程

起點可以走到 Studio 或 Avenue，終點可從 Depot 或 DepotEast 抵達。給定各方向的步行時間後，查詢可同時比較這些站點。

```sh
python area_trip.py examples/area-night.csv examples/area-walks.json 23:49:00 --transfer-rules examples/platform-transfer.json
python area_trip.py examples/area-night.csv examples/area-walks.json 24:15:00 --transfer-rules examples/platform-transfer.json --arrive-by
python area_trip.py examples/area-night.csv examples/area-walks.json 24:15:00 --transfer-rules examples/platform-transfer.json --arrive-by --step-free examples/area-step-free.json
```

| 條件 | 從起點出發 | 下車站與時間 | 抵達終點 |
| --- | --- | --- | --- |
| 直達 | 23:49 | DepotEast 24:10 | 24:15 |
| 最快，轉乘一次 | 23:49 | Depot 24:12 | 24:14 |
| 最晚出發，24:14 前抵達 | 23:49 | Depot 24:12 | 24:14 |
| 最晚出發，24:15 前抵達 | 23:50 | DepotEast 24:10 | 24:15 |
| 24:15 前抵達，含步行的無階梯路線 | 23:49 | Depot 24:12 | 24:14 |

步行設定的 `origin` 與 `destination` 各列出 `stop` 和 `seconds`。前者是從起點走到站點，後者是從站點走至終點。行程至少包含一段搭乘。正查以步行後抵達終點的時間比較，反查以從起點開始步行的最晚時間比較。每輪共用班表掃描，並回傳有改善的轉乘選項。`access`、`egress` 與 `connections` 各自記錄步行與轉乘秒數。

延誤、停駛和定向轉乘規則沿用原路由。使用 `--step-free` 時，每段首尾步行也須在設定檔明列 `step_free: true`，對應停靠點及車班仍須符合確認清單。範例未確認 Avenue 的起點步行，所以無階梯反查會改選 Studio。測試用 120 份合成班表窮舉正查與反查，並核對服務異動後的轉乘餘裕。[步行設定](examples/area-walks.json) · [四組實際輸出](examples/area-demo.json) · [端對端測試](test_area_trip.py)

## 先還共享單車，再搭夜班車

從同一個騎車起點出發，最近的渡口站距離約 78 公尺，市場站約 156 公尺。23:54 從起點出發，市場站雖然較遠，還車後走到月台剛好趕上 00:05 快車，比在渡口站還車提早 11 分鐘抵達。使用 Python 3.12 和 .NET 10 SDK，從本專案目錄執行以下指令。

```sh
git worktree add --detach ../return-radius-verified a3d78f802b898eb6751a75ea5bb15a7b629648ff
dotnet build ../return-radius-verified/ReturnRadius.csproj -c Release
python dock_trip.py examples/night.csv examples/dock-night.json Depot 23:54:00 --max-meters 180
python dock_trip.py examples/night.csv examples/dock-night.json Depot 24:23:00 --max-meters 180 --needed 2 --arrive-by
python dock_checks.py
```

| 條件 | 還車站與行程 | 從起點出發 | 到達 Depot |
| --- | --- | --- | --- |
| 一人，23:54 出發 | 市場站，搭 express | 23:54:00 | 24:12:00 |
| 同樣條件，選最近的渡口站 | 渡口站，搭 feeder 再轉 shuttle | 23:54:00 | 24:23:00 |
| 改為 23:54:01 出發 | 市場站，改搭 shuttle，渡口站已無可行班次 | 23:54:01 | 24:23:00 |
| 兩人同站還車，24:23 前抵達 | 市場站，搭 shuttle | 最晚 23:58:00 | 24:23:00 |

Return Radius 先以球面距離及空位查出半徑內的全部合格站點。每個站在輸入快照指定對應的上車站、從起點實際騎行的秒數 `rideSeconds`，以及停車、還車和走到上車站的合計秒數 `returnAndWalkSeconds`。空位使用快照當下的讀數，騎行與步行時間由呼叫端提供。[站點快照](examples/dock-night.json) · [Return Radius 固定版本](https://github.com/peca12968/transfer-clock/tree/a3d78f802b898eb6751a75ea5bb15a7b629648ff)

正查從騎車出發時間加上兩段耗時，再向班表查詢。反查從最晚可上車時間扣除兩段耗時，取得最晚能從騎車起點出發的時間。輸出列出起點出發、到還車站、抵達上車站、最終到站時間與搭乘班次。每個還車站保留原路由器依轉乘次數提供的行程。跨站排序優先採最早抵達或最晚出發，同分依轉乘次數、距離與站點 ID 排序。若還車站旁就是目的站，可在還車步行後直接抵達。

`eligibleDocks` 表示通過容量與半徑篩選的站數。晚到而錯過末班車時，此數字可能大於零但 `journeys` 為空。可用 `--radius-dll` 指定已建置的 ReturnRadius.dll 路徑。整合測試另用 120 份合成班表，對兩種查詢方向逐一窮舉比對候選順序。[查詢實作](dock_trip.py) · [跨專案測試](dock_checks.py)

## 使用自己的時刻表

輸入為已篩出單一服務日班次的 CSV，使用 GTFS 的 `trip_id`、`stop_id`、`stop_sequence`、`arrival_time` 與 `departure_time` 欄位。時間需完整，可使用 24 點以上的小時數表示午夜後的班次。可選欄位 `pickup_type` 與 `drop_off_type` 接受空白、0 或 1，分別表示一般服務或禁止上下車。每一列以站點 ID 表示實際上下車位置。[GTFS Schedule 參考規格](https://gtfs.org/documentation/schedule/reference/#stop_timestxt)

本工具以單一服務日的固定時刻表為基礎，可另載入班次異動及定向轉乘規則。班表匯入時需先處理營運日曆和預約班次，轉乘站對及其步行時間由呼叫端提供。

## 如何驗證選到的路

每一輪多搭一班車。演算法先依上一輪可抵達站點及其轉乘連接，算出各站最早可上車時間，再沿每班車的停靠順序掃描。留在車上經過中途站時可直接續乘。只有比更少換車的行程還早到的結果才列入輸出。已排序的班表共 N 次停靠，最多換 K 次車的查詢最多掃描 (K+1)N 次停靠，並額外掃描每輪可用的轉乘連接。[路線實作](router.py)

測試另用窮舉逐一組合可行的上下車站，對照 300 份固定種子的合成班表，核對每種換車次數的到站結果，也從原始班表逐段檢查輸出的行程。另有同站重複停靠、同秒轉乘、跨午夜、禁止上車及班表錯誤案例。[測試](test_router.py)

反查時，工具將每班車的停靠順序與時間軸反轉，對調到站、離站與上下車權限，也反轉定向轉乘連接。班表掃描完成後，再將路段還原成旅客實際乘車的順序。除原有 300 份班表外，另以 100 份含跨站、停用連接及同站覆寫的班表向前窮舉，核對正查和反查的結果。[定向規則測試](test_connections.py)

本作品使用 AI 輔助開發。所有站名與班次均為合成示範資料。

[同庫模組與整合測試](modules/README.md)
