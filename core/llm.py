import os
from openai import OpenAI


def chat(messages: list[dict]) -> str:
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        temperature=0,
    )
    return resp.choices[0].message.content.strip()
