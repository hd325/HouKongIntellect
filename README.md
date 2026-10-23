# HouKongIntellect · 濠江小智

校園學習助手：對話問答、帶來源的資料檢索、教師審核、化學教師模式，以及可選裝的本機文件編輯工具。

[下載 ZIP](https://github.com/hd325/HouKongIntellect/archive/refs/heads/main.zip) · [安全說明](SECURITY.md) · [第三方授權](THIRD_PARTY_NOTICES.md)

## 功能

| 功能 | 安裝與配置 |
|---|---|
| 帳號、對話、檔案管理、教師審核 | 基礎安裝，建立自己的本機帳號 |
| TF-IDF 檢索、原生文字 PDF／Office 提取 | 基礎安裝，無需模型下載或 API 密鑰 |
| AI 問答、題目生成 | 自己的模型接口與憑證 |
| 網頁搜尋與閱讀 | Serper，以及模型供應商的 Fetch 權限 |
| 圖片／掃描 PDF OCR | 自己的華為雲 OCR 憑證 |
| 化學教師模式 | 通用規則包含；私有課堂證據由部署者自行取得授權 |
| Word／Excel／PowerPoint／PDF 建立和編輯 | 選裝文件工具並開啟功能 |
| Office 轉 PDF 預覽 | 另裝 LibreOffice；缺少時仍可下載原檔 |
| 本機神經網絡嵌入 | 選裝模型依賴並自行下載模型 |

公開版不包含真實師生帳號、密碼、密鑰、錄音、轉寫、上傳資料、聊天記錄或向量庫。外部服務費用由部署者自行承擔。

## 快速開始

建議 **Python 3.12**。基礎安裝不需要 Node.js、PyTorch 或本機大語言模型。

下載 ZIP 並解壓，或使用 Git：

~~~sh
git clone https://github.com/hd325/HouKongIntellect.git
cd HouKongIntellect
~~~

Windows PowerShell：

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts/init_accounts.py --account my-admin --name "管理員" --role teacher --admin
.\.venv\Scripts\python.exe main.py
~~~

Linux／macOS：

~~~sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python scripts/init_accounts.py --account my-admin --name "管理員" --role teacher --admin
.venv/bin/python main.py
~~~

帳號工具會隱藏密碼輸入，至少 12 個字元，使用隨機鹽 PBKDF2 雜湊。開啟 **http://127.0.0.1:8000**，使用剛建立的帳號登入；沒有預設帳號或通用密碼。

建立學生帳號：

~~~sh
python scripts/init_accounts.py --account student-01 --name "示例學生" --grade "高二" --class-name "示例班"
~~~

以上命令請用虛擬環境中的 Python 執行。新增或修改帳號後重啟服務。教師以 --role teacher 指定，管理權限另外指定 --admin；帳號字首不決定權限。

## 服務配置

只編輯自己的 .env，勿提交它。程序環境變量優先，僅讀取本專案根目錄的 .env。

- DEEPSEEK_API_KEY、DEEPSEEK_API_URL：模型憑證與聊天接口。
- DEEPSEEK_MODEL、DEEPSEEK_REASONING_MODEL：普通／思考模式的別名，依自己的供應商權限設定；不保證別名對應某個固定模型修訂版本。
- HUAWEI_AK、HUAWEI_SK：華為雲 OCR。區域為 cn-north-4，必要時設定 HUAWEI_PROJECT_ID。
- SERPER_API_KEY：網頁搜尋。閱讀網頁另需 Fetch 權限；失敗會明確提示，不會宣稱已閱讀。
- EMBEDDING_BACKEND=tfidf：預設離線模式；其他後端需另外配置。
- HOUKONG_HOST／HOUKONG_PORT：預設 127.0.0.1:8000；重新載入預設關閉。

缺少密鑰時仍可使用資料管理、審核與基礎檢索，需要外部服務的操作會提示缺少配置。原生文字 PDF 在本地提取，掃描頁才需要 OCR；舊版二進位 Office 格式可能僅保存原件，建議先轉 DOCX／XLSX／PPTX。

## 選裝文件編輯

~~~sh
python -m pip install -r requirements-file-agent.txt
~~~

在 .env 設定 ENABLE_FILE_AGENT=true，配置模型接口後重啟。Office 預覽另需 LibreOffice，讓 soffice 位於系統 PATH。功能預設關閉；未安裝／未啟用時會明確提示。

工具以服務端檔案 ID 存取資料，每個使用者和任務使用獨立目錄，不接受模型提供任意本機路徑，也不提供 shell 或任意程式執行工具。輸出沿用檔案權限與下載接口；使用前仍應核對模型生成內容。

**2 核、2 GB 的保守預設：** 文件解析、編輯和 Office 預覽共用一個處理名額；繁忙時返回可重試提示。解析／預覽在獨立進程執行，具有 120 秒期限與 768 MiB 工作進程上限。這是資源保護配置，並非使用者承載量保證。

本機神經網絡嵌入另外安裝：

~~~sh
python -m pip install -r requirements-local.txt
~~~

先自行下載模型，將 LOCAL_EMBEDDING_MODEL 指向本機目錄並設定 EMBEDDING_BACKEND=local。執行時僅讀取既有模型；2 GB 主機建議保留 TF-IDF。

## 化學教師模式

通用教學規則隨原始碼提供。沒有自備課堂資料時，不會假稱引用某位老師的講課內容或個人風格。

私有資料位於 HOUKONG_DATA_DIR/private/chemistry-classroom/。部署者自行授權、整理 corpus.json 及必要教師資料，擁有者必須對應自己的本機帳號。空白格式見 examples/chemistry-corpus.example.json。

~~~sh
python scripts/build_chemistry_corpus.py --data-dir <自己的私有資料目錄> --owner-account <自己的帳號>
~~~

此工具整理自行準備的資料；本倉庫不提供任何人的錄音帳號、課堂轉寫或私人同步服務憑證。

## 資料、備份與部署

預設資料在 data/，包括帳號、會話、對話、文件記錄、已發布／個人原件、待審原件和私有教學資料，均由 Git 忽略。學生共享資料需教師審核；個人資料只對擁有者可見。

HOUKONG_CHROMA_DIR 可單獨設定向量庫。Windows 路徑包含非 ASCII 字元時，預設向量庫位於 %LOCALAPPDATA%/HouKongIntellectPublic/<資料目錄識別碼>/chroma_db；備份應同時涵蓋資料目錄和實際向量庫。不同部署不可共用同一資料路徑。

採用單一應用進程，避免多進程寫入 JSON。對外部署另配 HTTPS、反向代理、系統權限、請求速率限制、監控和備份。刪除文件會保留刪除標記並撤銷檢索權限，即使向量清理暫時失敗也不得再次公開。

安全限制：單原檔 50 MB；ZIP 最多 100 個成員、單成員解壓 10 MB、累計 50 MB，並限制膨脹比例；PDF 最多 50 頁，每頁渲染不超過 1000 萬像素。超限請分拆文件。

索引文字最多 100 萬字元、512 個段落塊，向量以每批 32 塊處理；背景索引與解析、檔案代理共用單一處理名額。這些上限適用於小型部署，不代表可容納無限文件或使用者；2 核 2 GB 的實際並發還取決於外部 API 配額、回應時間、文件數量和監控結果。登入每帳號每分鐘最多 10 次，每來源最多 30 次，密碼校驗最多同時 2 個。

## 開發與測試

~~~sh
python -m pip install -r requirements-dev.txt -r requirements-file-agent.txt
python -m pip check
python -m unittest discover -s tests -v
~~~

測試必須在新的 Python 進程執行。測試套件在匯入應用前清空外部密鑰並切換至獨立暫存資料目錄，不讀取現有帳號、會話或私有資料；若應用配置已先載入則停止執行。測試用的文件、帳號及模型回應均為合成資料。

完整測試需設定 ENABLE_FILE_AGENT=true，使用臨時 HOUKONG_DATA_DIR 和 HOUKONG_CHROMA_DIR。測試採用合成帳號、臨時文件及模擬外部接口。GitHub Actions 分別驗證 Windows／Linux 的基礎安裝與選裝功能。

安全結果僅適用於被檢查版本和範圍，不代表實際部署主機或第三方服務已受測試。請參閱 SECURITY.md。
