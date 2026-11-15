import time
import urllib.request
import json

LLM_URL = "http://127.0.0.1:18082/v1/chat/completions"

SYSTEM_PROMPT = (
    "你是一个专业的语音输入法后处理助手。请对用户的语音识别文本进行："
    "1. 去除口癖（如嗯、啊、那个、就是）；"
    "2. 修正同音错别字与中英混排格式（保留英文专业术语）；"
    "3. 补充准确标点符号。"
    "只输出最终修正后的文本，不要输出任何解释或多余内容。/no_think"
)

def polish_text(raw_text: str) -> tuple[str, float]:
    payload = {
        "model": "qwen3-4b",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    elapsed = (time.perf_counter() - t0) * 1000
    text = data["choices"][0]["message"]["content"].strip()
    return text, elapsed

if __name__ == "__main__":
    sample = "嗯那个就是帮我把今天的 api 接口文档发到群里然后顺便 check 一下数据库有没有报错"
    print("原始识别:", sample)
    out, ms = polish_text(sample)
    print(f"润色结果 ({ms:.1f} ms):", out)
