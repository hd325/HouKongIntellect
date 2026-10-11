"""HouKongIntellect learning assistant API and web application."""

import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from typing import Any, Dict, List, Literal, Optional, Set
from urllib.parse import quote

logger = logging.getLogger("hk_intellect")

import httpx
import zhconv
from docx import Document
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 導入搜尋工具
from search_util import web_search, web_search_with_sources

# 騰訊雲 OCR 文字提取（取代本地 MinerU / LibreOffice / Tesseract）
from services.ocr.tencent_ocr import TencentOCR, TencentOCRNotConfigured
from services.upload_analysis import (
    MAX_NOTES_CHARS, classify_upload, find_possible_duplicate, hash_text,
    sign_extraction, study_period_for_month, verify_extraction,
)
from services.review_policy import is_published_record
from config.settings import (
    is_tencent_ocr_configured, DEEPSEEK_API_KEY, DEEPSEEK_API_URL,
    DEEPSEEK_MODEL, DEEPSEEK_REASONING_MODEL, DATA_DIR as CONFIG_DATA_DIR,
    PROJECT_ROOT, HOST, PORT, RELOAD,
)
from services.accounts import load_accounts, verify_password_record

# 模組級別 OCR 客戶端（騰訊雲）
_tencent_ocr = TencentOCR()


