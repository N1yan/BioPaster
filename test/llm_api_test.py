from dotenv import load_dotenv
import os
from anthropic import Anthropic

os.chdir("/home/yan/projects/BioPaster")
load_dotenv(override=True)

PROVIDER = os.getenv("PROVIDER")
BASE_URL = os.getenv(f"{PROVIDER}_BASE_URL")
API_KEY = os.getenv(f"{PROVIDER}_API_KEY")
MODEL = os.getenv(f"{PROVIDER}_MODEL_4")

client = Anthropic(base_url=BASE_URL, api_key=API_KEY)

messages = [
    {"role": "user", "content": "Hello, how are you?"}
]

response = client.messages.create(
    model=MODEL,
    messages=messages,
    max_tokens=1000
)

print(response.content[-1].text)