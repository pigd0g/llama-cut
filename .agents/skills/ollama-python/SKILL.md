---
name: ollama-python
description: Use the official ollama-python SDK to call local or cloud Ollama models from Python, including chat, generation, streaming, structured outputs, tool calling, multimodal input, embeddings, model management, async workflows, and error handling.
---

# Ollama Python Skill

Use this skill whenever Python code needs to interact with Ollama through the official `ollama-python` package.

## Scope

The official Python client is the preferred interface over manually calling Ollama's HTTP API. The package supports Python 3.8+ and is installed with `pip install ollama`. Ollama itself must be installed/running for local use, and the target model must be available locally or through a configured cloud endpoint.

## Installation

```bash
python -m pip install -U ollama
```

For projects using `uv`:

```bash
uv add ollama
```

Prefer a project virtual environment rather than installing into a system Python environment.

## Basic usage

Prefer `chat()` for conversational prompts and `generate()` for single-prompt generation.

```python
from ollama import chat

response = chat(
    model="gemma3",
    messages=[
        {"role": "user", "content": "Explain recursion in one paragraph."},
    ],
)

print(response.message.content)
```

Use typed response objects where useful:

```python
from ollama import ChatResponse, chat

response: ChatResponse = chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Why is the sky blue?"}],
)

text = response.message.content
```

## Client selection

Use the top-level functions for simple scripts. Use `Client` when configuration should be reused, and `AsyncClient` in async applications.

```python
from ollama import Client

client = Client(host="http://localhost:11434")
response = client.chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Do not create a new client for every request in a long-running application; reuse a configured client.

### Async

```python
from ollama import AsyncClient

client = AsyncClient()
response = await client.chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Use `AsyncClient` instead of blocking synchronous calls inside an async application.

## Streaming

Use `stream=True` when partial output should be consumed incrementally.

```python
from ollama import chat

stream = chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Write a short story."}],
    stream=True,
)

for chunk in stream:
    print(chunk.message.content, end="", flush=True)
```

For async streaming:

```python
from ollama import AsyncClient

client = AsyncClient()
stream = await client.chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Write a short story."}],
    stream=True,
)

async for chunk in stream:
    print(chunk.message.content, end="", flush=True)
```

When collecting a streamed response, append only the incremental content from each chunk. Do not duplicate the accumulated text.

## Chat message construction

Use the standard roles:

- `system`: model behaviour/instructions
- `user`: user input
- `assistant`: prior model output/tool calls
- `tool`: results returned by application tools

Maintain the complete conversation history when performing multi-turn chat or tool-calling loops.

```python
messages = [
    {"role": "system", "content": "You are a concise technical assistant."},
    {"role": "user", "content": "Explain Docker networking."},
]
```

Do not manually concatenate the entire conversation into a single prompt unless the task specifically requires prompt-formatting.

## Options and generation controls

Pass Ollama generation settings through `options` rather than inventing SDK-specific top-level parameters.

```python
response = client.chat(
    model="gemma3",
    messages=[{"role": "user", "content": "Generate three names."}],
    options={
        "temperature": 0,
        "num_predict": 256,
    },
)
```

Use low temperature for deterministic extraction/classification tasks. Avoid excessive token limits unless needed.

## Structured outputs

When application code needs reliable machine-readable output, prefer structured outputs over asking for JSON in prose.

Pass a JSON schema through `format` and validate the returned JSON with Pydantic.

```python
from pydantic import BaseModel
from ollama import chat

class Person(BaseModel):
    name: str
    age: int

response = chat(
    model="gemma3",
    messages=[
        {"role": "user", "content": "Extract: Alice is 32 years old."},
    ],
    format=Person.model_json_schema(),
    options={"temperature": 0},
)

person = Person.model_validate_json(response.message.content)
print(person.name, person.age)
```

Rules:

1. Define a strict Pydantic model for the expected response.
2. Pass `Model.model_json_schema()` to `format`.
3. Prefer `temperature=0` for extraction/classification.
4. Validate with `model_validate_json()` rather than trusting the model output.
5. Handle validation failures explicitly.

JSON mode is also available with `format="json"`, but a JSON schema is preferred when the output shape is known.