# ===================== Configuration & Constants =====================
# 數據檔案路徑
DATA_DIR = str(CONFIG_DATA_DIR)
CONVERSATIONS_FILE = os.path.join(DATA_DIR, "conversations_data.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions_data.json")
FILES_FILE = os.path.join(DATA_DIR, "files_data.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REVIEW_UPLOAD_DIR = os.path.join(DATA_DIR, "review_uploads")

# 確保上傳目錄存在
os.makedirs(UPLOAD_DIR, exist_ok=True)

API_CONFIG = {
    "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
    "DEEPSEEK_API_URL": DEEPSEEK_API_URL,
    "DEEPSEEK_MODEL": DEEPSEEK_MODEL,
    "DEEPSEEK_REASONING_MODEL": DEEPSEEK_REASONING_MODEL,
    "REQUEST_TIMEOUT": 30.0,
    "MAX_RETRIES": 3,
    "RETRY_DELAY": 1.0,
}

APP_CONFIG = {
    "TITLE": "濠江小智",
    "VERSION": "5.0",
    "DESCRIPTION": "智能學習輔導系統 - 問答路由 v5.0",
    "SESSION_TIMEOUT": 2592000,  # 30 days = 30 * 24 * 60 * 60
    "MAX_CONTEXT_MESSAGES": 5,
    "FILE_CONTENT_PREVIEW_LENGTH": 100,
    "FILE_CONTENT_MAX_LENGTH": 1000000,  # 100萬字（約1,000,000字符）
    "MAX_UPLOAD_SIZE": 50 * 1024 * 1024,  # 50MB
    "MATCH_THRESHOLD": 0.01,
    "MATCH_SCORE_BONUS_MULTI_KEYWORD": 0.15,
    "MATCH_SCORE_BONUS_LONG_KEYWORD": 0.08,
}

# ===================== 啟動前憑證校驗 =====================
# 本地環境變量由 config.settings 載入；缺少可選服務憑證不阻斷啟動。
# 不打印憑證內容；服務功能在使用時提示缺少配置。

# Accounts are loaded from the ignored local data directory. No users are bundled.
VALID_ACCOUNTS: Dict[str, Dict[str, Any]] = load_accounts()


def is_teacher_account(account: str) -> bool:
    return VALID_ACCOUNTS.get(account, {}).get("role") in {"老師", "教師", "teacher"}


def is_admin_account(account: str) -> bool:
    return VALID_ACCOUNTS.get(account, {}).get("is_admin") is True


def verify_account_password(account: str, password: str) -> bool:
    return verify_password_record(VALID_ACCOUNTS.get(account), password)


def public_user_info(account: str) -> Dict[str, Any]:
    record = VALID_ACCOUNTS.get(account, {})
    result = {field: record.get(field, "") for field in ("name", "class", "grade", "role")}
    result.update(account=account, is_teacher=is_teacher_account(account),
                  is_admin=is_admin_account(account))
    result["role"] = "老師" if result["is_teacher"] else "學生"
    if result["is_teacher"]:
        result["publish_classes"] = sorted({
            value["class"] for value in VALID_ACCOUNTS.values()
            if value.get("class") and value.get("role", "student") in {"student", "學生"}
        })
    return result


SYSTEM_PROMPT = """
你是"濠江小智"，由濠江中學研發的校園AI助手。你基於 DeepSeek-V4 大語言模型構建，專門為濠江中學的師生提供學習輔導和校園生活幫助。風格專業、友好，像老師一樣耐心解答學生的問題。

**關於濠江中學：**
- 濠江中學位於中國澳門，是一所歷史悠久的中學
- 現任校長：尤端陽先生
- 學校設有初中部（初一至初三）和高中部（高一至高三）
- 校訓：忠誠、勤奮、求實、創新

**時間感知（必須在每次回答中自然體現）：**
1. 開頭已提供當前完整時間，你必須在所有回答中自然地考慮時間因素
2. 如果用户說「最近」「今天」「昨天」「明天」「這週」「下周」「幾天前」等時間詞，需結合當前日期準確計算
3. 回答涉及日期、星期、節假日、倒計時（如「離高考還有多久」）時，直接用提供的當前時間計算，不要反問用户
4. **不要**在回答中強調「根據當前時間」，直接說出正確的時間或計算結果即可

**文件引用規則（嚴格遵循，違規即為錯誤）：**
5. 如果用户消息中包含文件引用標籤（如 [文件名.pdf]），系統已為你提供該文件的完整內容。請**嚴格基於文件內容**回答，不要自行編造文件以外的信息
6. 當文件內容足夠回答問題時 → 直接給出答案，**禁止**使用「根據文件」「文件中提到」「文件顯示」等暴露來源的詞語，像你本來就知道一樣自然作答
7. 當文件內容不足以回答問題時 → 如實告知「文件中未提及該內容」，這是唯一允許提及文件的場景。之後可補充你的知識
8. 如果用户未引用任何文件，系統不會提供文件內容。此時**絕對禁止**提及文件、上傳、資料庫等詞語，用自己的知識自由回答
9. **絕對禁止**主動詢問用户是否需要上傳文件，除非用户自己先明確表達了上傳意圖
10. 回答簡潔、準確、易懂，適合學生理解（初中/高中程度）
11. 記住當前對話上下文，進行多輪對話

**數學內容渲染規範（強制）：**
- 禁止輸出任何LaTeX命令：如 \\( \\) \\[ \\] $$ \\frac \\sqrt \\sum \\int \\binom \\begin \\end 等
- 禁止使用HTML實體編碼代替數學符號（如 &amp; &lt;），應直接輸出Unicode字符
- 使用純文本/Unicode字符表示數學公式：

  分數：用斜槓 / + 括號，如 (a+b)/(c+d)
  指數/下標：用 ^ / _，多字符用 {} 包裹，如 x^{2n+1}  a_{ij}
  平方根：√( )  如 √(a²+b²)
  立方根：∛( )  如 ∛(27)
  高次根：用指數形式，如 x^(1/5)
  排列數：A(n,m) 或 A_n^m  如 A(5,3)=60
  組合數：C(n,m) 或 C_n^m  如 C(5,3)=10
  求和：Σ_{i=1}^{n} 或 Σ(i=1, n)  如 Σ(i=1,10) i = 55
  積分：∫_{a}^{b}  如 ∫_0^1 x dx = 1/2
  極限：lim_{x→0} 或 lim(x→0)  如 lim(x→∞) 1/x = 0
  希臘字母：直接使用Unicode，如 α β γ Δ Σ π θ λ
  不等號：≠ ≤ ≥ ≈  如 x ≠ 0
  正負號：± ∓  如 x = ±2
  無窮：∞  如 x → ∞
  三角函數：sin(x) cos(x) tan(x)  如 sin(π/2) = 1
  對數：log(x) ln(x)  如 ln(e) = 1
  矩陣：每行用 [a, b, c] 分行表示，如：
    [1, 2]
    [3, 4]

- 輸出格式規範：
  所有公式單獨成行，與正文之間空一行
  重要公式用 ** 加粗，如 **x = (-b ± √(b²-4ac)) / (2a)**
  舉例時代入具體數字，展示逐步計算
  若用户輸入含LaTeX，先轉換為純文本再輸出

- 正確示例：
  求解 x² + 3x + 2 = 0
  使用求根公式解方程 **x² + 3x + 2 = 0**
  對於 **ax² + bx + c = 0**，求根公式為：
  **x = [-b ± √(b² - 4ac)] / (2a)**
  代入 a=1, b=3, c=2：
  x = [-3 ± √(9 - 8)] / 2
  x = [-3 ± √1] / 2
  x = [-3 ± 1] / 2
  所以 **x₁ = -1，x₂ = -2**

-   錯誤示例（嚴禁）：
  解方程 \\(x^2 + 3x + 2 = 0\\)，由求根公式 \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a} 得...

**用戶感知（當前對話專屬，絕不跨用戶）：**
- 系統已在開頭「當前用戶」欄位提供正在與你對話的這位用戶的資料（姓名、班級、角色）。
- 當用戶問「我是誰」「我在哪個班」時，直接依該資料回答，不要反問。
- 在鼓勵、建議或問候時，可自然地稱呼用戶姓名（如「某某同學」），顯得親切。
- 涉及班級、年級事務時，可結合班級資訊給出針對性建議。
- 不要過度稱呼姓名，保持自然；絕對不要在回答中透露、猜測或引用其他用戶的資訊。
"""

# ===================== 用戶資訊查詢（用戶感知） =====================

def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """
    根據 user_id（即登入賬號）查詢本地用戶基本資訊。
    僅返回安全欄位（姓名/班級/年級/角色），絕不包含密碼。
    返回 None 表示找不到該用戶。
    """
    acct = VALID_ACCOUNTS.get(user_id)
    if not acct:
        return None
    return {
        "name": acct.get("name", ""),
        "class": acct.get("class", ""),
        "grade": acct.get("grade", ""),
        "role": "老師" if is_teacher_account(user_id) else "學生",
    }


def build_user_info_line(user_profile: Dict[str, Any]) -> str:
    """
    將用戶資訊整理為注入系統提示詞的簡潔字串。
    格式：當前用戶：示例用戶，示例班級學生。
    """
    name = user_profile.get("name")
    cls = user_profile.get("class", "")
    role = user_profile.get("role", "學生")
    if not name:
        return ""
    return f"當前用戶：{name}，{cls}{role}。"


# ===================== Data Storage & Management =====================
class DataStore:
    """線程安全的數據存儲管理器（支援持久化）"""
    
    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._conversations: Dict[str, Dict[str, Any]] = {}
        self._files: List[Dict[str, Any]] = []
        self._file_contents: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        # 啓動時加載歷史數據
        self._load_data()
    
    def _load_data(self):
        """從檔案加載歷史數據"""
        try:
            if os.path.exists(CONVERSATIONS_FILE):
                with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._conversations = data.get("conversations", {})
                    
                    # 【修復】優先加載新的 files 數組（v4.0 格式）
                    new_files = data.get("files", [])
                    if new_files:
                        self._files = new_files
                        logging.info(f"✅ 從 files 數組加載了 {len(self._files)} 個檔案")
                    else:
                        # 兼容舊數據：加載 public_files 和 private_files，然後合併
                        old_public = data.get("public_files", [])
                        old_private = data.get("private_files", {})
                        self._files = self._migrate_old_files(old_public, old_private)
                        logging.info(f"✅ 從舊格式遷移了 {len(self._files)} 個檔案")
                    
                    self._file_contents = data.get("file_contents", {})
                    # 從舊檔案加載會話（兼容舊版本）
                    old_sessions = data.get("sessions", {})
                    if old_sessions and not self._sessions:
                        self._sessions = old_sessions
                    logging.info(f"✅ 加載了 {len(self._conversations)} 條對話歷史，{len(self._files)} 個檔案")
            else:
                self._files = []
                logging.info("📝 檔案不存在，創建空檔案列表")
        except Exception as e:
            logging.error(f"加載歷史數據失敗: {e}")
            self._files = []
        
        # 加載會話數據（支援服務器重啓後會話恢復）
        try:
            if os.path.exists(SESSIONS_FILE):
                with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                    self._sessions = json.load(f)
                    logging.info(f"✅ 加載了 {len(self._sessions)} 個會話")
            else:
                self._sessions = {}
                logging.info("📝 會話檔案不存在，創建新會話存儲")
        except Exception as e:
            logging.error(f"加載會話數據失敗: {e}")
            self._sessions = {}
    
    def _migrate_old_files(self, old_public: List[Dict], old_private: Dict[str, List[Dict]]) -> List[Dict]:
        """將舊的四級分類（public/private + audience）遷移到新的四級歸屬羣體"""
        migrated_files = []
        
        # 遷移公開檔案
        for f in old_public:
            audience = f.get("audience", "")
            if not audience:
                audience = "school"  # 默認映射到 school
            f["audience"] = audience
            if "type" in f:
                del f["type"]
            migrated_files.append(f)
        
        # 遷移私密檔案
        for user_id, files in old_private.items():
            for f in files:
                f["audience"] = "personal"  # 私密檔案映射到 personal
                if "type" in f:
                    del f["type"]
                migrated_files.append(f)
        
        logging.info(f"✅ 遷移了 {len(migrated_files)} 個檔案（從 public/private 到 audience）")
        return migrated_files
    
    async def save_data(self, *, strict: bool = False):
        """儲存數據到檔案（異步，使用線程池避免阻塞）"""
        try:
            async with self._persist_lock:
                data = copy.deepcopy({
                    "conversations": self._conversations,
                    "files": self._files,
                    "file_contents": self._file_contents,
                    "sessions": self._sessions,
                })
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._write_json, data)
        except Exception as e:
            logging.error(f"儲存數據失敗: {e}")
            if strict:
                raise
    
    def _write_json(self, data):
        """同步寫入檔案（由 run_in_executor 調用）"""
        temporary_path = CONVERSATIONS_FILE + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(temporary_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temporary_path, CONVERSATIONS_FILE)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
    
    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """獲取會話（線程安全）"""
        async with self._lock:
            return self._sessions.get(session_id)
    
    async def set_session(self, session_id: str, session_data: Dict[str, Any]) -> None:
        """設置會話（線程安全 + 持久化）"""
        async with self._lock:
            self._sessions[session_id] = session_data
        # 異步儲存會話
        asyncio.create_task(self._save_sessions())
    
    async def delete_session(self, session_id: str) -> None:
        """刪除會話（線程安全 + 持久化）"""
        async with self._lock:
            self._sessions.pop(session_id, None)
        # 異步儲存會話
        asyncio.create_task(self._save_sessions())
    
    async def _save_sessions(self):
        """儲存會話數據到檔案"""
        try:
            with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"儲存會話數據失敗: {e}")
    
    async def get_all_sessions(self) -> Dict[str, Dict[str, Any]]:
        """獲取所有會話（線程安全）"""
        async with self._lock:
            return self._sessions.copy()
    
    # Conversations methods
    async def get_conversation(self, conv_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return self._conversations.get(conv_id)
    
    async def set_conversation(self, conv_id: str, conversation: Dict[str, Any]) -> None:
        async with self._lock:
            self._conversations[conv_id] = conversation
        # 儲存到檔案
        await self.save_data()
    
    async def delete_conversation(self, conv_id: str) -> bool:
        """刪除指定對話"""
        async with self._lock:
            if conv_id in self._conversations:
                del self._conversations[conv_id]
                await self.save_data()
                return True
            return False
    
    async def get_user_conversations(self, user_id: str) -> List[Dict[str, Any]]:
        async with self._lock:
            return [
                conv for conv in self._conversations.values()
                if conv.get("user_id") == user_id
            ]
    
    # Files methods
    async def add_file(self, file_info: Dict[str, Any], content: str) -> None:
        """添加檔案（統一方法）"""
        async with self._lock:
            self._files.append(file_info)
            self._file_contents[file_info["id"]] = content
            try:
                await self.save_data(strict=True)
            except Exception:
                self._files.remove(file_info)
                self._file_contents.pop(file_info["id"], None)
                raise
    
    async def get_all_files(self, user_id: str) -> List[Dict[str, Any]]:
        """獲取用户有權限訪問的所有檔案"""
        async with self._lock:
            accessible_files = []
            logging.info(f"[get_all_files] user_id={user_id}, 內存中檔案總數={len(self._files)}")
            for i, f in enumerate(self._files):
                permission = check_file_permission(f, user_id)
                if permission:
                    accessible_files.append(f.copy())
                else:
                    logging.info(f"[get_all_files] 檔案{i}被過濾: id={f.get('id','N/A')[:20]}, name={f.get('name','N/A')[:30]}, audience={f.get('audience','N/A')}, owner={f.get('owner','N/A')}")
            logging.info(f"[get_all_files] 返回檔案數量={len(accessible_files)}")
            return accessible_files
    
    async def get_file_content(self, file_id: str) -> Optional[str]:
        async with self._lock:
            return self._file_contents.get(file_id)
    
    async def find_file_by_id(self, file_id: str) -> Optional[Dict[str, Any]]:
        """根據檔案ID查找檔案記錄"""
        async with self._lock:
            for f in self._files:
                if f.get("id") == file_id:
                    return f.copy()
            return None

    async def list_file_records(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return copy.deepcopy(self._files)

    async def update_file_record(self, file_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an audit transition atomically, restoring memory on failure."""
        async with self._lock:
            record = next((item for item in self._files if item.get("id") == file_id), None)
            if record is None:
                raise HTTPException(status_code=404, detail="檔案不存在")
            previous = copy.deepcopy(record)
            record.update(updates)
            try:
                await self.save_data(strict=True)
            except Exception:
                record.clear()
                record.update(previous)
                raise
            return copy.deepcopy(record)
    
    async def delete_file(self, user_id: str, file_id: str) -> bool:
        """刪除檔案（統一方法）"""
        async with self._lock:
            for i, f in enumerate(self._files):
                if f.get("id") == file_id:
                    # 檢查權限：僅檔案所有者或管理員可以刪除
                    owner = f.get("owner")
                    if user_id != owner and not is_admin_account(user_id):
                        return False
                    
                    deleted_file = self._files.pop(i)
                    self._file_contents.pop(file_id, None)
                    # 刪除磁盤上的原始檔案
                    storage_filename = deleted_file.get("storage_path")
                    if storage_filename:
                        disk_path = resolve_storage_path(storage_filename, deleted_file)
                        if disk_path and os.path.exists(disk_path):
                            try:
                                os.remove(disk_path)
                                logging.info(f"🗑️ 已刪除磁盤檔案: {disk_path}")
                            except OSError as e:
                                logging.warning(f"⚠️ 刪除磁盤檔案失敗: {disk_path}, {e}")
                    # 儲存操作在鎖外執行避免死鎖
                    asyncio.create_task(self.save_data())
                    return True
            return False
    
    async def cleanup_expired_sessions(self, timeout: int) -> int:
        """清理過期會話"""
        async with self._lock:
            now = datetime.now()
            expired = []
            for _session_id, _session in self._sessions.items():
                created_at = datetime.fromisoformat(_session.get("created_at", ""))
                if (now - created_at).total_seconds() > timeout:
                    expired.append(_session_id)
            
            for session_id in expired:
                del self._sessions[session_id]
            
            return len(expired)


# Initialize data store
data_store = DataStore()


# ===================== 檔案工具函數 =====================

# MIME 類型映射
MIME_TYPES: Dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json",
    ".xml": "text/xml; charset=utf-8",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".rar": "application/x-rar-compressed",
    ".7z": "application/x-7z-compressed",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
}

# 可在線預覽的檔案擴展名
PREVIEWABLE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".txt", ".md", ".csv", ".html", ".htm", ".json", ".xml",
    ".docx", ".xlsx", ".pptx",
}

# 純文本預覽的擴展名
TEXT_PREVIEW_EXTENSIONS = {
    ".txt", ".md", ".csv", ".html", ".htm", ".json", ".xml",
}


def safe_filename(filename: str) -> str:
    """清洗檔案名，防止路徑遍歷攻擊"""
    # 只保留檔案名部分（去掉路徑）
    filename = os.path.basename(filename)
    # 替換危險字符
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    # 限制長度
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200 - len(ext)] + ext
    return filename


def resolve_storage_path(storage_filename: Optional[str], file_info: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return a safe absolute path for a stored upload, or None if invalid."""
    if not storage_filename:
        return None

    upload_root = os.path.realpath(REVIEW_UPLOAD_DIR if file_info and not is_published_record(file_info) else UPLOAD_DIR)
    disk_path = os.path.realpath(os.path.join(upload_root, storage_filename))
    try:
        if os.path.commonpath([upload_root, disk_path]) != upload_root:
            return None
    except ValueError:
        return None
    return disk_path


def has_original_file(file_info: Dict[str, Any]) -> bool:
    """Whether the original uploaded file is still available on disk."""
    disk_path = resolve_storage_path(file_info.get("storage_path"), file_info)
    return bool(disk_path and os.path.isfile(disk_path))


def build_content_disposition(filename: str, disposition_type: str = "attachment") -> str:
    """Build an RFC 6266 header that keeps Unicode filenames intact."""
    original = os.path.basename(filename or "file")
    fallback = safe_filename(original).encode("ascii", "ignore").decode("ascii").strip()
    fallback = re.sub(r'["\\;]', "_", fallback) or "file"
    if fallback.startswith("."):
        fallback = f"file{fallback}"
    encoded = quote(original, safe="")
    return f'{disposition_type}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def get_file_extension(filename: str) -> str:
    """獲取檔案小寫擴展名"""
    return os.path.splitext(filename)[1].lower()


def get_mime_type(filename: str) -> str:
    """根據檔案名獲取 MIME 類型"""
    ext = get_file_extension(filename)
    return MIME_TYPES.get(ext, "application/octet-stream")


def is_previewable(filename: str) -> bool:
    """判斷檔案是否可在線預覽"""
    ext = get_file_extension(filename)
    return ext in PREVIEWABLE_EXTENSIONS


def is_text_preview(filename: str) -> bool:
    """判斷檔案是否用純文本方式預覽"""
    ext = get_file_extension(filename)
    return ext in TEXT_PREVIEW_EXTENSIONS


def check_file_permission(file_info: Dict[str, Any], user_id: str) -> bool:
    """
    檢查用户是否有權限訪問檔案
    
    權限規則：
    - personal: 僅檔案所有者
    - class: 同班同學
    - grade: 同年級
    - school: 全校
    """
    if not is_published_record(file_info):
        return False
    audience = file_info.get("audience", "school")
    owner = file_info.get("owner")
    
    # personal：僅所有者
    if audience == "personal":
        return user_id == owner
    
    # school：全校可見
    if audience == "school":
        return True

    if is_teacher_account(user_id) and audience in {"class", "grade"}:
        return True
    
    # 需要獲取用户資訊
    user_info = VALID_ACCOUNTS.get(user_id)
    if not user_info:
        return False
    
    if audience == "class":
        if file_info.get("audience_class"):
            return user_info.get("class") == file_info["audience_class"]
        # 同班可見
        if owner:
            owner_info = VALID_ACCOUNTS.get(owner)
            if owner_info:
                return user_info.get("class") == owner_info.get("class")
        return False
    
    if audience == "grade":
        if file_info.get("audience_grade"):
            return user_info.get("grade") == file_info["audience_grade"]
        # 同年級可見
        if owner:
            owner_info = VALID_ACCOUNTS.get(owner)
            if owner_info:
                owner_class = owner_info.get("class", "")
                user_class = user_info.get("class", "")
                # 提取年級（如 "高二3班" → "高二"）
                owner_grade = re.match(r'(.+?)\d', owner_class)
                user_grade = re.match(r'(.+?)\d', user_class)
                if owner_grade and user_grade:
                    return owner_grade.group(1) == user_grade.group(1)
        return False
    
    return False


def can_preview_file_record(file_info: Dict[str, Any], user_id: str) -> bool:
    if not is_published_record(file_info):
        return file_info.get("owner") == user_id or is_teacher_account(user_id)
    return check_file_permission(file_info, user_id)


class LoginRequest(BaseModel):
    """登入請求模型"""
    account: str = Field(..., min_length=1, max_length=64, description="用户賬號")
    password: str = Field(..., min_length=1, max_length=1024, description="用户密碼")


class QuestionRequest(BaseModel):
    """提問請求模型"""
    question: str = Field(..., min_length=1, max_length=2000, description="用户問題")
    session_id: str = Field(..., description="會話ID")
    think: bool = Field(default=False, description="是否啓用思考模式")
    search: bool = Field(default=False, description="是否啓用智能搜尋模式")
    trace: bool = Field(default=False, description="是否啓用溯源模式（段落级引用+RAG）")
    file_references: str = Field(default="", description="引用的檔案名列表，換行分隔")


class NewConversationRequest(BaseModel):
    """新建對話請求模型"""
    session_id: str = Field(..., description="會話ID")


class ApiResponse(BaseModel):
    """統一API響應模型"""
    code: int = Field(..., description="響應碼")
    message: Optional[str] = Field(None, description="響應消息")
    data: Optional[Dict[str, Any]] = Field(None, description="響應數據")


# ===================== Utility Functions =====================
def truncate_text(text: str, max_length: int = None) -> tuple[str, bool]:
    """
    截斷文本到指定長度，並添加提示資訊

    Args:
        text: 原始文本
        max_length: 最大字符數，默認使用 APP_CONFIG["FILE_CONTENT_MAX_LENGTH"]

    Returns:
        (截斷後的文本, 是否被截斷)
    """
    if max_length is None:
        max_length = APP_CONFIG.get("FILE_CONTENT_MAX_LENGTH", 1000000)

    if not text or len(text) <= max_length:
        return text, False

    # 截斷到指定長度
    truncated = text[:max_length]

    # 添加截斷提示
    original_length = len(text)
    truncation_notice = f"\n\n[注：原文共{original_length}字，已截取前{max_length}字]"

    logging.info(f"📏 文本已截斷: {original_length} -> {max_length} 字符")

    return truncated + truncation_notice, True


def extract_text_from_file(file: UploadFile, *, progress_callback=None, retry: bool = True) -> Dict[str, Any]:
    """
    從上傳的檔案中提取文本內容（騰訊雲 OCR 架構）

    支援格式:
    - 文檔: txt, docx, doc, pdf, md, rtf, wps, odt
    - 圖片: jpg, jpeg, png, gif, bmp, webp, tiff,tif, heic, svg (騰訊雲 OCR 識別)
    - 表格: xlsx, xls, csv, ods, et
    - 演示: pptx, ppt, odp, dps
    - 電子書: epub
    - 網頁: html, htm, xml
    - 壓縮包: zip, rar (嘗試提取文本檔案)

    PDF 與圖片統一由騰訊雲 OCR 識別；Office 文檔（docx/xlsx/pptx）使用原生庫提取，
    其餘格式使用各自解析器（非 OCR）。

    Args:
        file: 上傳的檔案對象

    Returns:
        {
            "content":          str,   # 提取的文本（OCR 失敗/無文字時為空）
            "source":           str,   # 提取來源標識
            "page_count":       int,   # OCR 頁數（非 OCR 格式為 0）
            "extraction_status":str,   # "success" | "partial" | "empty" | "failed" | "none"
            "error":            str,   # 不支援/解析失敗說明（非空時上傳應失敗）
        }
    """
    file_ext = file.filename.split(".")[-1].lower() if file.filename else ""

    try:
        file_content = file.file.read()

        # ========== 文本檔案 (增強多編碼支援) ==========
        if file_ext == "txt":
            # 嘗試多種編碼
            for encoding in ["utf-8", "gbk", "gb2312", "big5", "utf-16", "iso-8859-1"]:
                try:
                    return _ok_result(file_content.decode(encoding), "txt")
                except (UnicodeDecodeError, LookupError):
                    continue
            return _ok_result(file_content.decode("utf-8", errors="ignore"), "txt")

        # ========== Markdown 檔案 ==========
        elif file_ext in ["md", "markdown"]:
            for encoding in ["utf-8", "gbk", "gb2312"]:
                try:
                    return _ok_result(file_content.decode(encoding), "md")
                except UnicodeDecodeError:
                    continue
            return _ok_result(file_content.decode("utf-8", errors="ignore"), "md")

        # ========== RTF 富文本格式 ==========
        elif file_ext == "rtf":
            try:
                # 簡單RTF解析，提取純文本
                rtf_text = file_content.decode("utf-8", errors="ignore")
                # 移除RTF控制字
                import re
                # 移除分組、控制符
                rtf_text = re.sub(r'\\[a-z]+\d*\s?', '', rtf_text)
                rtf_text = re.sub(r'[{}]', '', rtf_text)
                rtf_text = re.sub(r'\\\'[0-9a-f]{2}', '', rtf_text)
                # 移除多餘空白但保留換行
                rtf_text = re.sub(r'\\line\s?', '\n', rtf_text)
                rtf_text = re.sub(r'\\par\s?', '\n', rtf_text)
                rtf_text = re.sub(r'\n{3,}', '\n\n', rtf_text)
                return _ok_result(rtf_text.strip(), "rtf") if rtf_text.strip() else _err_result("RTF 檔案解析失敗")
            except Exception as rtf_err:
                logging.error(f"RTF 解析失敗: {rtf_err}")
                return _err_result(f"RTF 解析失敗: {str(rtf_err)}")

        # ========== Word 文檔 (docx 原生提取 / doc 跳過) ==========
        elif file_ext == "docx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "doc":
            return _skipped_result(file.filename)

        # ========== WPS 文檔 (et/sxs/wps) ==========
        elif file_ext in ["et", "sxs", "wps"]:
            # WPS格式嘗試作為ZIP解析
            return _wrap_extract(_extract_wps_like, file_content, file.filename, "wps")

        # ========== PDF 檔案（騰訊雲 OCR）==========
        elif file_ext == "pdf":
            try:
                text = _tencent_ocr.recognize_pdf(
                    file_content, file.filename, progress_callback=progress_callback, retry=retry,
                )
            except TencentOCRNotConfigured:
                return {
                    "content": "",
                    "source": "tencent_ocr",
                    "page_count": 0,
                    "extraction_status": "failed",
                    "error": "",
                }
            return _tencent_ocr_result(text)

        # ========== 圖片檔案（騰訊雲 OCR）==========
        elif file_ext in ["jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif", "heic"]:
            try:
                text = _tencent_ocr.recognize_image(
                    file_content, file.filename, progress_callback=progress_callback, retry=retry,
                )
            except TencentOCRNotConfigured:
                return {
                    "content": "",
                    "source": "tencent_ocr",
                    "page_count": 0,
                    "extraction_status": "failed",
                    "error": "",
                }
            return _tencent_ocr_result(text)
        
        # ========== SVG 矢量圖 (嘗試提取文字) ==========
        elif file_ext == "svg":
            try:
                svg_text = file_content.decode("utf-8", errors="ignore")
                # 提取SVG中的文字
                text_elements = re.findall(r'<text[^>]*>([^<]+)</text>', svg_text, re.IGNORECASE)
                if text_elements:
                    return _ok_result(" | ".join(text_elements), "svg")
                # 嘗試提取CDATA
                cdata_text = re.findall(r'<!\[CDATA\[([^\]]+)\]\]>', svg_text)
                if cdata_text:
                    return _ok_result(" | ".join(cdata_text), "svg")
                return _err_result("SVG 檔案中未找到文本內容")
            except Exception as svg_err:
                return _err_result(f"SVG 解析失敗: {str(svg_err)}")

        # ========== Excel 表格 (xlsx 原生提取 / xls 跳過) ==========
        elif file_ext == "xlsx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "xls":
            return _skipped_result(file.filename)

        # ========== ODS 表格 (ods 跳過，無本地轉換器) ==========
        elif file_ext == "ods":
            return _skipped_result(file.filename)

        # ========== CSV/TSV 表格 ==========
        elif file_ext in ["csv", "tsv"]:
            return _wrap_extract(_extract_csv, file_content, file.filename, "csv", file_ext=file_ext)

        # ========== PowerPoint 演示文稿 (pptx 原生提取 / ppt 跳過) ==========
        elif file_ext == "pptx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "ppt":
            return _skipped_result(file.filename)

        # ========== ODP 演示文稿 (odp 跳過，無本地轉換器) ==========
        elif file_ext == "odp":
            return _skipped_result(file.filename)

        # ========== DPS 演示文稿 (金山格式) ==========
        elif file_ext == "dps":
            return _err_result("金山演示 .dps 格式暫不支援，建議另存為 .pptx 後上傳")

        # ========== HTML 網頁 (增強) ==========
        elif file_ext in ["html", "htm", "xml"]:
            return _wrap_extract(_extract_html, file_content, file.filename, "html")

        # ========== EPUB 電子書 ==========
        elif file_ext == "epub":
            return _wrap_extract(_extract_epub, file_content, file.filename, "epub")

        # ========== ODT 文檔 (odt 跳過，無本地轉換器) ==========
        elif file_ext == "odt":
            return _skipped_result(file.filename)

        # ========== 壓縮包 - 嘗試提取文本檔案 ==========
        elif file_ext in ["zip", "rar", "7z", "tar", "gz"]:
            return _wrap_extract(_extract_from_archive, file_content, file.filename, "archive", file_ext=file_ext)

        # ========== 不支援的格式：跳過 OCR，仍允許上傳 ==========
        else:
            # 不支援類型（如 .exe）不阻塞上傳：標記 skipped，返回空文本，文件仍可下載
            logging.info(f"⏭️ 不支援的格式 .{file_ext}，跳過文字提取: {file.filename}")
            return {
                "content": "",
                "source": "skip",
                "page_count": 0,
                "extraction_status": "skipped",
                "error": "",
            }

    except Exception as e:
        logging.error(f"檔案解析失敗 {file.filename}: {str(e)}")
        return _err_result(f"檔案解析失敗: {str(e)}")

    finally:
        file.file.seek(0)  # Reset file pointer


def _ok_result(content: str, source: str) -> Dict[str, Any]:
    """構造成功的提取結果。"""
    return {
        "content": content,
        "source": source,
        "page_count": 0,
        "extraction_status": "none",
        "error": "",
    }


def _err_result(error: str) -> Dict[str, Any]:
    """構造失敗的提取結果（不支援格式 / 解析異常）。"""
    return {
        "content": "",
        "source": "error",
        "page_count": 0,
        "extraction_status": "failed",
        "error": error,
    }


def _wrap_extract(fn, file_content: bytes, filename: str, source: str, file_ext: str = "") -> Dict[str, Any]:
    """
    包裝非 OCR 格式提取器：成功返回内容，失敗返回帶錯誤說明的結果。
    fn 簽名：fn(file_content, filename) 或 fn(file_content, filename, ext)
    """
    try:
        if "file_ext" in fn.__code__.co_varnames:
            text = fn(file_content, filename, file_ext) if file_ext else fn(file_content, filename)
        else:
            text = fn(file_content, filename)
    except Exception as e:
        logging.error(f"❌ {source} 解析異常: {e}")
        return _err_result(f"{source} 解析失敗: {str(e)}")

    if text and text.strip() and "失敗" not in text and "不支援" not in text:
        return _ok_result(text, source)
    # 解析器返回了錯誤說明字符串
    return _err_result(text if text else f"{source} 解析失敗")


def _skipped_result(filename: str) -> Dict[str, Any]:
    """構造跳過文字提取的結果（不支援格式 / 無本地轉換器）。上傳不阻塞。"""
    logging.info(f"⏭️ 跳過文字提取（無本地轉換器）: {filename}")
    return {
        "content": "",
        "source": "skip",
        "page_count": 0,
        "extraction_status": "skipped",
        "error": "",
    }


def _tencent_ocr_result(text: str) -> Dict[str, Any]:
    """將騰訊雲 OCR 返回的文本構造成標準提取結果。"""
    if text and text.strip():
        return {
            "content": text,
            "source": "tencent_ocr",
            "page_count": 0,
            "extraction_status": "success",
            "error": "",
        }
    return {
        "content": "",
        "source": "tencent_ocr",
        "page_count": 0,
        "extraction_status": "empty",
        "error": "",
    }


def _extract_office_text(file_content: bytes, filename: str) -> Dict[str, Any]:
    """
    使用原生庫提取 Office 文檔文本（docx / xlsx / pptx）。
    不再依賴 LibreOffice / MinerU。doc / xls / ppt / odt / ods / odp 不在支援範圍
    （由呼叫方走 _skipped_result）。

    提取失敗一律回傳空內容 + failed（error 留空），由 /api/upload 標記後仍返回 200，
    不阻塞上傳。
    """
    ext = filename.split(".")[-1].lower() if filename else ""
    from io import BytesIO
    try:
        if ext == "docx":
            doc = Document(BytesIO(file_content))
            paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            text = "\n".join(paras)
        elif ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(BytesIO(file_content), data_only=True, read_only=True)
            rows = []
            for ws in wb.worksheets:
                for r in ws.iter_rows(values_only=True):
                    cells = [str(c).strip() for c in r if c is not None and str(c).strip()]
                    if cells:
                        rows.append(" | ".join(cells))
            text = "\n".join(rows)
        elif ext == "pptx":
            from pptx import Presentation
            prs = Presentation(BytesIO(file_content))
            paras = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        t = shape.text_frame.text.strip()
                        if t:
                            paras.append(t)
            text = "\n".join(paras)
        else:
            return {"content": "", "source": "office", "page_count": 0, "extraction_status": "failed", "error": ""}

        if text and text.strip():
            return {"content": text, "source": ext, "page_count": 0, "extraction_status": "none", "error": ""}
        return {"content": "", "source": ext, "page_count": 0, "extraction_status": "failed", "error": ""}
    except Exception as e:
        logging.error(f"❌ Office 文本提取失敗 {filename}: {e}")
        return {"content": "", "source": ext, "page_count": 0, "extraction_status": "failed", "error": ""}


def _extract_from_archive(file_content: bytes, filename: str, file_ext: str) -> str:
    """從壓縮包中提取文本檔案"""
    try:
        if file_ext == 'zip':
            with zipfile.ZipFile(io.BytesIO(file_content), 'r') as zf:
                text_parts = []
                
                for name in zf.namelist():
                    if any(name.lower().endswith(ext) for ext in ['.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm']):
                        try:
                            content = zf.read(name).decode('utf-8', errors='ignore')
                            if len(content) > 10:
                                text_parts.append(f"[來自: {name}]\n{content[:5000]}")
                        except Exception:
                            continue
                
                if text_parts:
                    result = "\n\n".join(text_parts)
                    logging.info(f"✅ 壓縮包文本提取成功: {filename}")
                    return result
        
        return "壓縮包中未找到可解析的文本檔案"
        
    except zipfile.BadZipFile:
        return f".{file_ext} 壓縮包格式無效或已損壞"
    except Exception as archive_err:
        logging.error(f"壓縮包解析失敗: {archive_err}")
        return f"壓縮包解析失敗: {str(archive_err)}"




def extract_keywords(text: str) -> Set[str]:
    """
    從文本中提取關鍵詞（中文2字以上）
    統一轉為簡體再提取，兼容簡繁體輸入
    
    Args:
        text: 輸入文本
        
    Returns:
        關鍵詞集合
    """
    # 簡繁統一轉為簡體
    text_simp = zhconv.convert(text, "zh-cn")
    # 提取所有2-4字的中文詞（使用滑動視窗避免分詞錯誤）
    keywords: Set[str] = set()
    chinese_chars = re.findall(r"[\u4e00-\u9fa5]", text_simp.lower())
    for length in range(2, 5):
        for i in range(len(chinese_chars) - length + 1):
            keywords.add("".join(chinese_chars[i:i + length]))
    return keywords


def calculate_match_score(query: str, content: str) -> float:
    """
    計算查詢和內容的匹配分數（簡繁統一為簡體後再匹配）
    
    Args:
        query: 用户查詢
        content: 檔案內容
        
    Returns:
        匹配分數 (0.0-1.0)
    """
    # 統一轉為簡體，消除簡繁體差異
    query_simp = zhconv.convert(query.lower(), "zh-cn")
    content_simp = zhconv.convert(content.lower(), "zh-cn")
    query_lower = query_simp
    content_lower = content_simp
    
    # 精確短語匹配
    if query_lower in content_lower:
        return 1.0
    
    # 檢查所有查詢詞是否都出現在內容中
    query_words = query_lower.split()
    if query_words and all(word in content_lower for word in query_words):
        return 0.95
    
    # 關鍵詞匹配
    query_keywords = extract_keywords(query_lower)
    content_keywords = extract_keywords(content_lower)
    
    if not query_keywords:
        # 回退到單詞匹配
        if query_words:
            matched = sum(1 for word in query_words if word in content_lower)
            return matched / len(query_words)
        return 0.0
    
    # 計算關鍵詞重疊
    intersection = query_keywords & content_keywords
    if not intersection:
        return 0.0
    
    base_score = len(intersection) / len(query_keywords)
    
    # 多個關鍵詞匹配加分
    if len(intersection) >= 2:
        base_score += APP_CONFIG["MATCH_SCORE_BONUS_MULTI_KEYWORD"]
    
    # 長關鍵詞匹配加分
    for keyword in intersection:
        if len(keyword) >= 3:
            base_score += APP_CONFIG["MATCH_SCORE_BONUS_LONG_KEYWORD"]
    
    # 關鍵概念匹配
    key_concepts = [kw for kw in query_keywords if len(kw) >= 2]
    if key_concepts:
        concept_matches = sum(1 for concept in key_concepts if concept in content_lower)
        concept_score = concept_matches / len(key_concepts)
        base_score = max(base_score, concept_score * 0.9)
    
    return min(base_score, 1.0)


def build_file_match_score(filename: str, question: str) -> tuple[float, bool]:
    """
    基於檔案名和問題計算匹配分數（簡繁統一為簡體）
    
    Returns:
        (分數, 是否檔案名匹配)
    """
    # 統一轉為簡體
    filename_simp = zhconv.convert(filename.lower(), "zh-cn")
    question_simp = zhconv.convert(question.lower(), "zh-cn")
    filename_clean = filename_simp.replace(".docx", "").replace(".pdf", "").replace(".txt", "")
    question_lower = question_simp
    
    # 提取關鍵詞
    filename_keywords = extract_keywords(filename_clean)
    question_keywords = extract_keywords(question_lower)
    
    # 關鍵詞重疊
    keyword_overlap = len(filename_keywords & question_keywords)
    
    # 檢查精確匹配
    filename_lower = filename.lower()
    if question_lower in filename_lower:
        return 1.0, True
    
    # 計算關鍵詞重疊分數（即使不是精確匹配也要有分數）
    if question_keywords and keyword_overlap > 0:
        overlap_score = min(keyword_overlap / len(question_keywords) + 0.2, 1.0)
        return overlap_score, False  # 不是精確匹配，但有關鍵詞重疊
    
    return 0.0, False


async def stream_deepseek(prompt: str, question: str, think: bool = False):
    """
    流式調用設定的模型服務（stream=True）
    逐塊生成SSE事件，累積完整回覆

    Yields:
        SSE格式事件字串: data: {...}\n\n
    """
    if not API_CONFIG.get("DEEPSEEK_API_KEY"):
        error = {"type": "error", "detail": "尚未配置模型服務，請在本地 .env 設定 DEEPSEEK_API_KEY 後重啟應用。"}
        yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
        return
    headers = {
        "Authorization": f"Bearer {API_CONFIG['DEEPSEEK_API_KEY']}",
        "Content-Type": "application/json",
    }

    # 構建消息
    messages = [{"role": "system", "content": prompt}]
    
    if think:
        messages.append({
            "role": "user",
            "content": f"""請在回答前先用思考模式分析問題，將思考過程放在 <think> 標籤內，然後給出最終答案。

<think>
[你的思考分析過程]
</think>

[你的詳細解答]

用户問題：{question}"""
        })
    else:
        messages.append({"role": "user", "content": question})
    
    # 根據是否思考模式選擇模型
    model_name = API_CONFIG["DEEPSEEK_REASONING_MODEL"] if think else API_CONFIG["DEEPSEEK_MODEL"]
    
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.1,
        "stream": True,
    }
    
    accumulated = ""
    retries = 0
    while retries < API_CONFIG["MAX_RETRIES"]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
                verify=True
            ) as client:
                async with client.stream("POST", API_CONFIG["DEEPSEEK_API_URL"], headers=headers, json=payload) as response:
                    response.raise_for_status()
                    line_buffer = ""
                    async for raw_bytes in response.aiter_bytes():
                        if not raw_bytes:
                            continue
                        decoded = raw_bytes.decode("utf-8", errors="replace")
                        line_buffer += decoded
                        lines = line_buffer.split("\n")
                        # 最後一項可能不完整，留在 buffer 中
                        line_buffer = lines.pop()
                        for line in lines:
                            stripped = line.strip()
                            if not stripped or not stripped.startswith("data: "):
                                continue
                            data_str = stripped[6:]
                            if data_str == "[DONE]":
                                continue
                            try:
                                data = json.loads(data_str)
                                if "choices" in data and data["choices"]:
                                    delta = data["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        accumulated += content
                                        evt = json.dumps({"type": "content", "text": content, "done": False}, ensure_ascii=False)
                                        yield f"data: {evt}\n\n"
                            except json.JSONDecodeError:
                                continue
                    
                    # 處理 buffer 中可能殘留的數據行
                    if line_buffer.startswith("data: "):
                        data_str = line_buffer[6:]
                        if data_str and data_str != "[DONE]":
                            try:
                                data = json.loads(data_str)
                                if "choices" in data and data["choices"]:
                                    delta = data["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        accumulated += content
                                        evt = json.dumps({"type": "content", "text": content, "done": False}, ensure_ascii=False)
                                        yield f"data: {evt}\n\n"
                            except json.JSONDecodeError:
                                pass
                    return  # 成功完成
                    
        except httpx.TimeoutException:
            retries += 1
            if retries >= API_CONFIG["MAX_RETRIES"]:
                error_msg = json.dumps({"type": "error", "detail": "API 請求超時，請稍後重試"}, ensure_ascii=False)
                yield f"data: {error_msg}\n\n"
                return
            await asyncio.sleep(API_CONFIG["RETRY_DELAY"] * retries)
        except Exception as e:
            retries += 1
            if retries >= API_CONFIG["MAX_RETRIES"]:
                error_msg = json.dumps({"type": "error", "detail": f"API 調用失敗: {str(e)}"}, ensure_ascii=False)
                yield f"data: {error_msg}\n\n"
                return
            await asyncio.sleep(API_CONFIG["RETRY_DELAY"] * retries)
    
    return


# ===================== Dependencies =====================
async def verify_session(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> str:
    """
    驗證會話有效性
    
    Args:
        session_id: 會話ID
        
    Returns:
        驗證通過的會話ID
        
    Raises:
        HTTPException: 會話無效或已過期
    """
    session_id = x_session_id
    if not session_id and authorization:
        scheme, _, token = authorization.partition(" ")
        session_id = token.strip() if scheme.lower() == "bearer" and token else authorization.strip()

    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="會話無效或已過期"
        )

    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="會話無效或已過期"
        )
    
    # Removed accounts cannot retain access through an older persisted session.
    if session.get("account") not in VALID_ACCOUNTS:
        await data_store.delete_session(session_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效或已過期")

    # Malformed timestamps fail closed just like expired sessions.
    try:
        created_at = datetime.fromisoformat(session.get("created_at", ""))
        expired = (datetime.now() - created_at).total_seconds() > APP_CONFIG["SESSION_TIMEOUT"]
    except (ValueError, TypeError):
        expired = True
    if expired:
        await data_store.delete_session(session_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="會話已過期，請重新登入"
        )
    
    return session_id


# ===================== Application Lifecycle =====================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """應用生命週期管理"""
    # Startup
    logging.info(f"🚀 啓動濠江小智後端服務 v{APP_CONFIG['VERSION']}")

    # 騰訊雲 OCR 配置檢查（軟檢查：缺少金鑰時文字提取會失敗，但不阻斷啟動）
    if not is_tencent_ocr_configured():
        logging.warning(
            "⚠️ 騰訊雲 OCR 未配置（缺少 TENCENT_SECRET_ID / TENCENT_SECRET_KEY）。"
            "圖片與 PDF 的文字提取將失敗，請在本地 .env 配置憑證後重啟服務。"
        )
    else:
        logging.info("✅ 騰訊雲 OCR 憑證已載入，文字提取就緒")

    # 啓動後台任務：定期清理過期會話
    cleanup_task = asyncio.create_task(cleanup_expired_sessions())

    yield
    
    # Shutdown
    logging.info("🛑 關閉濠江小智後端服務")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


async def cleanup_expired_sessions():
    """後台任務：定期清理過期會話"""
    while True:
        try:
            cleaned = await data_store.cleanup_expired_sessions(APP_CONFIG["SESSION_TIMEOUT"])
            if cleaned > 0:
                logging.info(f"🧹 清理了 {cleaned} 個過期會話")
            await asyncio.sleep(300)  # 每5分鐘檢查一次
        except Exception as e:
            logging.error(f"清理過期會話時出錯: {e}")
            await asyncio.sleep(60)  # 出錯後1分鐘重試


# ===================== FastAPI Application =====================
app = FastAPI(
    title=APP_CONFIG["TITLE"],
    version=APP_CONFIG["VERSION"],
    description=APP_CONFIG["DESCRIPTION"],
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# 掛載靜態檔案目錄（用於提供logo等圖片）
static_dir = str(PROJECT_ROOT)
class FrontendStaticFiles(StaticFiles):
    """Serve public UI assets only; uploaded files must use authenticated routes."""
    PUBLIC_FILES = frozenset({
        "tailwindcss.js", "marked.js", "pdf.min.js", "pdf.worker.min.js",
        "font-awesome.css", "logo.png", "fonts/fontawesome-webfont.woff2",
    })

    async def get_response(self, path: str, scope):
        if path.replace("\\", "/") not in self.PUBLIC_FILES:
            raise HTTPException(status_code=404, detail="未找到静态资源")
        return await super().get_response(path, scope)


app.mount("/static", FrontendStaticFiles(directory=static_dir), name="static")


# ===================== 全局異常處理器 =====================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """將 FastAPI 驗證錯誤轉換為更友好的格式"""
    errors = exc.errors()
    
    # 提取第一個錯誤的詳細資訊
    if errors:
        first_error = errors[0]
        loc = " > ".join(str(l) for l in first_error.get("loc", []))
        msg = first_error.get("msg", "參數驗證失敗")
        detail = f"參數 '{loc}': {msg}"
        
        # 針對常見參數提供更友好的提示
        if "audience" in str(first_error.get("loc", [])).lower():
            detail = "缺少必需參數 'audience'（歸屬羣體），請選擇: personal, class, grade, school"
    else:
        detail = "請求參數驗證失敗"
    
    logging.warning(f"⚠️  參數驗證失敗: {detail}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": 422,
            "message": "參數驗證失敗",
            "detail": detail
        }
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """確保 HTTPException 返回一致格式"""
    # 檢查是否已有 detail 字段
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    
    # 4xx 是预期的客户端状态（例如页面尚未登录时检查旧会话），不应污染警告日志。
    log_http_error = logging.error if exc.status_code >= 500 else logging.info
    log_http_error(f"HTTP {exc.status_code}: {detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": "請求處理失敗",
            "detail": detail
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """捕獲未處理的異常，返回安全錯誤資訊"""
    logging.error(f"❌ 未處理的異常: {type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "服務器內部錯誤",
            "detail": "處理請求時發生未知錯誤，請稍後重試"
        }
    )


# ===================== API Routes =====================
@app.post("/api/login", response_model=ApiResponse, tags=["認證"])
async def login(request: LoginRequest) -> Dict[str, Any]:
    """
    用户登入介面
    
    Returns:
        會話ID和用户資訊
    """
    account = request.account.strip()
    password = request.password
    
    # 驗證賬號密碼
    if not verify_account_password(account, password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="賬號或密碼錯誤"
        )
    
    # 創建新會話
    session_id = str(uuid.uuid4())
    session_data = {
        "account": account,
        "user_info": public_user_info(account),
        "current_conversation_id": None,
        "created_at": datetime.now().isoformat(),
        "last_activity": datetime.now().isoformat(),
    }
    
    await data_store.set_session(session_id, session_data)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "登入成功",
        "data": {
            "session_id": session_id,
            "user_info": public_user_info(account)
        }
    }


@app.post("/api/new-conversation", response_model=ApiResponse, tags=["對話"])
async def new_conversation(
    _request: NewConversationRequest,
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    創建新對話
    
    Returns:
        新對話ID
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    # 創建新對話
    conversation_id = str(uuid.uuid4())
    new_conversation = {
        "id": conversation_id,
        "user_id": session["account"],
        "title": "新對話",
        "messages": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    
    await data_store.set_conversation(conversation_id, new_conversation)
    
    # 更新會話的當前對話
    session["current_conversation_id"] = conversation_id
    session["last_activity"] = datetime.now().isoformat()
    await data_store.set_session(session_id, session)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "新對話創建成功",
        "data": {"conversation_id": conversation_id}
    }


@app.post("/api/ask", tags=["對話"])
async def ask_question_stream(
    request: QuestionRequest,
    session_id: str = Depends(verify_session)
):
    """
    流式提問介面（stream=True，逐字輸出）
    """
    question = request.question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="問題不能為空"
        )
    
    # 獲取會話
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]

    # 獲取用戶基本資訊（用於對話個性化感知，僅安全欄位，不含密碼）
    user_profile = get_user_profile(user_id)
    logging.debug(f"👤 用戶畫像已注入對話（user_id={user_id}）")
    
    # 獲取或創建對話
    current_conv_id = session.get("current_conversation_id")
    
    if not current_conv_id:
        conv_response = await new_conversation(
            NewConversationRequest(session_id=session_id), session_id
        )
        conversation_id = conv_response["data"]["conversation_id"]
        conversation = await data_store.get_conversation(conversation_id)
    else:
        conversation = await data_store.get_conversation(current_conv_id)
        if not conversation:
            conv_response = await new_conversation(
                NewConversationRequest(session_id=session_id), session_id
            )
            conversation_id = conv_response["data"]["conversation_id"]
            conversation = await data_store.get_conversation(conversation_id)
        else:
            conversation_id = current_conv_id
    
    if not conversation:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="無法創建對話")
    
    # 構建對話上下文
    context = ""
    recent_messages = conversation.get("messages", [])[-APP_CONFIG["MAX_CONTEXT_MESSAGES"]:]
    for msg in recent_messages:
        role = "用户" if msg.get("is_user") else "助手"
        context += f"{role}: {msg.get('content', '')}\n"
    
    # ===== 智能問題分類路由 v5.0 =====
    question_type = classify_question(question, request.file_references)
    logging.info(f"📊 問題分類: {question_type} | 問題: {question[:60]}")

    # 構建文件引用增强問題
    enriched_question = question
    if request.file_references:
        enriched_question = f"用户引用了以下檔案：\n{request.file_references}\n\n用户問題：{question}"

    # ---- 系統問題直接應答（零延遲） ----
    if question_type == "system":
        direct_answer = get_direct_system_response(question, user_profile)
        if direct_answer:
            logging.info(f"⚡ 系統問題直接應答: \"{question[:40]}\"")

            # 儲存用户消息和直接回答
            conversation["messages"].append({
                "id": str(uuid.uuid4()),
                "content": question,
                "is_user": True,
                "timestamp": datetime.now().isoformat(),
            })
            conversation["updated_at"] = datetime.now().isoformat()
            await data_store.set_conversation(conversation_id, conversation)

            async def _direct_stream():
                """直接回答的 SSE 流"""
                full_text = direct_answer
                # 逐字發送以模擬流式效果（每次最多5字）
                i = 0
                chunk_size = 5
                while i < len(full_text):
                    chunk = full_text[i:i + chunk_size]
                    i += chunk_size
                    yield f"data: {json.dumps({'type': 'content', 'text': chunk}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)

                # 儲存回覆
                reply_id = str(uuid.uuid4())
                re_read_conv = await data_store.get_conversation(conversation_id)
                if re_read_conv:
                    re_read_conv["messages"].append({
                        "id": reply_id,
                        "content": full_text,
                        "is_user": False,
                        "timestamp": datetime.now().isoformat(),
                        "referenced_files": [],
                    })
                    if len(re_read_conv["messages"]) == 2:
                        re_read_conv["title"] = question[:30] + ("..." if len(question) > 30 else "")
                    re_read_conv["updated_at"] = datetime.now().isoformat()
                    await data_store.set_conversation(conversation_id, re_read_conv)

                # 更新會話活動時間
                session["last_activity"] = datetime.now().isoformat()
                await data_store.set_session(session_id, session)

                done_evt = json.dumps({"type": "done", "files": {"has_public_files": False, "has_private_files": False}}, ensure_ascii=False)
                yield f"data: {done_evt}\n\n"

            return StreamingResponse(
                _direct_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )

    # ---- 通用問題：跳過檔案搜尋 ----
    relevant_files: List[Dict[str, Any]] = []
    if question_type == "file":
        relevant_files = await search_relevant_files(question, user_id)
        if relevant_files:
            logging.info(f"🔎 找到 {len(relevant_files)} 個相關檔案")
        else:
            logging.info(f"📭 文件模式下未找到匹配檔案: \"{question[:50]}\"")
    elif question_type == "general":
        logging.info(f"📖 通用問題，跳過檔案搜尋")

    # 如果啓用了智能搜尋，先搜尋
    search_results = None
    if request.search:
        logging.info(f"🌐 智能搜尋模式：搜尋 \"{question}\"")
        search_results = web_search(question)
        if search_results:
            logging.info(f"✅ 搜尋完成，結果長度：{len(search_results)}")

    prompt = build_prompt_with_context(enriched_question, context, relevant_files, search_results, question_type, user_profile=user_profile)

    # 先儲存用户消息
    conversation["messages"].append({
        "id": str(uuid.uuid4()),
        "content": question,
        "is_user": True,
        "timestamp": datetime.now().isoformat(),
    })
    conversation["updated_at"] = datetime.now().isoformat()
    await data_store.set_conversation(conversation_id, conversation)

    async def generate_stream():
        """流式生成器：逐塊轉發 DeepSeek 回覆，完成後儲存對話"""
        full_text = ""
        reply_id = str(uuid.uuid4())
        has_public = any(f.get("audience") in ["school", "class", "grade"] for f in relevant_files)
        has_private = any(f.get("audience") == "personal" for f in relevant_files)
        
        try:
            async for event in stream_deepseek(prompt, question, think=request.think):
                yield event
                # 從SSE事件中提取文本
                if event.startswith("data: "):
                    try:
                        evt_data = json.loads(event[6:])
                        if evt_data.get("type") == "content":
                            full_text += evt_data.get("text", "")
                    except json.JSONDecodeError:
                        pass
            
            # 流式完成，儲存 AI 回覆到對話
            re_read_conv = await data_store.get_conversation(conversation_id)
            if re_read_conv:
                re_read_conv["messages"].append({
                    "id": reply_id,
                    "content": full_text,
                    "is_user": False,
                    "timestamp": datetime.now().isoformat(),
                    "referenced_files": [f["file_id"] for f in relevant_files[:3]],
                })
                if len(re_read_conv["messages"]) == 2:
                    re_read_conv["title"] = question[:30] + ("..." if len(question) > 30 else "")
                re_read_conv["updated_at"] = datetime.now().isoformat()
                await data_store.set_conversation(conversation_id, re_read_conv)
            
            # 更新會話活動時間
            session["last_activity"] = datetime.now().isoformat()
            await data_store.set_session(session_id, session)
            
            # 發送完成事件
            done_evt = json.dumps({
                "type": "done",
                "files": {
                    "has_public_files": has_public,
                    "has_private_files": has_private
                }
            }, ensure_ascii=False)
            yield f"data: {done_evt}\n\n"
            
        except Exception as e:
            logging.error(f"流式生成異常: {e}")
            # 儲存已累積的內容
            if full_text and conversation_id:
                try:
                    err_conv = await data_store.get_conversation(conversation_id)
                    if err_conv:
                        err_conv["messages"].append({
                            "id": reply_id,
                            "content": full_text or "抱歉，回覆不完整。",
                            "is_user": False,
                            "timestamp": datetime.now().isoformat(),
                        })
                        err_conv["updated_at"] = datetime.now().isoformat()
                        await data_store.set_conversation(conversation_id, err_conv)
                except Exception:
                    pass
            err_evt = json.dumps({
                "type": "error",
                "detail": "回覆生成中斷，請重試"
            }, ensure_ascii=False)
            yield f"data: {err_evt}\n\n"
    
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


def _append_rag_metrics(record: Dict[str, Any]) -> None:
    try:
        directory = os.path.join(DATA_DIR, "work")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "rag_metrics.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logging.warning("RAG 指标写入失败", exc_info=True)


@app.post("/api/chat", tags=["對話"])
async def chat_with_rag(
    request: QuestionRequest,
    session_id: str = Depends(verify_session)
):
    """
    溯源模式（段落级引用 + RAG 语义检索）流式问答接口。
    - 先做语义检索，取得与问题最相关的段落块；
    - 将参考资料与引用规则注入提示词，要求模型以 [N] 标注来源；
    - SSE 先推送 rag_context 事件（引用载荷），再流式推送 content；
    - 完成的 AI 消息附带 citation_map，供历史回看时渲染徽标。
    """
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="問題不能為空")

    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    user_id = session["account"]
    user_profile = get_user_profile(user_id)

    # 獲取或創建對話
    current_conv_id = session.get("current_conversation_id")
    if not current_conv_id:
        conv_response = await new_conversation(
            NewConversationRequest(session_id=session_id), session_id
        )
        conversation_id = conv_response["data"]["conversation_id"]
        conversation = await data_store.get_conversation(conversation_id)
    else:
        conversation = await data_store.get_conversation(current_conv_id)
        if not conversation:
            conv_response = await new_conversation(
                NewConversationRequest(session_id=session_id), session_id
            )
            conversation_id = conv_response["data"]["conversation_id"]
            conversation = await data_store.get_conversation(conversation_id)
        else:
            conversation_id = current_conv_id
    if not conversation:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="無法創建對話")

    # 構建對話上下文
    context = ""
    recent_messages = conversation.get("messages", [])[-APP_CONFIG["MAX_CONTEXT_MESSAGES"]:]
    for msg in recent_messages:
        role = "用户" if msg.get("is_user") else "助手"
        context += f"{role}: {msg.get('content', '')}\n"

    # 構建文件引用增强問題
    enriched_question = question
    if request.file_references:
        enriched_question = f"用户引用了以下檔案：\n{request.file_references}\n\n用户問題：{question}"

    # ===== 语义检索（RAG）=====
    citation_payload = []
    reference_block = ""
    retrieved = []
    retrieval_started = perf_counter()
    retrieval_failed = False
    retrieval_diagnostics = {
        "status": "no_relevant_context",
        "message": "未检索到相关资料，无法基于当前文件库作答。",
        "threshold_used": None,
        "threshold_path": [],
    }
    try:
        from services.retrieval.retriever import (
            retrieve, format_reference_block, build_citation_payload
        )
        filters = await retrieval_permission_filter(user_id)
        retrieved = retrieve(
            question,
            top_k=8,
            filters=filters,
            score_threshold=0.5,
            min_k=2,
        )
        if hasattr(retrieved, "diagnostics"):
            retrieval_diagnostics = retrieved.diagnostics()
        if retrieved:
            reference_block = format_reference_block(retrieved)
            citation_payload = build_citation_payload(retrieved)
            logging.info(f"🔗 溯源模式检索命中 {len(retrieved)} 个段落块")
    except Exception as e:
        logging.warning(f"⚠️ 溯源检索失敗，降級為普通回答: {e}")
        retrieval_failed = True
        retrieved = []
        retrieval_diagnostics = {
            "status": "retrieval_error",
            "message": "资料检索暂时不可用，无法基于文件库作答。",
            "threshold_used": None,
            "threshold_path": [],
        }

    from services.metrics_summary import build_retrieval_record
    rag_metrics = build_retrieval_record(retrieved, (perf_counter() - retrieval_started) * 1000)
    if retrieval_failed:
        rag_metrics["status"] = "retrieval_error"
    _append_rag_metrics(rag_metrics)
    prompt = build_rag_prompt(enriched_question, context, reference_block, user_profile=user_profile)

    # 先儲存用户消息
    conversation["messages"].append({
        "id": str(uuid.uuid4()),
        "content": question,
        "is_user": True,
        "timestamp": datetime.now().isoformat(),
    })
    conversation["updated_at"] = datetime.now().isoformat()
    await data_store.set_conversation(conversation_id, conversation)

    async def generate_stream():
        full_text = ""
        reply_id = str(uuid.uuid4())
        # 先推送引用载荷，前端據此渲染可點擊徽標
        try:
            rc = json.dumps(
                {
                    "type": "rag_context",
                    "chunks": citation_payload,
                    **retrieval_diagnostics,
                },
                ensure_ascii=False,
            )
            yield f"data: {rc}\n\n"
        except Exception:
            pass

        try:
            async for event in stream_deepseek(prompt, question, think=request.think):
                yield event
                if event.startswith("data: "):
                    try:
                        evt_data = json.loads(event[6:])
                        if evt_data.get("type") == "content":
                            full_text += evt_data.get("text", "")
                    except json.JSONDecodeError:
                        pass

            # 流式完成，儲存 AI 回覆（含 citation_map）
            citation_numbers = [int(number) for number in re.findall(r"\[(\d+)\]", full_text)]
            _append_rag_metrics({
                **rag_metrics, "event": "answer", "citation_marker_count": len(citation_numbers),
                "citation_map_length": len(citation_payload),
                "citation_coverage": len(citation_numbers) / len(citation_payload) if citation_payload else 0.0,
                "citation_out_of_range_rate": sum(number > len(citation_payload) for number in citation_numbers) / len(citation_numbers) if citation_numbers else 0.0,
            })
            re_read_conv = await data_store.get_conversation(conversation_id)
            if re_read_conv:
                re_read_conv["messages"].append({
                    "id": reply_id,
                    "content": full_text,
                    "is_user": False,
                    "timestamp": datetime.now().isoformat(),
                    "referenced_files": [
                        c["document_id"] for c in retrieved[:3] if c.get("document_id")
                    ],
                    "citation_map": citation_payload,
                    "references": [c["chunk_id"] for c in citation_payload],
                })
                if len(re_read_conv["messages"]) == 2:
                    re_read_conv["title"] = question[:30] + ("..." if len(question) > 30 else "")
                re_read_conv["updated_at"] = datetime.now().isoformat()
                await data_store.set_conversation(conversation_id, re_read_conv)

            session["last_activity"] = datetime.now().isoformat()
            await data_store.set_session(session_id, session)

            done_evt = json.dumps({
                "type": "done",
                "files": {"has_public_files": False, "has_private_files": False},
                "citation_map": citation_payload,
                "references": [c["chunk_id"] for c in citation_payload],
                "rag_status": retrieval_diagnostics,
            }, ensure_ascii=False)
            yield f"data: {done_evt}\n\n"

        except Exception as e:
            logging.error(f"溯源流式生成異常: {e}")
            if full_text and conversation_id:
                try:
                    err_conv = await data_store.get_conversation(conversation_id)
                    if err_conv:
                        err_conv["messages"].append({
                            "id": reply_id,
                            "content": full_text or "抱歉，回覆不完整。",
                            "is_user": False,
                            "timestamp": datetime.now().isoformat(),
                            "citation_map": citation_payload,
                            "references": [c["chunk_id"] for c in citation_payload],
                        })
                        err_conv["updated_at"] = datetime.now().isoformat()
                        await data_store.set_conversation(conversation_id, err_conv)
                except Exception:
                    pass
            err_evt = json.dumps({"type": "error", "detail": "回覆生成中斷，請重試"}, ensure_ascii=False)
            yield f"data: {err_evt}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def retrieval_permission_filter(user_id: str) -> Dict[str, Any]:
    """Use current publication/access records for both vector and BM25 search."""
    from services.knowledge.vector_store import get_vector_store
    records = {record["id"]: record for record in await data_store.list_file_records()}
    store = get_vector_store()
    indexed = await asyncio.to_thread(store._collection.get, include=["metadatas"])
    allowed = set()
    for metadata in indexed.get("metadatas") or []:
        document_id = metadata.get("document_id")
        effective_record = records.get(document_id, metadata)
        if (document_id and effective_record.get("audience") in {"personal", "class", "grade", "school"}
                and check_file_permission(effective_record, user_id)):
            allowed.add(document_id)
    # Chroma requires a non-empty $in list. A reserved impossible ID fails closed.
    return {"document_id": {"$in": sorted(allowed) or ["__no_authorized_document__"]}}


