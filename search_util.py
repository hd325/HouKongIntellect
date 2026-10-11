"""
搜尋工具模塊 - Serper API 集成
用於濠江小智"智能搜尋"功能
"""
import requests

from config.settings import SERPER_API_KEY

# Loaded from local environment configuration. Missing keys disable remote calls.
SERPER_KEY = SERPER_API_KEY


def web_search(q: str) -> str:
    """
    調用 Serper API 進行聯網搜尋，返回文本格式的搜尋結果摘要
    
    Args:
        q: 搜尋查詢字串
        
    Returns:
        格式化的搜尋結果文本，如果失敗則返回錯誤資訊
    """
    if not SERPER_KEY:
        return "聯網搜尋未啟用：請先設定 SERPER_API_KEY。"
    try:
        res = requests.post(
            "https://google.serper.dev/search",
            json={"q": q},
            headers={
                "X-API-KEY": SERPER_KEY,
                "Content-Type": "application/json"
            },
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        items = data.get("organic", [])
        
        if not items:
            return "未找到相關搜尋結果。"
        
        # 格式化搜尋結果
        results = []
        for i, item in enumerate(items[:5], 1):  # 只取前5條結果
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            results.append(f"{i}. **{title}**\n   {snippet}\n   來源：{link}")
        
        return "\n\n".join(results)
        
    except requests.exceptions.Timeout:
        return "搜尋請求超時，請稍後重試。"
    except requests.exceptions.RequestException:
        return "搜尋請求失敗，請檢查服務配置後重試。"
    except Exception:
        return "搜尋結果無法解析，請稍後重試。"


def web_search_with_sources(q: str) -> dict:
    """
    調用 Serper API 進行聯網搜尋，返回結構化結果（包含來源鏈接）
    
    Args:
        q: 搜尋查詢字串
        
    Returns:
        包含 answer 和 sources 的字典
    """
    if not SERPER_KEY:
        return {"answer": "聯網搜尋未啟用：請先設定 SERPER_API_KEY。",
                "sources": [], "snippets": []}
    try:
        res = requests.post(
            "https://google.serper.dev/search",
            json={"q": q},
            headers={
                "X-API-KEY": SERPER_KEY,
                "Content-Type": "application/json"
            },
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        items = data.get("organic", [])
        
        # 提取答案框（如果有的話）
        answer_box = data.get("answerBox", {})
        answer = answer_box.get("answer", "") or answer_box.get("snippet", "")
        
        # 提取來源
        sources = []
        for item in items[:5]:
            sources.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", "")
            })
        
        return {
            "answer": answer,
            "sources": sources,
            "snippets": [item.get("snippet", "") for item in items[:5]]
        }
        
    except Exception:
        return {
            "answer": "搜尋請求失敗，請檢查服務配置後重試。",
            "sources": [],
            "snippets": []
        }
