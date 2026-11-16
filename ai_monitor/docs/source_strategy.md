# 來源策略(2026-11-16 定案)

## 產品定位
目標用戶:**一般 AI 用戶**(唔係專業人士)。內容按用戶關心嘅嘢分五支柱。

## 五支柱 → 數據源

| 支柱 | 主數據源 | 驗證狀態 |
|---|---|---|
| ① 新模型 | 35 條公司源(feed 類別過濾取 release/model) | 全部實測 200 |
| ② 強度/排行榜 | arena.ai(LMArena 已由 lmarena.ai 301 遷移)、Artificial Analysis;每週 Scrapling diff | 301 確認 |
| ③ 收費 | LiteLLM `model_prices_and_context_window.json`(GitHub raw,4MB);消費訂閱每週 pricing 頁檢查 | 200 確認 |
| ④ 錯誤/故障 | Status API JSON:`status.openai.com`(HTML 403 但 API 200!)、`status.claude.com`(由 status.anthropic.com 301 遷移)、`status.cohere.com`、`status.mistral.ai`;DeepSeek/Groq(已遷 groqstatus.com)逐站適配 | JSON 200 確認 |
| ⑤ 新工具 | `discover_trending.py`(GitHub Search API)+ `discover_hn.py`(HN Algolia) | 已運行,實戰打磨 |

## 核心原則
1. **結構化優先**:有 JSON/RSS/Atom 唔刮 HTML;status 源監察 `indicator` 欄位 diff = 零噪音故障事件。
2. **HTML 源行 Scrapling 階梯**:Fetcher(純 HTTP)先 → StealthyFetcher(自動開關瀏覽器)fallback。唔好預先全用瀏覽器(慢 5-10 倍/風險面大/指紋暴露)。
3. **發現引擎對付清單過時**:新公司/新工具只要上 GitHub trending 或 HN 就自動入 feed,唔使人手更新清單。
4. **已被排除**:X/Twitter(API 貴)、Discord(封閉);arXiv 留待用戶需求確認。01.AI 已剷(轉型企業顧問);Stability/AI21 降級留低。

## 已驗證嘅關鍵端點(2026-11-16 實測)
```
GET https://status.openai.com/api/v2/status.json            -> 200 application/json
GET https://status.claude.com/api/v2/status.json            -> 200(301 自 status.anthropic.com)
GET https://status.cohere.com/api/v2/status.json            -> 200 application/json
GET https://status.mistral.ai/api/v2/status.json            -> 200 application/json
GET https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json -> 200 (4MB)
GET https://arena.ai/leaderboard                            -> 301 自 lmarena.ai
```
注意:OpenAI incidents/unresolved.json 端點已改 Next.js(返回 HTML 唔係 JSON),所以監察 status.json 嘅 `status.indicator` 欄位做 diff。

## 2026-11 前沿版圖快照(來源對準用)
Gemini 4(Argon/Carbon/Hydrogen)、Claude Fable 5.2、GPT-6 Cyber、Kimi K3(2.8T)、DeepSeek V4.1 Flash、GLM-5.3、Qwen3.8-Max、騰訊混元 Hy3、百度文心 5.5、字節 Seedream 5.5。企業源清單已相應更新(Baidu/ByteDance Seed/Tencent Hunyuan/Z.ai/Cognition/World Labs 加入;01.AI 剷除)。