async def _index_file_background(file_info: Dict[str, Any], content: str) -> None:
    """后台将文档切块向量化入库（不阻塞上传响应）。失败仅记录日志，不影响主链路。"""
    if not is_published_record(file_info):
        return
    try:
        from services.knowledge.vector_store import get_vector_store, ChromaUnavailable
        store = get_vector_store()
        n = await asyncio.to_thread(store.index_document, file_info, content)
        logging.info(f"🗂️ 后台索引完成: {file_info.get('name')} → {n} 个段落块")
    except ChromaUnavailable as e:
        logging.warning(f"⚠️ 跳过索引（Chroma 不可用）: {file_info.get('name')} | {e}")
    except Exception as e:
        logging.error(f"❌ 后台索引失败: {file_info.get('name')} | {e}")


@app.get("/api/documents/{document_id}/chunks/{chunk_id}", tags=["溯源"])
async def get_document_chunk(
    document_id: str,
    chunk_id: str,
    session_id: str = Depends(verify_session)
):
    """按 chunk_id 返回原文片段（用于点击引用徽标后的原文定位与高亮）。"""
    try:
        from services.knowledge.vector_store import get_vector_store
        store = get_vector_store()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"向量库不可用: {e}"
        )
    chunk = store.get_chunk(chunk_id)
    if not chunk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到该引用片段")
    meta = chunk.get("metadata") or {}
    if meta.get("document_id") != document_id:
        raise HTTPException(status_code=404, detail="引用片段與文件不一致")
    await _authorize_document(document_id, session_id)
    content = chunk.get("content") or ""
    return {
        "code": 200,
        "data": {
            "chunk_id": chunk.get("chunk_id"),
            "document_id": meta.get("document_id") or document_id,
            "page": meta.get("page"),
            "filename": meta.get("filename") or "未知檔案",
            "content": content,
            "snippet": content[:200],
            "char_start": meta.get("char_start"),
            "char_end": meta.get("char_end"),
            "score": None,
        }
    }


