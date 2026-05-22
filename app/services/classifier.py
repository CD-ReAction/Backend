import os, json, re, logging
from pathlib import Path
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# async 클라이언트 (FastAPI async 환경에 맞춤)
client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = (
    Path(__file__).parent.parent / "prompts" / "feedback_classifier.txt"
).read_text(encoding="utf-8")
MODEL_NAME = "claude-haiku-4-5-20251001"


def safe_parse_json(raw: str) -> dict:
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', cleaned)
        if m:
            return json.loads(m.group())
        raise ValueError(f"JSON 파싱 실패: {raw[:200]}")


async def classify_feedback(text: str) -> dict:
    msg = f'다음 피드백을 분류해주세요:\n\n"""\n{text}\n"""'
    resp = await client.messages.create(
        model=MODEL_NAME, max_tokens=2048, temperature=0.0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": msg}],
    )
    logger.info(f"토큰 입력:{resp.usage.input_tokens} 출력:{resp.usage.output_tokens}")
    return safe_parse_json(resp.content[0].text)


def extract_tags(result: dict) -> list[dict]:
    """분류 JSON → [{'tag_type','tag_value'}, ...] (중복 제거)"""
    tags, seen = [], set()

    def add(t, v):
        if (t, v) not in seen:
            seen.add((t, v))
            tags.append({"tag_type": t, "tag_value": v})

    units = result["segments"] if result.get("is_split") else [result]
    for unit in units:
        add("priority", unit["priority"])
        for c in unit["categories"]:
            add("category", f"{c['main']}:{c['sub']}")
    return tags