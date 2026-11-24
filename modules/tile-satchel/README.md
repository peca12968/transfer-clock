# Tile Satchel｜取出四個座標的地圖，需要下載多少資料？

在 138,231 B 的示範 PMTiles 封存檔中指定四個 XYZ 座標，其中兩個共用同一份 PNG，一個指向另一張 PNG，另一個缺圖。工具送出四次 HTTP Range 請求，依序讀取檔頭、根目錄和兩份圖磚內容，共傳回 1,873 B 回應本文。產出的 ZIP 含三個可依座標取用的圖磚，以及記錄缺圖與 SHA-256 的清單。此數字來自固定的本機合成樣本，不含 HTTP 標頭。

## 重現

使用 Python 3.12。示範會建立合成 PNG、用 PMTiles writer 打包，再啟動本機 HTTP 服務；測試另比對含多個葉目錄的封存檔與官方套件的本機讀取結果。

```sh
python -m pip install -r requirements.txt
python demo.py
python -m unittest -v
```

## 從自己的圖資擷取

以封存檔的直接 HTTP(S) 網址取代下方範例。來源須支援區段讀取、強 ETag 及未經 HTTP 內容編碼的回應。工具支援 PMTiles v3 的 gzip 目錄，並原樣保存圖磚本身的編碼。ZIP 內的 `manifest.json` 會記錄內容格式、壓縮方式、各座標摘要、缺圖清單與實際讀取量。

```sh
python satchel.py https://maps.example.org/region.pmtiles selection.zip 1/0/0 1/0/1 1/1/1 1/1/0
```

首個回應決定檔案長度與 ETag。後續每次請求帶上 `If-Match`，並逐一核對 206 回應的區段、總長、ETag 與本文長度。測試在取得第一張 PNG 後切換版本，分別重現伺服器正確回覆 412，以及忽略條件而誤回新版 206。兩者都會中止這次擷取，保留既有 ZIP。重新執行可取得新版資料。相同實體位址的圖磚只傳輸一次，重複輸入同一個座標也只寫一份。

示範與程式以 AI 輔助開發。[PMTiles v3 規格](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md) · [實際 HTTP 測試](test_satchel.py)