def _resolve_document_file(document_id: str) -> Optional[str]:
    """Resolve the same stable upload IDs used by the existing reindex script."""
    from services.document_files import stable_upload_id
    root = os.path.realpath(UPLOAD_DIR)
    for name in os.listdir(root):
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) == root and os.path.isfile(path) and stable_upload_id(name) == document_id:
            return path
    return None


async def _authorize_document(document_id: str, session_id: str) -> None:
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="會話無效")
    record = await data_store.find_file_by_id(document_id)
    if not record:
        from services.knowledge.vector_store import get_vector_store
        try:
            stored = await asyncio.to_thread(lambda: get_vector_store()._collection.get(
                where={"document_id": document_id}, include=["metadatas"], limit=1))
            record = next(iter(stored.get("metadatas") or []), None)
        except Exception as error:
            raise HTTPException(status_code=503, detail="文件权限暂时无法确认，请稍后重试") from error
        if not record:
            raise HTTPException(status_code=404, detail="未找到此文档")
        if record.get("audience") not in {"school", "personal", "class", "grade"}:
            raise HTTPException(status_code=403, detail="此文档未提供有效的访问权限")
    if not check_file_permission(record, session["account"]):
        raise HTTPException(status_code=403, detail="您沒有權限讀取此檔案")


@app.get("/api/documents/{document_id}/file", tags=["檔案"])
async def get_document_file(document_id: str, session_id: str = Depends(verify_session)):
    await _authorize_document(document_id, session_id)
    path = _resolve_document_file(document_id)
    allowed_types = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".heic", ".txt", ".md", ".doc", ".docx"}
    if not path or get_file_extension(path) not in allowed_types:
        raise HTTPException(status_code=404, detail="此文档没有可用的原始文件")
    from services.document_files import display_upload_name
    extra_types = {".md": "text/plain", ".svg": "image/svg+xml", ".heic": "image/heic"}
    media_type = extra_types.get(get_file_extension(path)) or get_mime_type(path)
    return FileResponse(path, media_type=media_type, headers={
        "Content-Disposition": build_content_disposition(display_upload_name(os.path.basename(path)), "inline"),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@app.get("/api/knowledge/chunks/{chunk_id}", tags=["溯源"])
async def get_knowledge_chunk(
    chunk_id: str,
    session_id: str = Depends(verify_session)
):
    """
    段落溯源：按 chunk_id 返回原文片段 + 前后上下文（前后各 3 段）。

    供前端「两级交互」的第二级（点击引用标签 → Modal 完整视图）使用：
    - data.content：目标段落全文
    - data.context_before / context_after：前后各 3 段（page + content），用于上下文展示
    - data.document_id：可拼接 /api/files/{document_id}/download 实现「查看完整文件」
    """
    try:
        from services.knowledge.vector_store import get_vector_store
        store = get_vector_store()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"向量库不可用: {e}"
        )

    ctx = store.get_chunk_context(chunk_id)
    if not ctx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到该引用片段")

    target = ctx["target"]
    tmeta = target.get("metadata") or {}
    document_id = tmeta.get("document_id")
    if not document_id:
        raise HTTPException(status_code=403, detail="無法確認引用文件的權限")
    await _authorize_document(str(document_id), session_id)
    tcontent = target.get("content") or ""

    def _mini(c: Dict[str, object]) -> Dict[str, object]:
        m = c.get("metadata") or {}
        return {
            "chunk_id": c.get("chunk_id"),
            "page": m.get("page"),
            "content": c.get("content") or "",
        }

    return {
        "code": 200,
        "data": {
            "chunk_id": target.get("chunk_id"),
            "document_id": tmeta.get("document_id"),
            "file_type": "pdf" if (_resolve_document_file(str(tmeta.get("document_id") or "")) or "").lower().endswith(".pdf") else "text",
            "page": tmeta.get("page"),
            "filename": tmeta.get("filename") or "未知檔案",
            "content": tcontent,
            "snippet": tcontent[:300],
            "char_start": tmeta.get("char_start"),
            "char_end": tmeta.get("char_end"),
            "score": None,
            "context_before": [_mini(c) for c in ctx["context_before"]],
            "context_after": [_mini(c) for c in ctx["context_after"]],
        }
    }


class QuizGenerateRequest(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=128)
    question_types: List[Literal["choice", "blank", "short"]] = Field(default_factory=lambda: ["choice"], min_length=1, max_length=3)
    count: int = Field(default=5, ge=1, le=20, strict=True)
    difficulty: Literal["basic", "medium", "hard"] = "medium"


@app.post("/api/quiz/generate", tags=["學習"])
async def generate_document_quiz(request: QuizGenerateRequest, session_id: str = Depends(verify_session)):
    await _authorize_document(request.document_id, session_id)
    from services.quiz_service import generate_quiz, QuizValidationError, QuizGenerationError
    try:
        result = await generate_quiz(request.document_id, question_types=request.question_types,
                                     count=request.count, difficulty=request.difficulty, api_config=API_CONFIG)
        return {"code": 200, "data": result}
    except QuizValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except QuizGenerationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/rag/status", tags=["溯源"])
async def rag_status(session_id: str = Depends(verify_session)):
    """返回 RAG 向量库就绪状态（用于前端展示溯源能力是否可用）。"""
    try:
        from services.knowledge.vector_store import get_vector_store
        store = get_vector_store()
        stats = await asyncio.to_thread(store.stats)
        metadata_rows = await asyncio.to_thread(store._collection.get, include=["metadatas"])
        stats["document_count"] = len({m.get("document_id") for m in metadata_rows.get("metadatas", []) if m and m.get("document_id")})
        stats["available"] = True
        return {"code": 200, "data": stats}
    except Exception as e:
        return {"code": 200, "data": {"available": False, "error": str(e)}}


@app.get("/api/metrics/summary", tags=["统计"])
async def metrics_summary(session_id: str = Depends(verify_session)):
    """Aggregate recorded retrievals over the last 30 Macau calendar days."""
    from services.metrics_summary import build_metrics_summary
    summary = await asyncio.to_thread(build_metrics_summary, os.path.join(DATA_DIR, "work", "rag_metrics.jsonl"))
    return {"code": 200, "data": summary}


@app.get("/api/search", tags=["搜尋"])
async def search(
    q: str = Query(..., description="搜尋關鍵詞"),
    session_id: str = Depends(verify_session)
):
    """
    聯網搜尋介面（獨立）
    
    Returns:
        搜尋結果（包含答案和來源）
    """
    if not q or not q.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="搜尋關鍵詞不能為空"
        )
    
    logging.info(f"🌐 API搜尋請求：\"{q}\"")
    
    # 調用搜尋工具
    results = web_search_with_sources(q.strip())
    
    return {
        "code": status.HTTP_200_OK,
        "message": "搜尋完成",
        "data": {
            "answer": results.get("answer", ""),
            "sources": results.get("sources", []),
            "snippets": results.get("snippets", [])
        }
    }


