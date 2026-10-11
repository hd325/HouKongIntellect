# 濠江小智 · HouKongIntellect

面向校園學習場景的 AI 問答與資料管理系統。使用 FastAPI 和靜態網頁提供對話、文件檢索、原文溯源、上傳整理及教師審核，支援在自己的電腦上部署。

**公開倉庫提供執行所需的程式、前端資源、配置範本與測試。** 帳號、密鑰、上傳資料、對話記錄、向量庫、影片及本機環境需由部署者自行建立，不隨原始碼提供。

[快速開始](#快速開始) · [功能與流程](#功能與流程) · [配置](#配置) · [測試](#測試) · [常見問題](#常見問題)

## 功能與流程

| 功能 | 說明 |
| --- | --- |
| 對話問答 | 支援一般對話、資料檢索及引用原文；保存、切換、重命名和刪除對話。生成回答需要配置模型服務。 |
| 資料管理 | 上傳文件、填寫學科與學段、選擇可見範圍，並提供列表、預覽、下載和刪除。單次上傳上限為 50 MB。 |
| 上傳整理 | 先提取文字，再由 AI 建議分類；使用者可修改所有欄位，確認後才儲存。備註最多 5 個字，學段依月份預填，重複資料僅提示。 |
| 審核發布 | 學生的班級、年級或全校共享資料須經教師審核；個人資料和教師資料可直接發布。待審原件獨立保存，發布前不進入正式檢索。 |
| 文件溯源 | 依資料權限查看原文、引用段落及來源頁；PDF 可在瀏覽器中預覽。 |
| 練習出題 | 依所選文件生成選擇、填空或簡答題，附答案和來源。出題素材限定為文件前 3,000 字，題目需人工核對。 |
| OCR 與搜尋 | 可選接入騰訊雲 OCR 和 Serper 搜尋；未設定密鑰時不呼叫相應服務。 |
| 使用資料 | 顯示知識庫規模及已累積的檢索統計；空資料庫不提供預先填入的展示數據。 |

上傳流程：**選擇文件 → 提取文字與 AI 建議 → 人工修改 → 確認上傳 → 按共享範圍直接發布或送審**。

沒有 API 密鑰也能啟動、建立本機帳號、管理文件並使用離線檢索。AI 回答、AI 分類和出題需要模型密鑰，掃描頁識別需要 OCR 密鑰；系統會提示配置缺失或功能不可用，不會用示範答案代替實際結果。

## 快速開始

### 1. 取得原始碼

安裝 **Python 3.12**。使用 Git 下載，或在 GitHub 點選 **Code → Download ZIP** 後解壓。

```bash
git clone https://github.com/hd325/HouKongIntellect-public.git
cd HouKongIntellect-public
```

以下命令均在解壓後的專案根目錄執行。

### 2. 建立環境並安裝

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux / macOS：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

核心安裝不需要下載大型模型或安裝 PyTorch。保留 `.env` 中的空密鑰可先體驗文件管理；要使用 AI 功能，再按下方配置說明填入自己的密鑰。

### 3. 建立自己的帳號

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe scripts/init_accounts.py
```

Linux / macOS（已啟用虛擬環境）：

```bash
python scripts/init_accounts.py
```

依提示輸入帳號、角色（`student` 或 `teacher`）及至少 12 個字元的密碼；密碼輸入不會顯示。可用 `--name`、`--grade` 和 `--class-name` 設定顯示名稱、年級及班級，例如：

```bash
python scripts/init_accounts.py --role teacher --name "本機教師" --grade "高二" --class-name "高二1班"
```

Windows 未啟用虛擬環境時，同樣將 `python` 改為 `.\.venv\Scripts\python.exe`。要建立更多帳號，可再次執行；`--admin` 明確授予刪除所有文件的權限，`--replace` 用於重設已有帳號。

公開版沒有預設帳號或通用密碼；權限由本機帳號設定決定。帳號資料保存在 `data/accounts.json`（或指定的資料目錄），密碼以加鹽 PBKDF2 雜湊保存，請勿將帳號資料加入 Git。

### 4. 啟動並登入

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Linux / macOS（已啟用虛擬環境）：

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打開 **[http://127.0.0.1:8000](http://127.0.0.1:8000)**，使用剛建立的帳號登入。

亦可用同一個虛擬環境執行 `python main.py`；此啟動方式讀取 `.env` 中的 `HOUKONG_HOST`、`HOUKONG_PORT` 和 `HOUKONG_RELOAD`。Uvicorn 命令列則使用你在命令中指定的地址和連接埠。

## 配置

複製 [`.env.example`](.env.example) 為 `.env` 後編輯。程式只讀取本專案根目錄的 `.env`；系統已設定的環境變數優先。`.env` 已排除在 Git 追蹤之外。

| 變數 | 預設值／用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 留空；配置後啟用模型回答、AI 分類與出題。 |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/chat/completions`；可配置相容的聊天服務。 |
| `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_MODEL` | `deepseek-chat` / `deepseek-reasoner`；須由所用服務支援。 |
| `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | 留空；兩項均設定後可使用騰訊雲 OCR。 |
| `TENCENT_OCR_REGION` | `ap-guangzhou`。 |
| `SERPER_API_KEY` | 留空；設定後啟用聯網搜尋。 |
| `EMBEDDING_BACKEND` | `tfidf`；離線雜湊詞袋檢索，不需外部密鑰或模型。可選 `local`、`tencent`、`auto`。 |
| `LOCAL_EMBEDDING_MODEL` | `shibing624/text2vec-base-chinese`；亦可設為已下載的模型目錄。 |
| `HOUKONG_DATA_DIR` | `data`；保存帳號、對話、登入狀態、上傳和待審原件。相對路徑以專案根目錄為準。 |
| `HOUKONG_CHROMA_DIR` | 留空時自動選擇獨立向量庫位置，詳見下文。 |
| `HOUKONG_HOST` / `HOUKONG_PORT` | `127.0.0.1` / `8000`；用於 `python main.py`。 |
| `HOUKONG_RELOAD` | `false`；本機開發時可改為 `true`。 |

生成式 AI、雲端 OCR 和搜尋會向所配置的第三方服務傳送相關文字、圖片或查詢。使用自己的密鑰及服務額度，按資料的使用權限決定是否啟用。

### 可選：本地語義模型

預設 `tfidf` 可離線使用，但語義理解能力有限。需要本地語義嵌入時，在同一個虛擬環境中安裝可選依賴並預先下載模型：

```bash
python -m pip install -r requirements-local.txt
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='shibing624/text2vec-base-chinese')"
```

Windows PowerShell 未啟用虛擬環境時，將上述 `python` 改成 `.\.venv\Scripts\python.exe`。

然後把 `.env` 中的 `EMBEDDING_BACKEND` 改為 `local` 並重啟。模型在執行期間只讀取本機快取，缺少模型時會回退至 `tfidf`。`auto` 會按配置嘗試雲端、本地和離線後端；雲端嵌入使用騰訊雲憑證，另需開通相應服務。

改變嵌入後端後，新舊向量集合分開保存。已有資料需按新後端重建索引；重建腳本會處理本機資料，請先備份，詳見 `python scripts/reindex_all.py --help`。

### 資料與備份

應用在首次執行時建立資料目錄。一般情況下，向量庫位於 `data/chroma_db`；Windows 若資料路徑包含中文等非 ASCII 字元，預設向量庫會改放在 `%LOCALAPPDATA%/HouKongIntellectPublic/<資料目錄識別碼>/chroma_db`，避免向量庫後端的路徑相容問題。可使用 `HOUKONG_CHROMA_DIR` 指定自己的位置。

備份時停止服務，保存 `HOUKONG_DATA_DIR` 和實際向量庫目錄。請保持一個服務程序；JSON 資料儲存方式不適合多個 Uvicorn worker 同時修改同一資料目錄。

## 專案結構

```text
.
├── main.py                  # FastAPI、登入、對話、文件與審核 API
├── index.html               # 前端頁面
├── config/settings.py       # 環境變數與路徑設定
├── services/                # OCR、嵌入、檢索、審核、出題與統計
├── utils/                   # 圖片處理等共用工具
├── scripts/                 # 帳號初始化與索引重建工具
├── tests/                   # 隔離回歸測試及瀏覽器測試資料
├── fonts/                   # 頁面圖示字型
├── requirements.txt         # 核心執行依賴
├── requirements-local.txt   # 可選本地模型依賴
├── requirements-dev.txt     # 測試與開發依賴
├── .env.example             # 無密鑰的配置範本
└── data/                    # 執行時建立，不納入 Git
```

前端用到的 Tailwind、Marked、PDF.js、Font Awesome 和圖示字型隨倉庫提供。Inter 字型由 Google Fonts 可選載入，無法連線時使用系統字型；頁面不需要前端建置步驟。

## 測試

先在虛擬環境安裝開發依賴：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests
```