Important: Ollama's current cloud documentation states that structured outputs are not supported by Ollama Cloud. Do not assume a schema-based response will work against a cloud endpoint; detect/handle that case or use a supported local model.

## Tool / function calling

Use tools when the model needs to request application actions or retrieve external state.

The Python SDK can accept Python functions directly and derive the tool schema from their type hints and docstrings.

```python
from ollama import chat


def get_temperature(city: str) -> str:
    """Get the current temperature for a city.

    Args:
        city: City name.
    """
    temperatures = {
        "Melbourne": "15°C",
        "Sydney": "18°C",
    }
    return temperatures.get(city, "Unknown")

messages = [
    {"role": "user", "content": "What's the temperature in Melbourne?"},
]

response = chat(
    model="qwen3",
    messages=messages,
    tools=[get_temperature],
)

messages.append(response.message)

if response.message.tool_calls:
    for call in response.message.tool_calls:
        if call.function.name == "get_temperature":
            result = get_temperature(**call.function.arguments)
            messages.append({
                "role": "tool",
                "tool_name": call.function.name,
                "content": str(result),
            })

    final_response = chat(
        model="qwen3",
        messages=messages,
        tools=[get_temperature],
    )
    print(final_response.message.content)
```

Tool-calling rules:

- Always inspect `response.message.tool_calls` before assuming there is normal text output.
- Append the assistant message containing tool calls back into `messages`.
- Execute only tools explicitly registered by the application.
- Validate tool arguments before executing side effects.
- Append each tool result with role `tool` and the matching `tool_name`.
- Call the model again with the updated message history to produce the final answer.
- Support multiple tool calls; do not assume only one call unless the selected model is known to produce a single call.
- Never allow model-generated tool arguments to bypass application authorization or safety checks.

## Thinking

For models that support thinking, `think` may be enabled as `True` or with supported levels such as `low`, `medium`, or `high`.

```python
response = chat(
    model="qwen3",
    messages=[{"role": "user", "content": "Solve this problem."}],
    think="medium",
)
```

Only enable thinking when the target model supports it and the extra reasoning latency/token use is useful.

## Multimodal input

For models with vision/multimodal support, include images with the message in the format expected by the SDK/model.

```python
from ollama import chat

response = chat(
    model="gemma3",
    messages=[
        {
            "role": "user",
            "content": "Describe this image.",
            "images": ["/path/to/image.jpg"],
        }
    ],
)

print(response.message.content)
```

Verify that the selected model supports vision before relying on image input. Do not assume every Ollama model accepts images.

## Generate API

Use `generate()` for a standalone prompt rather than maintaining chat history.

```python
from ollama import generate

response = generate(
    model="gemma3",
    prompt="Write a one-sentence summary of Docker.",
)

print(response.response)
```

Use chat when roles, history, tools, or multimodal messages are important.

## Embeddings

Use `embed()` for vector embeddings.

```python
from ollama import embed

response = embed(
    model="embeddinggemma",
    input=[
        "Document one",
        "Document two",
    ],
)

vectors = response.embeddings
```

For RAG/search systems:

- Use the same embedding model for indexing and querying.
- Store the vector dimensions expected by the selected model.
- Batch inputs when appropriate.
- Keep source text and metadata alongside vectors.
- Do not compare embeddings from unrelated embedding models without an explicit migration strategy.

## Model management

The SDK exposes model lifecycle operations including:

```python
import ollama

ollama.list()
ollama.show("gemma3")
ollama.pull("gemma3")
ollama.create(model="my-model", from_="gemma3", system="You are helpful.")
ollama.copy("gemma3", "my-model-copy")
ollama.delete("my-model-copy")
ollama.push("user/my-model")
ollama.ps()
```

Do not automatically pull large models in normal application startup unless that behaviour is intentional. Model downloads can be large and slow.

## Cloud endpoints

For Ollama Cloud/API access, configure a `Client` with the cloud host and authentication headers as documented by Ollama.

```python
import os
from ollama import Client

client = Client(
    host="https://ollama.com",
    headers={
        "Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}",
    },
)
```

Never hard-code API keys. Read them from environment variables or a secret manager.

Do not assume local and cloud capabilities are identical. Check model and feature support before using advanced features such as structured outputs.

## Error handling