async def search_relevant_files(question: str, user_id: str) -> List[Dict[str, Any]]:
    """
    搜尋相關檔案（基於歸屬羣體權限）

    關鍵修復（圖片引用無資訊問題）：用戶顯式引用的檔案（[檔名.後綴] 標籤）
    即使 OCR 後無文字，也必須納入上下文並標記 empty_content，
    交由 build_prompt_with_context 明確告知 AI「該檔案未提取到文字」，
    避免 AI 回答「沒有這張圖片的任何資訊」。

    Returns:
        按相關性排序的檔案列表
    """
    relevant_files = []
    
    # 獲取用户有權限訪問的所有檔案
    all_files = await data_store.get_all_files(user_id)

    # 提取用戶顯式引用的檔案名（[檔名.後綴]）
    referenced_names = _extract_referenced_filenames(question)
    
    # 搜尋所有可見檔案
    for file_info in all_files:
        file_id = file_info["id"]
        content = (await data_store.get_file_content(file_id)) or ""
        is_referenced = any(
            _name_matches(ref, file_info["name"]) for ref in referenced_names
        )

        # 檔案名匹配分數
        filename_score, is_filename_match = build_file_match_score(file_info["name"], question)
        # 內容匹配分數
        content_score = calculate_match_score(question, content) if content else 0.0
        # 合併分數：檔案名+內容取最大值（避免內容為空時檔案被遺漏）
        score = max(filename_score, content_score)

        # 顯式引用：強制納入（即便 OCR 無文字），並給予最高優先級
        if is_referenced:
            score = max(score, 10000.0)
            is_filename_match = True
        elif not content:
            # 未被引用且無 OCR 內容：跳過，避免噪音
            continue

        if score > APP_CONFIG["MATCH_THRESHOLD"] or is_referenced:
            relevant_files.append({
                "name": file_info["name"],
                "content": content[:APP_CONFIG["FILE_CONTENT_MAX_LENGTH"]],
                "audience": file_info.get("audience", "school"),
                "score": score,
                "file_id": file_id,
                "filename_match": is_filename_match or is_referenced,
                "empty_content": not bool(content),
            })
    
    # 按分數排序
    relevant_files.sort(key=lambda x: x["score"], reverse=True)
    return relevant_files


def _extract_referenced_filenames(question: str) -> List[str]:
    """
    從問題文本中提取用戶顯式引用的檔案名（[檔名.後綴] 標籤）。
    兼容前端注入的「用户引用了以下檔案：\n[xxx.png]」格式。
    """
    names: List[str] = []
    if not question:
        return names
    for m in re.finditer(r"\[([^\[\]\n]+)\]", question):
        token = m.group(1).strip()
        # 僅保留看起來像檔名的（含 . 後綴且非以 . 開頭）
        if "." in token and not token.startswith("."):
            names.append(token)
    return names


def _name_matches(ref: str, filename: str) -> bool:
    """
    判斷引用名 ref 是否指向檔案 filename（忽略簡繁 / 大小寫差異）。
    """
    a = zhconv.convert((ref or "").lower(), "zh-cn")
    b = zhconv.convert((filename or "").lower(), "zh-cn")
    if not a or not b:
        return False
    return a == b or a in b or b in a


# ===================== 問題分類路由 v5.0 =====================

# 文件相關關鍵詞（觸發文件內容注入）
_FILE_KEYWORDS: Set[str] = {
    "這個檔案", "這個文件", "這份檔案", "這份文件",
    "這份", "文件裡", "檔案裡", "文件里", "档案里",
    "第幾頁", "第几页", "這道題", "这道题",
    "這個檔案裡", "這個文件裡", "這份檔案裡", "這份文件裡",
    "上傳的檔案", "上傳的文件", "上传的档案", "上传的文件",
}

# 系統/基礎常識關鍵詞（觸發快速應答）
_SYSTEM_KEYWORDS: Set[str] = {
    "現在幾點", "现在几点", "今天幾號", "今天几号",
    "星期幾", "星期几", "你是誰", "你是谁",
    "你叫什麼", "你叫什么", "你的名字",
    "校長", "校长", "怎麼用", "怎么用",
    "功能", "幫助", "帮助", "身份",
    "幾點", "几点", "幾號", "几号",
    "日期", "時間", "时间",
    "老師", "老师", "學校", "学校",
    "校訓", "校训", "校區", "校区",
    "哪個班", "哪个班", "班級", "班级",
    # 用戶自身身份（感知當前登入者）
    "我是誰", "我是谁", "我的名字", "我叫什麼", "我叫什么", "我是幾班", "我是几班",
}