Windows PowerShell 未啟用虛擬環境時，使用 `.\.venv\Scripts\python.exe` 取代 `python`。測試採用合成帳號、臨時文件及模擬模型；更多覆蓋範圍和瀏覽器檢查方法見 [tests/README.md](tests/README.md)。

## 常見問題

**登入頁沒有可用帳號。** 執行 `scripts/init_accounts.py` 建立帳號，再重啟服務。請在同一份 `.env` 和資料目錄下初始化及啟動。

**AI 回答、分類或出題不可用。** 檢查 `.env` 的模型密鑰、服務地址和模型名稱，並確認服務額度及網路連線。未配置時，文件管理仍可使用。

**掃描 PDF 或圖片沒有文字。** 原生文字 PDF 可直接提取；掃描頁及圖片需要配置 OCR。識別品質受解析度和頁面內容影響，請核對原文。

**舊版 Office 文件無法轉換。** 建議先另存為 `.docx`、`.xlsx` 或 `.pptx`。處理部分舊格式需要額外安裝 LibreOffice，並讓 `soffice` 可從系統路徑執行。

**Windows 中文路徑下向量庫重啟失敗。** 保留預設自動位置，或設定 `HOUKONG_CHROMA_DIR` 為獨立的 ASCII 路徑，重啟後按需要重新索引。

**需要讓其他裝置連線。** 本機預設只接受本機連線。部署前應配置 HTTPS、反向代理及存取控制，參閱 [SECURITY.md](SECURITY.md)；使用自己的資料目錄與帳號，避免共享開發環境。

## 第三方資源

第三方程式和字型的來源、版權聲明及授權文件見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。第三方授權適用於各自資源，不代表整個專案採用同一授權。