The SDK raises `ollama.ResponseError` when a request fails or when a streaming error is detected.

```python
import ollama

try:
    response = ollama.chat(
        model="gemma3",
        messages=[{"role": "user", "content": "Hello"}],
    )
except ollama.ResponseError as exc:
    print(f"Ollama error: {exc.error}")
    print(f"Status: {exc.status_code}")
```

Recommended handling:

- `404`: the model may not exist; decide whether the application should prompt for installation or call `pull()`.
- connection errors: verify Ollama is running and the configured host/port is reachable.
- validation errors: treat model output as untrusted input and surface/retry the parse failure.
- streaming failures: preserve any content already received and report the failure clearly.

Do not catch every exception and silently continue. Distinguish transport, Ollama, tool execution, and output-validation failures.

## Performance guidance

- Reuse `Client`/`AsyncClient` instances.
- Use streaming for interactive long responses.
- Use structured outputs for extraction instead of post-processing free-form prose.
- Use batching for embeddings.
- Keep prompts and conversation history bounded.
- Avoid unnecessary model switching because model loading can dominate latency.
- For local inference, verify that the desired model is actually using the intended GPU/CPU configuration at the Ollama level rather than assuming Python controls hardware placement.

## Security and reliability

Treat model output, tool arguments, file paths, URLs, and structured JSON as untrusted input.

Never:

- execute arbitrary shell commands generated by a model without strict validation;
- let tool calls bypass application permissions;
- expose secrets in prompts or tool results;
- trust a model-generated JSON object without schema validation when correctness matters;
- hard-code API keys or tokens into source code.

For file-processing agents, validate paths against approved roots before opening or writing files.

## Common mistakes

### Calling an async client synchronously

Use `await client.chat(...)` with `AsyncClient`, not a blocking sync pattern.

### Assuming every model supports every feature

Capability varies by model. Check the model's documented support for vision, tool calling, thinking, and structured output before designing around it.

### Forgetting the second tool-call request

A tool call is a request from the model. The application must execute the tool, append the tool result, and make the follow-up model request.

### Treating streamed chunks like complete responses

A streamed chunk is incremental. Concatenate content or process it incrementally.

### Parsing structured output without validation

Use Pydantic or another strict validator before using model-generated data in application logic.

### Pulling models implicitly in every run

Model installation is an operational concern, not normally part of each inference request.

## Recommended project pattern

For an application, isolate Ollama access behind a small service/module instead of scattering raw SDK calls throughout the codebase.

```python
from ollama import Client


class OllamaService:
    def __init__(self, host: str = "http://localhost:11434") -> None:
        self.client = Client(host=host)

    def chat(self, model: str, messages: list[dict]) -> str:
        response = self.client.chat(model=model, messages=messages)
        return response.message.content
```

This makes it easier to add retries, logging, model selection, timeouts, structured-output validation, and testing without changing every caller.

## Agent decision rules

When implementing an Ollama feature:

1. Prefer `chat()` for conversational/task-oriented interactions.
2. Prefer `generate()` for a single prompt with no conversation state.
3. Use `Client` for reusable synchronous configuration.
4. Use `AsyncClient` for async applications.
5. Use `stream=True` for interactive/long-running output.
6. Use Pydantic + `format=<json schema>` for reliable structured data.
7. Use Python functions as `tools` for application actions and implement the complete tool-call loop.
8. Use `images` only with a model that supports multimodal input.
9. Use `embed()` for vector embeddings and keep indexing/query models consistent.
10. Handle `ollama.ResponseError` explicitly.
11. Verify capability and model availability instead of assuming a feature is supported.
12. Reuse clients and avoid unnecessary model downloads/loading.

## Reference sources

- Official SDK repository: https://github.com/ollama/ollama-python
- Official SDK README: https://github.com/ollama/ollama-python/blob/main/README.md
- Official SDK examples: https://github.com/ollama/ollama-python/tree/main/examples
- Ollama API documentation: https://github.com/ollama/ollama/blob/main/docs/api.md
- Tool calling: https://github.com/ollama/ollama/blob/main/docs/capabilities/tool-calling.mdx
- Structured outputs: https://github.com/ollama/ollama/blob/main/docs/capabilities/structured-outputs.mdx