def classify_question(question: str, file_references: str = "") -> str:
    """
    輕量級問題分類器（快速方案：正則 + 關鍵詞匹配）

    Returns:
        "file"   - 需要文件內容支持
        "system" - 基礎常識/系統問題（時間、身份、校內資訊等）
        "general" - 通用學習/知識問題
    """
    # 1. 文件引用標籤（最高優先級）
    if file_references and file_references.strip():
        return "file"

    # 2. 檢測 [文件名.後綴] 模式
    if re.search(r'\[.+?\.(pdf|docx?|txt|png|jpg|jpeg|gif|xlsx?|pptx?|md|csv|zip|rar|7z)\]',
                 question, re.IGNORECASE):
        return "file"

    # 3. 文件相關關鍵詞
    q_lower = question.strip().lower()
    for kw in _FILE_KEYWORDS:
        if kw in q_lower:
            return "file"

    # 4. 系統/基礎常識關鍵詞
    for kw in _SYSTEM_KEYWORDS:
        if kw in q_lower:
            return "system"

    # 5. 默認為通用問題
    return "general"


def get_direct_system_response(question: str, user_profile: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    處理基礎系統問題的快速應答（零延遲，不調用 AI）

    Args:
        question: 用戶問題
        user_profile: 當前登入用戶畫像（含 name/class/role），用於用戶身份類問題即時應答

    Returns:
        str  - 直接回答文本
        None - 需要 AI 回答
    """
    q = question.strip()

    # ---- 時間/日期 ----
    _time_kw = {"幾點", "几点", "幾號", "几号", "日期", "時間", "时间", "星期"}
    if any(kw in q for kw in _time_kw):
        now = datetime.now()
        _wd = ["一", "二", "三", "四", "五", "六", "日"]
        return (
            f"现在是 {now.year} 年 {now.month} 月 {now.day} 日，"
            f"星期{_wd[now.weekday()]}，{now.hour:02d}:{now.strftime('%M')}:{now.strftime('%S')} CST（澳门标准时间）。"
        )

    # ---- 身份 ----
    _id_kw = {"你是誰", "你是谁", "你叫什麼", "你叫什么", "你的名字", "你的身份"}
    if any(kw in q for kw in _id_kw):
        return (
            "我是**濠江小智**，由濠江中學研發的校園AI助手。"
            "我基於 DeepSeek-V4 大語言模型構建，專門為濠江中學的師生提供學習輔導和校園生活幫助。"
            "有什麼我可以幫你的嗎？"
        )

    # ---- 用戶身份（當前登入者） ----
    _me_kw = {"我是誰", "我是谁", "我的名字", "我叫什麼", "我叫什么", "我的身份", "我的身分"}
    if any(kw in q for kw in _me_kw):
        if user_profile:
            name = user_profile.get("name", "同學")
            cls = user_profile.get("class", "")
            role = user_profile.get("role", "學生")
            grade = user_profile.get("grade", "")
            if grade:
                return (
                    f"您是**{name}**，{grade}{cls.replace(grade, '')}{role}。"
                    "有什麼我可以幫您的嗎？"
                )
            return (
                f"您是**{name}**，{cls}{role}。"
                "有什麼我可以幫您的嗎？"
            )
        return "抱歉，我暫時無法確認您的身份，請確認您已登入。"

    # ---- 用戶班級 ----
    _class_kw = {"我在哪個班", "我在哪个班", "我是幾班", "我是几班", "我的班級", "我的班级", "我幾班", "我几班"}
    if any(kw in q for kw in _class_kw):
        if user_profile:
            name = user_profile.get("name", "同學")
            cls = user_profile.get("class", "")
            return f"{name}同學，您在**{cls}**。"
        return "抱歉，我暫時無法確認您的班級，請確認您已登入。"

    # ---- 校長 ----
    if "校長" in q or "校长" in q:
        return "濠江中學現任校長是**尤端陽**先生。"

    # ---- 校訓 ----
    if "校訓" in q or "校训" in q:
        return "濠江中學校訓是：**忠誠、勤奮、求實、創新**。"

    # ---- 幫助/用法 ----
    _help_kw = {"怎麼用", "怎么用", "幫助", "帮助", "功能", "能做什么", "能做什麼", "有什麼功能", "有什么功能"}
    if any(kw in q for kw in _help_kw):
        return (
            "**濠江小智 使用指南：**\n\n"
            "1. **自由提問**：直接輸入學習問題，我會幫你解答\n"
            "2. **上傳文件**：點擊右上角的檔案管理按鈕，上傳學習資料（支持 PDF、Word、圖片等）\n"
            "3. **引用文件**：在文件面板雙擊文件名，即可在對話中引用文件內容提問\n"
            "4. **思考模式**：開啟後會展示我的推理過程\n"
            "5. **聯網搜尋**：需要查資料時可以開啟聯網搜尋功能\n\n"
            "有什麼想了解的嗎？儘管問我！"
        )

    # ---- 學校基本資訊（通用） ----
    if "學校" in q or "学校" in q or "校區" in q or "校区" in q:
        # 讓 AI 回答（資訊可能更全面）
        return None

    return None


# ===================== Prompt 構建 =====================


def build_prompt_with_context(
    question: str,
    context: str,
    relevant_files: List[Dict[str, Any]],
    search_results: Optional[str] = None,
    question_type: str = "general",
    user_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """構建帶上下文的Prompt（支援搜尋結果注入 + 問題分類路由 + 用戶感知注入）"""
    # 動態注入當前完整時間（每次請求刷新，精確到秒）
    _weekday_cn = ["一", "二", "三", "四", "五", "六", "日"]
    _now = datetime.now()
    _time_str = (
        f"现在时间是：{_now.year}年{_now.month}月{_now.day}日 星期{_weekday_cn[_now.weekday()]} "
        f"{_now.hour:02d}:{_now.strftime('%M')}:{_now.strftime('%S')} CST（澳门标准时间）"
    )
    # 構建系統提示詞：時間 → 當前用戶資訊（動態注入，每次對話不同）→ 基礎人設
    prompt = _time_str + "\n"
    if user_profile:
        _user_line = build_user_info_line(user_profile)
        if _user_line:
            prompt += _user_line + "\n"
    prompt += SYSTEM_PROMPT + "\n\n"

    if context:
        prompt += f"**對話上下文：**\n{context}\n\n"

    # 如果有搜尋結果，優先注入搜尋結果（不受分類影響）
    if search_results:
        prompt += f"**🌐 聯網搜尋結果：**\n{search_results}\n\n"
        prompt += f"**用户問題：** {question}\n\n"
        prompt += "**重要：** 請基於以上搜尋結果回答用户問題。如果搜尋結果中包含答案，請直接給出；如果搜尋結果不充分，可以結合你的知識庫補充回答。\n"
        return prompt

    # ===== 根據問題分類路由構建 Prompt =====
    if question_type == "system":
        # 系統/基礎問題：不注入任何文件內容，直接回答
        prompt += f"**用户問題：** {question}\n\n"
        prompt += "**説明：** 這是基礎系統問題（時間、身份、校園資訊、功能說明等），請直接回答，不要提及任何文件。\n"
        return prompt

    if question_type == "file":
        # 文件相關問題：注入匹配的文件內容
        if not relevant_files:
            prompt += f"**用户問題：** {question}\n\n"
            prompt += (
                "**説明：** 用户引用了文件但系統暫時無法讀取文件內容。"
                "請告知用户「文件暫時無法讀取，請稍後再試」。"
                "**禁止說「找不到文件」「文件中沒有」「文件缺失」等暴露系統狀態的話。**\n"
            )
            return prompt

        # 分類檔案
        filename_matched = [f for f in relevant_files if f.get("filename_match", False)]
        content_matched = [f for f in relevant_files if not f.get("filename_match", False)]

        # 檔案名高度匹配的檔案
        if filename_matched:
            prompt += "**🎯 高度相關檔案（檔案名匹配）：**\n"
            for idx, file in enumerate(filename_matched[:2], 1):
                prompt += f"【檔案{idx}：{file['name']}】（檔案名與用户問題直接相關）\n"
                if file.get("empty_content"):
                    # OCR 後無文字的檔案（如無文字圖片）：明確告知 AI，避免「沒有資訊」
                    prompt += (
                        f"用户引用了文件《{file['name']}》，但该文件经 OCR 处理后未提取到文字。\n\n"
                    )
                else:
                    content = file['content'][:APP_CONFIG["FILE_CONTENT_MAX_LENGTH"]]
                    prompt += f"文件《{file['name']}》内容如下：\n{content}\n\n"

        # 內容匹配的檔案
        if content_matched:
            start_idx = len(filename_matched) + 1
            prompt += "**📄 相關內容檔案：**\n"
            for idx, file in enumerate(content_matched[:3], start_idx):
                prompt += f"【檔案{idx}：{file['name']}】\n"
                if file.get("empty_content"):
                    prompt += (
                        f"用户引用了文件《{file['name']}》，但该文件经 OCR 处理后未提取到文字。\n\n"
                    )
                else:
                    content = file['content'][:APP_CONFIG["FILE_CONTENT_MAX_LENGTH"]]
                    prompt += f"文件《{file['name']}》内容如下：\n{content}\n\n"

        prompt += f"**用户問題：** {question}\n\n"

        if filename_matched:
            prompt += (
                "**重要：** 上面的【高度相關檔案】檔案名與用户問題直接匹配。"
                "請嚴格基於文件內容作答：內容足夠 → 直接給答案，不要提文件來源；"
                "若文件標註「经 OCR 处理后未提取到文字」（即圖片/檔案中確實沒有可識別的文字），"
                "請如實回答「該圖片/文件中未檢測到文字」；"
                "內容不足 → 可說「文件中未提及該內容」後補充你的知識。\n"
            )
        else:
            prompt += (
                "請嚴格基於以上文件內容回答問題。"
                "文件內容足夠 → 直接給答案，不要說「根據文件」等來源詞；"
                "文件內容不足 → 可如實說「文件中未提及該內容」後補充你的知識。\n"
            )

        return prompt

    # ===== 通用問題：不注入文件內容，直接調用大模型原生能力 =====
    prompt += f"**用户問題：** {question}\n\n"
    prompt += "**説明：** 這是通用學習/知識問題，請使用你的知識庫自由回答。不需要提及或依賴任何文件。\n"
    return prompt


def build_rag_prompt(
    question: str,
    context: str,
    reference_block: str,
    user_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """
    構建「溯源模式」(RAG) 的系統提示詞：注入段落级参考资料，
    并要求 AI 在每條关键信息後用 [N] 標註來源，便於前端渲染可點擊引用。
    """
    _weekday_cn = ["一", "二", "三", "四", "五", "六", "日"]
    _now = datetime.now()
    _time_str = (
        f"现在时间是：{_now.year}年{_now.month}月{_now.day}日 星期{_weekday_cn[_now.weekday()]} "
        f"{_now.hour:02d}:{_now.strftime('%M')}:{_now.strftime('%S')} CST（澳门标准时间）"
    )
    prompt = _time_str + "\n"
    if user_profile:
        _user_line = build_user_info_line(user_profile)
        if _user_line:
            prompt += _user_line + "\n"
    prompt += SYSTEM_PROMPT + "\n\n"

    if context:
        prompt += f"**對話上下文：**\n{context}\n\n"

    if reference_block:
        prompt += "**📚 参考资料（段落级溯源，请优先依据以下内容作答）：**\n"
        prompt += reference_block + "\n\n"
        prompt += (
            "**引用规则（必须遵守）：**\n"
            "1. 凡是答案中来自上述参考资料的具体事实、定义、数据、结论，"
            "必须在对应句子（或短语）末尾用方括号標註來源編號，例如 `……如圖所示[1]。`\n"
            "2. 若同时引用多个资料，可連寫如 `[1][3]`；编号必须与上方参考资料顺序严格一致。\n"
            "3. 若参考资料不足以回答，可結合你的知識補充，但補充内容不要加 [N] 標註；"
            "並可說明「参考资料中未直接提及」。\n"
            "4. 不要编造参考资料中不存在的编号；编号範圍限定在已给出的 [1]..[N]。\n"
            "5. 用繁體中文（澳門用語）回答。\n"
            "6. 每一個包含事實、數據或結論的陳述句，句末必須標註 [N]；"
            "寧可多標不可漏標。分項、列表或表格中的每一條具體資訊也要各自帶引用，"
            "不能只在引導句或整段末尾標一次。先選出支持該句的原文，再寫句子與引用。\n"
            "7. [N] 只能指向該編號對應的資料片段；引用必須支持同一句中的實體、"
            "日期、數字、條件和結論。資料中沒有的內容不要標註，也不要編造。\n"
            "8. 回答結尾不要輸出「參考來源」「参考来源」或參考文獻列表；"
            "前端已有引用徽標渲染。提交答案前逐句檢查引用是否齊全及對應正確。\n"
            "9. 先檢查片段是否真的回答了問題所問的實體、年份、專業概念和關係。"
            "若只有相似關鍵詞或無關片段，應明確回答「未檢索到相關資料，"
            "無法基於當前文件庫作答」，並指出缺少的資料；不要用自身知識猜測"
            "該問題的答案，也不要用無關片段加引用來填補空缺。\n"
        )
    else:
        prompt += (
            "**資料庫查詢結果：未检索到相关资料。**\n"
            "請明确告知用户目前无法依据文件库回答，并建议补充关键词、文件名或上传相关资料。"
            "不要使用模型自身知识猜测答案，也不要添加 [N] 引用標註。\n"
        )

    prompt += f"**用户問題：** {question}\n\n"
    return prompt


# ===================== 檔案自動重命名 =====================

_AUDIENCE_LABELS = {"personal": "個人", "class": "班級", "grade": "年級", "school": "學校"}
_SUBJECT_LABELS = {
    "chinese": "語文", "math": "數學", "english": "英語", "physics": "物理",
    "chemistry": "化學", "biology": "生物", "civics": "公民", "history": "歷史",
    "geography": "地理", "computer": "電腦", "other": "其他"
}
_PERIOD_LABELS = {"first": "第一段", "second": "第二段", "third": "第三段", "fourth": "第四段", "none": ""}
_MATERIAL_LABELS = {
    "quiz": "小測", "unified_test": "統測", "review_paper": "複習卷子",
    "exam_paper": "統測考試卷", "exam_outline": "考試提綱",
    "courseware": "課堂課件", "homework": "課後作業", "other": "其他"
}


def generate_file_name(
    original_name: str,
    audience: str,
    subject: str,
    material_type: str,
    material_custom: str,
    study_period: str,
    notes: str,
    user_id: str,
    data_store: "DataStore",
    quick_upload: bool = False
) -> str:
    """
    根據分類字段自動生成檔案名，格式：歸屬羣體_科目_學習階段_資料類型_備註.後綴
    處理同名衝突：自動追加 (1)(2)(3)...
    個人快速上傳模式：個人_1、個人_2 遞增命名
    """
    # 獲取檔案後綴
    ext = ""
    if "." in original_name:
        ext = original_name.rsplit(".", 1)[-1].lower()
    
    # 個人快速上傳模式：自動編號命名
    if audience == "personal" and quick_upload:
        # 統計當前用戶已上傳的個人類型文件數量
        count = 0
        for f in data_store._files:
            if f.get("audience") == "personal" and f.get("owner") == user_id:
                count += 1
        seq = count + 1
        base_name = f"個人_{seq}"
        final_name = f"{base_name}.{ext}" if ext else base_name
        
        # 衝突處理：若同名則追加時間戳後綴
        existing_names = _get_existing_filenames(audience, user_id, data_store)
        if final_name in existing_names:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            final_name = f"{base_name}_{ts}.{ext}" if ext else f"{base_name}_{ts}"
        
        return final_name
    
    # 構建檔案名各段
    parts = []
    
    aud_label = _AUDIENCE_LABELS.get(audience, "")
    if aud_label:
        parts.append(aud_label)
    
    subj_label = _SUBJECT_LABELS.get(subject, "")
    if subj_label:
        parts.append(subj_label)
    
    period_label = _PERIOD_LABELS.get(study_period, "")
    if period_label:
        parts.append(period_label)
    
    if material_type == "other" and material_custom:
        parts.append(material_custom)
    elif material_type:
        mat_label = _MATERIAL_LABELS.get(material_type, "")
        if mat_label:
            parts.append(mat_label)
    
    if notes:
        parts.append(notes)
    
    # 如果沒有任何分類資訊，回退原始檔案名
    if not parts:
        return original_name
    
    base_name = safe_filename("_".join(parts))
    
    # 處理同名衝突
    counter = 0
    extension = f".{ext}" if ext else ""
    final_name = base_name[:max(1, 200 - len(extension))] + extension
    existing_names = _get_existing_filenames(audience, user_id, data_store)
    
    while final_name in existing_names:
        counter += 1
        suffix = f"({counter})"
        final_name = base_name[:max(1, 200 - len(extension) - len(suffix))] + suffix + extension
    
    return final_name


def _get_existing_filenames(
    audience: str,
    user_id: str,
    data_store: "DataStore"
) -> set:
    """獲取已存在的檔案名集合，用於衝突檢測（基於歸屬羣體）"""
    names = set()
    # 從統一存儲中檢查同名
    for f in data_store._files:
        # 如果是 personal，只檢查當前用户的檔案
        if f.get("audience") == "personal":
            if f.get("owner") == user_id:
                names.add(f.get("name", ""))
        else:
            # school/class/grade 所有人都能看到，需要全局檢查
            names.add(f.get("name", ""))
    return names


@app.post("/api/upload/analyze", tags=["檔案"])
async def analyze_upload(
    file: UploadFile = File(...),
    session_id: str = Depends(verify_session),
):
    """Analyze this request only; never create a file record or saved analysis job."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="檔案名不能為空")
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > APP_CONFIG.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024):
        raise HTTPException(status_code=400, detail="檔案過大")
    raw = await file.read()
    filename = file.filename
    await file.close()
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="會話無效")
    user_id = session["account"]
    raw_hash = hashlib.sha256(raw).hexdigest()
    system_study_period = study_period_for_month(datetime.now().month)

    async def events():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        stopped = False
        page_count = 0

        def progress(event):
            nonlocal page_count
            if stopped:
                raise RuntimeError("Analysis request closed")
            if event.get("type") == "ocr_started" and isinstance(event.get("total"), int):
                page_count = event["total"]
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def process():
            source = UploadFile(filename=filename, file=io.BytesIO(raw))
            suggestions = dict.fromkeys(("subject", "study_period", "material_type", "material_custom", "notes"), "")
            extraction = {"content": "", "page_count": 0, "extraction_status": "failed", "is_truncated": False}
            possible_duplicate = False
            try:
                try:
                    result = await asyncio.to_thread(
                        extract_text_from_file, source, progress_callback=progress, retry=False,
                    )
                    text = result.get("content", "") if not result.get("error") else ""
                    full_text = text if isinstance(text, str) else ""
                    content, truncated = truncate_text(full_text)
                    extraction = {
                        "content": content,
                        "page_count": page_count or result.get("page_count", 0),
                        "extraction_status": result.get("extraction_status", "failed") if not result.get("error") else "failed",
                        "is_truncated": truncated,
                    }
                except Exception:
                    logging.warning("Upload analysis extraction failed")
                    full_text = ""
                try:
                    visible = await data_store.get_all_files(user_id)
                    contents = {entry["id"]: await data_store.get_file_content(entry["id"]) for entry in visible}
                    possible_duplicate = await asyncio.to_thread(
                        find_possible_duplicate, raw_hash, full_text, visible, contents, user_id,
                        uploads_dir=UPLOAD_DIR,
                    )
                except Exception:
                    logging.warning("Upload duplicate check failed")
                if extraction["content"]:
                    await queue.put({"type": "ai_started"})
                    try:
                        suggestions = await classify_upload(filename, extraction["content"], API_CONFIG)
                    except Exception:
                        logging.warning("Upload automatic classification failed")
                await queue.put({"type": "result", "data": {
                    **suggestions, "study_period": system_study_period,
                    "possible_duplicate": possible_duplicate,
                    "original_sha256": raw_hash,
                    "text_sha256": hash_text(full_text) if sum(char.isalnum() for char in full_text) >= 200 and not extraction["is_truncated"] else "",
                    "extraction": extraction,
                    "extraction_signature": sign_extraction(extraction, raw_hash, session_id),
                }})
            finally:
                await source.close()
                await queue.put(None)

        worker = asyncio.create_task(process())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = {"type": "heartbeat"}
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            stopped = True
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


_upload_commit_lock = asyncio.Lock()


def _upload_response(file_info: Dict[str, Any], content: str) -> Dict[str, Any]:
    published = is_published_record(file_info)
    return {
        "code": status.HTTP_200_OK,
        "message": f"{'檔案上傳成功' if published else '已提交審核'}: {file_info['name']}",
        "data": {
            "file_id": file_info["id"], "audience": file_info["audience"],
            "filename": file_info["name"],
            "original_name": file_info.get("original_name", ""),
            "original_filename": file_info.get("original_filename", ""),
            "has_original": True,
            "content_preview": content[:APP_CONFIG["FILE_CONTENT_PREVIEW_LENGTH"]] + ("..." if content else ""),
            "content_size": len(content), "page_count": file_info.get("page_count", 0),
            "extraction_status": file_info.get("extraction_status", "none"),
            "is_truncated": file_info.get("is_truncated", False),
            "review_status": file_info.get("review_status", "published"),
            "is_published": published,
            "publisher_role": file_info.get("publisher_role", "student"),
        },
    }


@app.post("/api/upload", response_model=ApiResponse, tags=["檔案"])
async def upload_file(
    file: UploadFile = File(...),
    audience: str = Query(..., description="歸屬羣體: personal, class, grade, school"),
    subject: str = Query("", description="科目"),
    material_type: str = Query("", description="資料類型"),
    material_custom: str = Query("", description="其他資料類型名稱"),
    study_period: str = Query("", description="學習階段"),
    notes: str = Query("", description="備註（最多5字）"),
    target_class: str = Query("", description="老師發布班級或年級資料的目標班級"),
    quick_upload: str = Query("false", description="舊版個人快速上傳"),
    extraction_result: str = Form(""),
    extraction_signature: str = Form(""),
    upload_request_id: str = Form(""),
    session_id: str = Depends(verify_session),
) -> Dict[str, Any]:
    """Commit the user's final values; the analysis request saves nothing."""
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="會話無效")
    user_id = session["account"]
    if audience not in _AUDIENCE_LABELS:
        raise HTTPException(status_code=422, detail="請選擇歸屬羣體")
    teacher = is_teacher_account(user_id)
    profile = public_user_info(user_id)
    audience_class = profile.get("class", "")
    audience_grade = profile.get("grade", "")
    if teacher and audience in {"class", "grade"}:
        target_class = target_class.strip() or audience_class
        if target_class not in profile.get("publish_classes", []):
            raise HTTPException(status_code=422, detail="請選擇發布資料的目標班級")
        audience_class = target_class
        audience_grade = next((record.get("grade", "") for record in VALID_ACCOUNTS.values()
                               if record.get("class") == target_class), "")
    subject, material_type, study_period = subject.strip(), material_type.strip(), study_period.strip()
    material_custom, notes = material_custom.strip(), notes.strip()
    for field, value, options in (
        ("科目", subject, _SUBJECT_LABELS),
        ("資料類型", material_type, _MATERIAL_LABELS),
        ("學習階段", study_period, _PERIOD_LABELS),
    ):
        if not value or value not in options:
            raise HTTPException(status_code=422, detail=f"請選擇{field}")
    if material_type == "other" and not material_custom:
        raise HTTPException(status_code=422, detail="請輸入資料類型名稱")
    if len(notes) > MAX_NOTES_CHARS:
        raise HTTPException(status_code=422, detail=f"備註最多 {MAX_NOTES_CHARS} 字")
    if len(material_custom) > 50:
        raise HTTPException(status_code=422, detail="資料文字過長")
    if material_type != "other":
        material_custom = ""
    if upload_request_id and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", upload_request_id):
        raise HTTPException(status_code=422, detail="無效的上傳請求")
    if not file.filename:
        raise HTTPException(status_code=400, detail="檔案名不能為空")
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size > APP_CONFIG.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024):
        raise HTTPException(status_code=400, detail="檔案過大")
    raw_bytes = await file.read()
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    file.file.seek(0)

    if extraction_result or extraction_signature:
        if not extraction_result or not extraction_signature or len(extraction_result) > 12000000:
            raise HTTPException(status_code=422, detail="無效的文字提取結果")
        try:
            extraction = json.loads(extraction_result)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="無效的文字提取結果")
        if not verify_extraction(extraction, extraction_signature, raw_hash, session_id):
            raise HTTPException(status_code=422, detail="文字提取結果與檔案不符")
        content = extraction["content"]
        is_truncated = extraction["is_truncated"]
        page_count = extraction["page_count"]
        extraction_status = extraction["extraction_status"]
    elif upload_request_id:
        # The analysis connection failed: preserve manual metadata, never retry OCR.
        content, is_truncated, page_count, extraction_status = "", False, 0, "failed"
    else:
        # Preserve compatibility for existing callers outside the analysis modal.
        extraction = await asyncio.to_thread(extract_text_from_file, file)
        if extraction.get("error"):
            raise HTTPException(status_code=400, detail=extraction["error"])
        content = extraction.get("content", "") or ""
        extraction_status = extraction.get("extraction_status", "none")
        if not content.strip() and extraction_status not in ("failed", "empty", "skipped"):
            raise HTTPException(status_code=400, detail="檔案中未提取到有效文本內容")
        content, is_truncated = truncate_text(content)
        page_count = extraction.get("page_count", 0)
    text_fingerprint = hash_text(content) if content and not is_truncated else ""

    async with _upload_commit_lock:
        # Deduplicate one request, while deliberately permitting another upload of the same file.
        if upload_request_id:
            for existing in data_store._files:
                if existing.get("owner") == user_id and existing.get("upload_request_id") == upload_request_id:
                    if existing.get("original_sha256") != raw_hash:
                        raise HTTPException(status_code=409, detail="上傳請求與檔案不符")
                    previous_content = await data_store.get_file_content(existing["id"]) or ""
                    return _upload_response(existing, previous_content)
        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{safe_filename(file.filename)}"
        review_required = not teacher and audience != "personal"
        storage_root = REVIEW_UPLOAD_DIR if review_required else UPLOAD_DIR
        storage_path = os.path.realpath(os.path.join(storage_root, storage_filename))
        if os.path.commonpath([storage_path, os.path.realpath(storage_root)]) != os.path.realpath(storage_root):
            raise HTTPException(status_code=400, detail="檔案名不合法")
        new_name = generate_file_name(
            file.filename, audience, subject, material_type, material_custom,
            study_period, notes, user_id, data_store, False,
        )
        file_info = {
            "id": file_id, "name": new_name,
            "original_name": file.filename, "original_filename": file.filename,
            "size": file_size, "uploaded_at": datetime.now().isoformat(),
            "owner": user_id, "audience": audience, "subject": subject,
            "material_type": material_type, "material_custom": material_custom,
            "study_period": study_period, "notes": notes,
            "storage_path": storage_filename, "is_truncated": is_truncated,
            "page_count": page_count, "extraction_status": extraction_status,
            "original_sha256": raw_hash, "text_sha256": text_fingerprint,
            "upload_request_id": upload_request_id,
            "review_status": "pending" if review_required else "published",
            "publisher_role": "teacher" if teacher else "student",
            "audience_class": audience_class, "audience_grade": audience_grade,
            "submitted_at": datetime.now().isoformat(),
            "published_at": "" if review_required else datetime.now().isoformat(),
            "review_history": [],
        }
        def write_original():
            os.makedirs(storage_root, exist_ok=True)
            with open(storage_path, "wb") as handle:
                handle.write(raw_bytes)
        try:
            await asyncio.to_thread(write_original)
            await data_store.add_file(file_info, content)
        except Exception:
            if os.path.isfile(storage_path):
                os.remove(storage_path)
            logging.error("Formal upload persistence failed")
            raise HTTPException(status_code=500, detail="檔案儲存失敗")
    if content.strip() and is_published_record(file_info):
        asyncio.create_task(_index_file_background(file_info, content))
    session["last_activity"] = datetime.now().isoformat()
    await data_store.set_session(session_id, session)
    return _upload_response(file_info, content)


def file_listing_info(record: Dict[str, Any]) -> Dict[str, Any]:
    """Expose document and review details without disk paths or credentials."""
    fields = ("id", "name", "audience", "size", "uploaded_at", "subject", "material_type",
              "study_period", "notes", "page_count", "extraction_status", "owner", "review_reason",
              "reviewed_by", "reviewed_at", "published_at", "submitted_at", "audience_class", "audience_grade")
    result = {field: record.get(field, "") for field in fields}
    owner = public_user_info(record.get("owner", ""))
    result.update(
        original_filename=record.get("original_filename") or record.get("original_name") or record.get("name", ""),
        has_original=has_original_file(record), is_truncated=record.get("is_truncated", False),
        review_status=record.get("review_status", "published"),
        publisher_role=record.get("publisher_role") or ("teacher" if owner["is_teacher"] else "student"),
        owner_name=owner.get("name", ""), owner_class=owner.get("class", ""),
    )
    return result


class ReviewDecisionRequest(BaseModel):
    action: Literal["approve", "reject"]
    reason: str = Field("", max_length=500)


@app.get("/api/reviews", response_model=ApiResponse, tags=["審核"])
async def get_reviews(session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    session = await data_store.get_session(session_id)
    user_id = session["account"]
    teacher = is_teacher_account(user_id)
    records = await data_store.list_file_records()
    reviewing = [record for record in records if not is_published_record(record)
                 and (teacher or record.get("owner") == user_id)]
    # Students track their own submissions and can browse authorized teacher materials.
    published = [record for record in records if is_published_record(record)
                 and check_file_permission(record, user_id)
                 and (teacher or record.get("owner") == user_id
                      or record.get("publisher_role") == "teacher")]
    reviewing.sort(key=lambda item: item.get("submitted_at", item.get("uploaded_at", "")), reverse=True)
    published.sort(key=lambda item: item.get("published_at") or item.get("uploaded_at", ""), reverse=True)
    return {"code": 200, "message": "success", "data": {
        "is_teacher": teacher,
        "reviewing": [file_listing_info(record) for record in reviewing],
        "published": [file_listing_info(record) for record in published],
    }}


@app.post("/api/reviews/{file_id}/decision", response_model=ApiResponse, tags=["審核"])
async def review_decision(file_id: str, request: ReviewDecisionRequest,
                          session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    session = await data_store.get_session(session_id)
    user_id = session["account"]
    if not is_teacher_account(user_id):
        raise HTTPException(status_code=403, detail="只有老師可以審核資料")
    reason = request.reason.strip()
    if request.action == "reject" and not reason:
        raise HTTPException(status_code=422, detail="請填寫退回原因")
    async with _upload_commit_lock:
        record = await data_store.find_file_by_id(file_id)
        if not record:
            raise HTTPException(status_code=404, detail="檔案不存在")
        if record.get("review_status") == "published" and record.get("reviewed_by") and request.action == "approve":
            return {"code": 200, "message": "資料已發布", "data": file_listing_info(record)}
        if record.get("review_status") != "pending":
            raise HTTPException(status_code=409, detail="此資料目前不在待審核狀態")
        now = datetime.now().isoformat()
        history = copy.deepcopy(record.get("review_history", []))
        history.append({"action": request.action, "account": user_id, "at": now, "reason": reason})
        updates = {"review_status": "published" if request.action == "approve" else "rejected",
                   "reviewed_by": user_id, "reviewed_at": now, "review_reason": reason,
                   "review_history": history}
        destination = None
        source = None
        if request.action == "approve":
            source = resolve_storage_path(record.get("storage_path"), record)
            destination = resolve_storage_path(record.get("storage_path"))
            if not source or not destination or not os.path.isfile(source):
                raise HTTPException(status_code=409, detail="原始檔案無法讀取，不能發布")
            if os.path.exists(destination):
                # A crash may leave an unpublished copy; verify it before reusing.
                with open(destination, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != record.get("original_sha256"):
                        raise HTTPException(status_code=409, detail="發布檔案與送審原件不一致")
            else:
                try:
                    os.makedirs(UPLOAD_DIR, exist_ok=True)
                    await asyncio.to_thread(shutil.copyfile, source, destination)
                except OSError as error:
                    if os.path.isfile(destination):
                        os.remove(destination)
                    raise HTTPException(status_code=500, detail="檔案發布失敗") from error
            updates["published_at"] = now
        try:
            updated = await data_store.update_file_record(file_id, updates)
        except Exception as error:
            if destination and os.path.isfile(destination):
                os.remove(destination)
            raise HTTPException(status_code=500, detail="審核結果儲存失敗") from error
        if source:
            try:
                os.remove(source)
            except OSError:
                logging.warning("Published review copy cleanup deferred: %s", file_id)
        if request.action == "approve":
            content = (await data_store.get_file_content(file_id)) or ""
            if content.strip():
                asyncio.create_task(_index_file_background(updated, content))
    return {"code": 200, "message": "已發布" if request.action == "approve" else "已退回修改",
            "data": file_listing_info(updated)}


@app.post("/api/reviews/{file_id}/resubmit", response_model=ApiResponse, tags=["審核"])
async def resubmit_review(file_id: str, session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    session = await data_store.get_session(session_id)
    user_id = session["account"]
    async with _upload_commit_lock:
        record = await data_store.find_file_by_id(file_id)
        if not record:
            raise HTTPException(status_code=404, detail="檔案不存在")
        if record.get("owner") != user_id:
            raise HTTPException(status_code=403, detail="只能重新提交自己的資料")
        if record.get("review_status") != "rejected":
            raise HTTPException(status_code=409, detail="此資料目前不能重新提交")
        now = datetime.now().isoformat()
        history = copy.deepcopy(record.get("review_history", []))
        history.append({"action": "resubmit", "account": user_id, "at": now})
        try:
            updated = await data_store.update_file_record(file_id, {
                "review_status": "pending", "review_reason": "", "reviewed_by": "", "reviewed_at": "",
                "submitted_at": now, "review_history": history,
            })
        except Exception as error:
            raise HTTPException(status_code=500, detail="提交審核失敗") from error
    return {"code": 200, "message": "已重新提交審核", "data": file_listing_info(updated)}


@app.get("/api/files", response_model=ApiResponse, tags=["檔案"])
async def get_files(session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    """
    獲取檔案列表
    
    Returns:
        公有和私有檔案列表
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]
    # 獲取用户有權限訪問的所有檔案（已按權限過濾）
    accessible_files = await data_store.get_all_files(user_id)
    
    # 按上傳時間降序排序（新→老）【v3.11新增】
    accessible_files.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    
    # 格式化返回數據（統一格式）
    files_list = []
    for f in accessible_files:
        # 提取文本（即使為空也一併返回，便於前端顯示提取狀態 / 調試）
        try:
            extracted_text = (await data_store.get_file_content(f["id"])) or ""
        except Exception:
            extracted_text = ""
        files_list.append({
            **file_listing_info(f),
            "id": f["id"],
            "name": f["name"],
            "original_filename": f.get("original_filename") or f.get("original_name") or f.get("name", ""),
            "audience": f.get("audience", "school"),
            "size": f["size"],
            "uploaded_at": f["uploaded_at"],
            "has_original": has_original_file(f),
            "subject": f.get("subject", ""),
            "material_type": f.get("material_type", ""),
            "study_period": f.get("study_period", ""),
            "notes": f.get("notes", ""),
            "page_count": f.get("page_count", 0),
            "extraction_status": f.get("extraction_status", "none"),
            "extracted_text": extracted_text,
        })
    
    # Current records override stale indexed/legacy metadata, including pending records.
    try:
        from services.document_catalog import build_indexed_file_entries
        from services.knowledge.vector_store import get_vector_store
        store = get_vector_store()
        current_records = {record["id"]: record for record in await data_store.list_file_records()}
        def indexed_entries():
            try:
                with open(os.path.join(DATA_DIR, "files_data.json"), encoding="utf-8") as handle:
                    legacy = json.load(handle)
            except (OSError, ValueError):
                legacy = {}
            return build_indexed_file_entries(
                store, UPLOAD_DIR, set(current_records) | {entry["id"] for entry in files_list},
                can_access=lambda metadata: check_file_permission(
                    current_records.get(metadata.get("document_id"), metadata), user_id),
                inline_records=legacy.get("files", []), inline_contents=legacy.get("file_contents", {}),
            )
        files_list.extend(await asyncio.to_thread(indexed_entries))
    except Exception:
        logging.warning("已索引文件列表暂不可用，保留现有上传文件", exc_info=True)

    return {
        "code": status.HTTP_200_OK,
        "message": "success",
        "data": {
            "files": files_list,
            "total": len(files_list),
        }
    }


@app.delete("/api/files/{file_id}", response_model=ApiResponse, tags=["檔案"])
async def delete_file(
    file_id: str,
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    刪除檔案（統一介面）
    
    權限檢查：
    - 檔案所有者可刪除自己的檔案
    - 管理員賬號可刪除所有檔案
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]
    logging.info(f"刪除檔案請求: user={user_id}, file_id={file_id}")
    
    # 使用統一的刪除方法（內部會檢查權限）
    async with _upload_commit_lock:
        success = await data_store.delete_file(user_id, file_id)
    
    if success:
        logging.info(f"檔案刪除成功: {file_id}")
        return {"code": status.HTTP_200_OK, "message": "刪除成功", "data": None}
    else:
        logging.warning(f"刪除失敗: 檔案不存在或權限不足, file_id={file_id}, user={user_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="刪除失敗：檔案不存在或權限不足")


@app.get("/api/files/{file_id}/preview", tags=["檔案"])
async def preview_file(
    file_id: str,
    session_id: str = Depends(verify_session)
):
    """
    檔案預覽介面
    
    根據檔案類型返回：
    - PDF/圖片：返回檔案流，瀏覽器直接展示（Content-Disposition: inline）
    - 文本檔案：返回提取的文本內容（JSON 格式）
    - Office 檔案：返回 Office Online Viewer URL（JSON 格式）
    - 其他：返回不支援預覽提示
    """
    # 驗證會話
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]
    
    # 查找檔案記錄
    file_info = await data_store.find_file_by_id(file_id)
    if not file_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="檔案不存在")
    
    # 權限校驗
    if not can_preview_file_record(file_info, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="您沒有權限預覽此檔案")
    
    # 確定用哪個檔案名來檢查擴展名（優先用原始檔案名）
    check_name = file_info.get("original_name") or file_info.get("name", "")
    
    # ---- 文本檔案預覽：返回提取的文本內容 ----
    if is_text_preview(check_name):
        content = await data_store.get_file_content(file_id)
        if content is None:
            content = ""
        return {
            "code": 200,
            "data": {
                "preview_type": "text",
                "file_name": file_info.get("original_name") or file_info.get("name"),
                "content": content,
            }
        }
    
    # ---- Office 檔案預覽：返回預覽資訊（前端使用 Office Online 或提示下載）----
    ext = get_file_extension(check_name)
    if ext in (".docx", ".xlsx", ".pptx"):
        # 檢查是否有磁盤檔案可下載
        has_raw = has_original_file(file_info)
        return {
            "code": 200,
            "data": {
                "preview_type": "office",
                "file_name": file_info.get("original_name") or file_info.get("name"),
                "has_download": has_raw,
                "download_url": f"/api/files/{file_id}/download" if has_raw else None,
            }
        }
    
    # ---- 二進制檔案預覽（PDF、圖片等）：返回檔案流 ----
    storage_filename = file_info.get("storage_path")
    if not storage_filename:
        # 舊檔案沒有磁盤副本，嘗試返回文本內容
        content = await data_store.get_file_content(file_id)
        if content:
            return {
                "code": 200,
                "data": {
                    "preview_type": "text",
                    "file_name": file_info.get("original_name") or file_info.get("name"),
                    "content": content,
                }
            }
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="檔案原始數據不存在")
    
    disk_path = resolve_storage_path(storage_filename, file_info)
    if not disk_path or not os.path.exists(disk_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="檔案原始數據不存在")
    
    # 安全校驗
    if not disk_path:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="檔案路徑不合法")
    
    if not is_previewable(check_name):
        # 不支援預覽，提示下載
        return {
            "code": 200,
            "data": {
                "preview_type": "unsupported",
                "file_name": file_info.get("original_name") or file_info.get("name"),
                "download_url": f"/api/files/{file_id}/download",
            }
        }
    
    # 返回檔案流
    mime_type = get_mime_type(check_name)
    original_name = file_info.get("original_name") or file_info.get("name", "file")
    
    return FileResponse(
        path=disk_path,
        media_type=mime_type,
        headers={
            "Content-Disposition": build_content_disposition(original_name, "inline"),
            "Cache-Control": "no-cache",
        }
    )


@app.get("/api/files/{file_id}/download", tags=["檔案"])
async def download_file(
    file_id: str,
    session_id: str = Depends(verify_session)
):
    """
    檔案下載介面
    
    返回檔案流，Content-Disposition: attachment 強制瀏覽器下載
    """
    # 驗證會話
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]
    
    # 查找檔案記錄
    file_info = await data_store.find_file_by_id(file_id)
    if file_info and not can_preview_file_record(file_info, user_id):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "無權限訪問"},
        )
    if file_info:
        storage_filename = file_info.get("storage_path")
        disk_path = resolve_storage_path(storage_filename, file_info)
        if not storage_filename or not disk_path or not os.path.isfile(disk_path):
            return JSONResponse(
                status_code=status.HTTP_410_GONE,
                content={"error": "原始檔案已丟失，無法下載"},
            )

        try:
            with open(disk_path, "rb"):
                pass
        except OSError:
            logging.exception("Failed to open original file for download: %s", disk_path)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "下載檔案讀取失敗"},
            )

        original_name = (
            file_info.get("original_filename")
            or file_info.get("original_name")
            or file_info.get("name")
            or "file"
        )
        return FileResponse(
            path=disk_path,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": build_content_disposition(original_name, "attachment"),
                "Cache-Control": "no-cache",
            },
        )
    if not file_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="檔案不存在")
@app.get("/api/conversations", response_model=ApiResponse, tags=["對話"])
async def get_conversations(session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    """
    獲取對話歷史列表
    
    Returns:
        對話列表（按更新時間倒序）
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    user_id = session["account"]
    conversations = await data_store.get_user_conversations(user_id)
    
    # 過濾掉空白對話（沒有消息的對話），並收集需要刪除的ID
    conversations_to_delete = []
    conversation_list = []
    
    for conv in conversations:
        messages = conv.get("messages", [])
        if len(messages) == 0:
            # 空白對話，標記待刪除
            conversations_to_delete.append(conv["id"])
        else:
            conversation_list.append({
                "id": conv["id"],
                "title": conv.get("title", "新對話"),
                "created_at": conv["created_at"],
                "updated_at": conv.get("updated_at", conv["created_at"]),
                "message_count": len(messages),
            })
    
    # 異步刪除空白對話
    for conv_id in conversations_to_delete:
        await data_store.delete_conversation(conv_id)
    
    conversation_list.sort(key=lambda x: x["updated_at"], reverse=True)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "success",
        "data": {"conversations": conversation_list, "total": len(conversation_list)},
    }


@app.get("/api/conversation/{conversation_id}", response_model=ApiResponse, tags=["對話"])
async def get_conversation_detail(
    conversation_id: str,
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    獲取指定對話的詳細內容
    
    Returns:
        對話詳情（包含所有消息）
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    conversation = await data_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="對話不存在"
        )
    
    # 驗證對話屬於當前用户
    if conversation.get("user_id") != session["account"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="無權訪問此對話"
        )
    
    return {
        "code": status.HTTP_200_OK,
        "message": "success",
        "data": {
            "id": conversation["id"],
            "title": conversation.get("title", "新對話"),
            "messages": conversation.get("messages", []),
            "created_at": conversation["created_at"],
            "updated_at": conversation.get("updated_at", conversation["created_at"]),
        }
    }


@app.post("/api/conversation/{conversation_id}/switch", response_model=ApiResponse, tags=["對話"])
async def switch_conversation(
    conversation_id: str,
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    切換到指定對話
    
    Returns:
        切換結果
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    conversation = await data_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="對話不存在"
        )
    
    # 驗證對話屬於當前用户
    if conversation.get("user_id") != session["account"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="無權訪問此對話"
        )
    
    # 切換當前對話
    session["current_conversation_id"] = conversation_id
    session["last_activity"] = datetime.now().isoformat()
    await data_store.set_session(session_id, session)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "切換成功",
        "data": {
            "conversation_id": conversation_id,
            "title": conversation.get("title", "新對話"),
            "messages": conversation.get("messages", []),
        }
    }


@app.delete("/api/conversation/{conversation_id}", response_model=ApiResponse, tags=["對話"])
async def delete_conversation(
    conversation_id: str,
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    刪除指定對話
    
    Returns:
        刪除結果
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    conversation = await data_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="對話不存在"
        )
    
    # 驗證對話屬於當前用户
    if conversation.get("user_id") != session["account"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="無權訪問此對話"
        )
    
    # 從存儲中刪除
    async with data_store._lock:
        if conversation_id in data_store._conversations:
            del data_store._conversations[conversation_id]
    
    # 如果刪除的是當前對話，清除會話中的引用
    if session.get("current_conversation_id") == conversation_id:
        session["current_conversation_id"] = None
        await data_store.set_session(session_id, session)
    
    # 儲存更改
    await data_store.save_data()
    
    return {
        "code": status.HTTP_200_OK,
        "message": "刪除成功",
        "data": {"deleted_id": conversation_id}
    }


@app.put("/api/conversation/{conversation_id}/rename", response_model=ApiResponse, tags=["對話"])
async def rename_conversation(
    conversation_id: str,
    request: Dict[str, str],
    session_id: str = Depends(verify_session)
) -> Dict[str, Any]:
    """
    重命名指定對話
    
    Returns:
        重命名結果
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    conversation = await data_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="對話不存在"
        )
    
    # 驗證對話屬於當前用户
    if conversation.get("user_id") != session["account"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="無權訪問此對話"
        )
    
    # 獲取新標題
    new_title = request.get("title", "").strip()
    if not new_title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="標題不能為空"
        )
    
    # 更新標題
    conversation["title"] = new_title
    await data_store.set_conversation(conversation_id, conversation)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "重命名成功",
        "data": {
            "conversation_id": conversation_id,
            "title": new_title
        }
    }


@app.get("/api/session", response_model=ApiResponse, tags=["認證"])
async def get_session_info(session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    """
    獲取當前會話資訊
    
    Returns:
        用户資訊和當前對話
    """
    session = await data_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="會話無效")
    
    # 獲取當前對話
    current_conv = None
    current_conv_id = session.get("current_conversation_id")
    if current_conv_id:
        conversation = await data_store.get_conversation(current_conv_id)
        if conversation:
            current_conv = {
                "id": conversation["id"],
                "messages": conversation.get("messages", []),
                "title": conversation.get("title", "新對話"),
            }
    
    return {
        "code": status.HTTP_200_OK,
        "message": "success",
        "data": {
            "user_info": public_user_info(session["account"]),
            "current_conversation": current_conv,
            "session_created_at": session["created_at"],
            "last_activity": session.get("last_activity", session["created_at"]),
        }
    }


@app.post("/api/logout", response_model=ApiResponse, tags=["認證"])
async def logout(session_id: str = Depends(verify_session)) -> Dict[str, Any]:
    """
    退出登入
    
    Returns:
        退出結果
    """
    await data_store.delete_session(session_id)
    
    return {
        "code": status.HTTP_200_OK,
        "message": "已退出登入",
        "data": {},
    }


@app.get("/", response_class=FileResponse, tags=["前端"])
async def serve_frontend():
    """
    提供前端頁面
    
    Returns:
        index.html 檔案
    """
    return FileResponse(str(PROJECT_ROOT / "index.html"))


@app.get("/favicon.ico", response_class=FileResponse, include_in_schema=False)
async def serve_favicon():
    """复用项目 Logo，避免浏览器自动请求 favicon 时产生 404。"""
    return FileResponse(str(PROJECT_ROOT / "logo.png"), media_type="image/png")


@app.get("/api/health", tags=["系統"])
async def health_check() -> Dict[str, Any]:
    """
    健康檢查介面（帶詳細調試資訊）
    
    Returns:
        系統狀態資訊
    """
    sessions = await data_store.get_all_sessions()
    
    return {
        "status": "healthy",
        "service": APP_CONFIG["TITLE"],
        "version": APP_CONFIG["VERSION"],
        "timestamp": datetime.now().isoformat(),
        "statistics": {
            "active_sessions": len(sessions),
            "total_conversations": len(data_store._conversations),
            "total_files": len(data_store._files),
            "loaded_file_contents": len(data_store._file_contents),
        },
    }


# ===================== Main =====================
if __name__ == "__main__":
    import uvicorn
    
    # 設定日誌
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
        log_level="info",
    )
