import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, Iterator, Type, Union, cast

import anthropic
from anthropic import NOT_GIVEN
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from fireworks.client import Fireworks
from google import genai  # type: ignore[attr-defined]
from google.genai.types import (
    Content,
    FunctionDeclaration,
    GenerateContentConfig,
    GenerateContentResponse,
    GoogleSearch,
    Part,
)
from google.genai.types import Tool as GeminiTool
from langfuse.decorators import langfuse_context, observe
from langfuse.openai import openai
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
    ChatCompletionReasoningEffort,
)

from .models import (
    LlmGenerateStructuredResponse,
    LlmGenerateTextResponse,
    LlmModelDefaultConfig,
    LlmProviderDefaults,
    LlmProviderType,
    LlmRefusalError,
    LlmResponseMetadata,
    Message,
    StructuredOutputType,
    ToolCall,
    Usage,
)
from .tools import FunctionTool
from .utils import backoff_on_exception

logger = logging.getLogger(__name__)


@dataclass
class OpenAiProvider:
    model_name: str
    provider_name = LlmProviderType.OPENAI
    defaults: LlmProviderDefaults | None = None

    default_configs: ClassVar[list[LlmModelDefaultConfig]] = [
        LlmModelDefaultConfig(
            match=r"^o1-mini.*",
            defaults=LlmProviderDefaults(temperature=1.0),
        ),
        LlmModelDefaultConfig(
            match=r"^o1-preview.*",
            defaults=LlmProviderDefaults(temperature=1.0),
        ),
        LlmModelDefaultConfig(
            match=r"^o3-mini.*",
            defaults=LlmProviderDefaults(temperature=1.0),
        ),
        LlmModelDefaultConfig(
            match=r".*",
            defaults=LlmProviderDefaults(temperature=0.0),
        ),
    ]

    @staticmethod
    def get_client() -> openai.Client:
        return openai.Client(max_retries=4)

    @classmethod
    def model(cls, model_name: str) -> "OpenAiProvider":
        model_config = cls._get_config(model_name)
        return cls(
            model_name=model_name,
            defaults=model_config.defaults if model_config else None,
        )

    @classmethod
    def _get_config(cls, model_name: str):
        for config in cls.default_configs:
            if re.match(config.match, model_name):
                return config
        return None

    @staticmethod
    def is_completion_exception_retryable(exception: Exception) -> bool:
        return isinstance(exception, openai.InternalServerError)

    def generate_text(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        predicted_output: str | None = None,
        reasoning_effort: str | None = None,
    ):
        message_dicts, tool_dicts = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )

        openai_client = self.get_client()

        completion = openai_client.chat.completions.create(
            model=self.model_name,
            messages=cast(Iterable[ChatCompletionMessageParam], message_dicts),
            temperature=temperature,
            tools=(
                cast(Iterable[ChatCompletionToolParam], tool_dicts)
                if tool_dicts
                else openai.NotGiven()
            ),
            max_tokens=max_tokens or openai.NotGiven(),
            timeout=timeout or openai.NotGiven(),
            prediction=(
                {
                    "type": "content",
                    "content": predicted_output,
                }
                if predicted_output
                else openai.NotGiven()
            ),
            reasoning_effort=(
                cast(ChatCompletionReasoningEffort, reasoning_effort)
                if reasoning_effort
                else openai.NotGiven()
            ),
        )

        openai_message = completion.choices[0].message
        if openai_message.refusal:
            raise LlmRefusalError(completion.choices[0].message.refusal)

        message = Message(
            content=openai_message.content,
            role=openai_message.role,
            tool_calls=(
                [
                    ToolCall(
                        id=call.id,
                        function=call.function.name,
                        args=call.function.arguments,
                    )
                    for call in openai_message.tool_calls
                ]
                if openai_message.tool_calls
                else None
            ),
        )

        usage = Usage(
            completion_tokens=(
                completion.usage.completion_tokens if completion.usage else 0
            ),
            prompt_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            total_tokens=completion.usage.total_tokens if completion.usage else 0,
        )

        return LlmGenerateTextResponse(
            message=message,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )

    def generate_structured(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        response_format: Type[StructuredOutputType],
        max_tokens: int | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
    ) -> LlmGenerateStructuredResponse[StructuredOutputType]:
        message_dicts, tool_dicts = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )

        openai_client = self.get_client()

        completion = openai_client.beta.chat.completions.parse(
            model=self.model_name,
            messages=cast(Iterable[ChatCompletionMessageParam], message_dicts),
            temperature=temperature,
            tools=(
                cast(Iterable[ChatCompletionToolParam], tool_dicts)
                if tool_dicts
                else openai.NotGiven()
            ),
            response_format=response_format,
            max_tokens=max_tokens or openai.NotGiven(),
            timeout=timeout or openai.NotGiven(),
            reasoning_effort=(
                cast(ChatCompletionReasoningEffort, reasoning_effort)
                if reasoning_effort
                else openai.NotGiven()
            ),
        )

        openai_message = completion.choices[0].message
        if openai_message.refusal:
            raise LlmRefusalError(completion.choices[0].message.refusal)

        parsed = cast(StructuredOutputType, completion.choices[0].message.parsed)

        usage = Usage(
            completion_tokens=(
                completion.usage.completion_tokens if completion.usage else 0
            ),
            prompt_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            total_tokens=completion.usage.total_tokens if completion.usage else 0,
        )

        return LlmGenerateStructuredResponse(
            parsed=parsed,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )

    @staticmethod
    def to_message_dict(message: Message) -> ChatCompletionMessageParam:
        message_dict: dict[str, Any] = {
            "content": message.content if message.content else "",
            "role": message.role,
        }

        if message.tool_calls:
            tool_calls = [
                tool_call.model_dump(mode="json") for tool_call in message.tool_calls
            ]
            parsed_tool_calls = []
            for item in tool_calls:
                new_item = item.copy()
                new_item["function"] = {
                    "name": item["function"],
                    "arguments": item["args"],
                }
                new_item["type"] = "function"
                parsed_tool_calls.append(new_item)
            message_dict["tool_calls"] = parsed_tool_calls
            message_dict["role"] = "assistant"

        if message.tool_call_id:
            message_dict["tool_call_id"] = message.tool_call_id

        return cast(ChatCompletionMessageParam, message_dict)

    @staticmethod
    def to_tool_dict(tool: FunctionTool) -> ChatCompletionToolParam:
        return ChatCompletionToolParam(
            type="function",
            function={
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        param["name"]: {
                            key: value
                            for key, value in {
                                "type": param["type"],
                                "description": param.get("description", ""),
                                "items": param.get("items"),
                            }.items()
                            if value is not None
                        }
                        for param in tool.parameters
                    },
                    "required": tool.required,
                },
            },
        )

    @classmethod
    def _prep_message_and_tools(
        cls,
        *,
        messages: list[Message] | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        reasoning_effort: str | None = None,
    ):
        message_dicts = (
            [cls.to_message_dict(message) for message in messages] if messages else []
        )
        if system_prompt:
            message_dicts.insert(
                0,
                cls.to_message_dict(
                    Message(
                        role="system" if not reasoning_effort else "developer",
                        content=system_prompt,
                    )
                ),
            )
        if prompt:
            message_dicts.append(
                cls.to_message_dict(Message(role="user", content=prompt))
            )

        tool_dicts = (
            [cls.to_tool_dict(tool) for tool in tools]
            if tools and len(tools) > 0
            else None
        )

        return message_dicts, tool_dicts

    @observe(as_type="generation", name="OpenAI Stream")
    def generate_text_stream(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[str | ToolCall | Usage]:
        message_dicts, tool_dicts = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )

        openai_client = self.get_client()

        stream = openai_client.chat.completions.create(
            model=self.model_name,
            messages=cast(Iterable[ChatCompletionMessageParam], message_dicts),
            temperature=temperature,
            tools=(
                cast(Iterable[ChatCompletionToolParam], tool_dicts)
                if tool_dicts
                else openai.NotGiven()
            ),
            max_tokens=max_tokens or openai.NotGiven(),
            timeout=timeout or openai.NotGiven(),
            stream=True,
            stream_options={"include_usage": True},
            reasoning_effort=(
                cast(ChatCompletionReasoningEffort, reasoning_effort)
                if reasoning_effort
                else openai.NotGiven()
            ),
        )  # type: ignore

        try:
            current_tool_call: dict[str, Any] | None = None
            current_tool_call_index = 0

            for chunk in stream:
                if not chunk.choices and chunk.usage:
                    usage = Usage(
                        completion_tokens=chunk.usage.completion_tokens,
                        prompt_tokens=chunk.usage.prompt_tokens,
                        total_tokens=chunk.usage.total_tokens,
                    )
                    yield usage
                    langfuse_context.update_current_observation(
                        model=self.model_name, usage=usage
                    )
                    break

                delta = chunk.choices[0].delta
                if delta.tool_calls:
                    tool_call = delta.tool_calls[0]

                    if (
                        not current_tool_call
                        or current_tool_call_index != tool_call.index
                    ):  # Start of new tool call
                        current_tool_call_index = tool_call.index
                        if current_tool_call:
                            yield ToolCall(**current_tool_call)
                        current_tool_call = None
                        current_tool_call = {
                            "id": tool_call.id,
                            "function": (
                                tool_call.function.name
                                if tool_call.function and tool_call.function.name
                                else ""
                            ),
                            "args": (
                                tool_call.function.arguments
                                if tool_call.function and tool_call.function.arguments
                                else ""
                            ),
                        }
                    else:
                        if tool_call.function and tool_call.function.arguments:
                            current_tool_call["args"] += tool_call.function.arguments
                if chunk.choices[0].finish_reason == "tool_calls" and current_tool_call:
                    yield ToolCall(**current_tool_call)
                if delta.content:
                    yield delta.content
        finally:
            stream.response.close()

    def construct_message_from_stream(
        self, content_chunks: list[str], tool_calls: list[ToolCall]
    ) -> Message:
        return Message(
            role="assistant",
            content="".join(content_chunks) if content_chunks else None,
            tool_calls=tool_calls if tool_calls else None,
        )


@dataclass
class AnthropicProvider:
    model_name: str
    provider_name = LlmProviderType.ANTHROPIC
    defaults: LlmProviderDefaults | None = None

    default_configs: ClassVar[list[LlmModelDefaultConfig]] = [
        LlmModelDefaultConfig(
            match=r".*",
            defaults=LlmProviderDefaults(temperature=0.0),
        ),
    ]

    @staticmethod
    def get_client() -> anthropic.AnthropicVertex:
        return anthropic.AnthropicVertex(
            project_id=os.getenv("GOOGLE_CLOUD_PROJECT"),  # type: ignore
            region="europe-west1" if os.getenv("USE_EU_REGION") else "us-east5",
            max_retries=8,
        )

    @classmethod
    def model(cls, model_name: str) -> "AnthropicProvider":
        model_config = cls._get_config(model_name)
        return cls(
            model_name=model_name,
            defaults=model_config.defaults if model_config else None,
        )

    @classmethod
    def _get_config(cls, model_name: str):
        for config in cls.default_configs:
            if re.match(config.match, model_name):
                return config
        return None

    @staticmethod
    def is_completion_exception_retryable(exception: Exception) -> bool:
        return isinstance(exception, anthropic.AnthropicError) and (
            "overloaded_error" in str(exception)
        )

    @observe(as_type="generation", name="Anthropic Generation")
    def generate_text(
        self,
        *,
        messages: list[Message] | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ):
        message_dicts, tool_dicts, system_prompt_block = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

        anthropic_client = self.get_client()

        completion = anthropic_client.messages.create(
            system=system_prompt_block or NOT_GIVEN,
            model=self.model_name,
            tools=cast(Iterable[ToolParam], tool_dicts) if tool_dicts else NOT_GIVEN,
            messages=cast(Iterable[MessageParam], message_dicts),
            max_tokens=max_tokens or 8192,
            temperature=temperature or NOT_GIVEN,
            timeout=timeout or NOT_GIVEN,
        )

        message = self._format_claude_response_to_message(completion)

        usage = Usage(
            completion_tokens=completion.usage.output_tokens,
            prompt_tokens=completion.usage.input_tokens,
            total_tokens=completion.usage.input_tokens + completion.usage.output_tokens,
        )

        langfuse_context.update_current_observation(model=self.model_name, usage=usage)

        return LlmGenerateTextResponse(
            message=message,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )

    @staticmethod
    def _format_claude_response_to_message(
        completion: anthropic.types.Message,
    ) -> Message:
        message = Message(role=completion.role)
        for block in completion.content:
            if block.type == "text":
                message.content = (
                    block.text
                )  # we're assuming there's only one text block per message
            elif block.type == "tool_use":
                if not message.tool_calls:
                    message.tool_calls = []
                message.tool_calls.append(
                    ToolCall(
                        id=block.id, function=block.name, args=json.dumps(block.input)
                    )
                )
                message.role = "tool_use"
                message.tool_call_id = message.tool_calls[
                    0
                ].id  # assumes we get only 1 tool call at a time, but we really don't use this field for tool_use blocks
        return message

    @staticmethod
    def to_message_param(message: Message) -> MessageParam:
        if message.role == "tool":
            return MessageParam(
                role="user",
                content=[
                    ToolResultBlockParam(
                        type="tool_result",
                        content=message.content or "",
                        tool_use_id=message.tool_call_id or "",
                    )
                ],
            )
        elif message.role == "tool_use" or (
            message.role == "assistant" and message.tool_calls
        ):
            if not message.tool_calls:
                return MessageParam(role="assistant", content=[])
            tool_call = message.tool_calls[0]  # Assuming only one tool call per message
            return MessageParam(
                role="assistant",
                content=[
                    ToolUseBlockParam(
                        type="tool_use",
                        id=tool_call.id or "",
                        name=tool_call.function,
                        input=json.loads(tool_call.args),
                    )
                ],
            )
        else:
            return MessageParam(
                role=message.role,  # type: ignore
                content=[TextBlockParam(type="text", text=message.content or "")],
            )

    @staticmethod
    def to_tool_dict(tool: FunctionTool) -> ToolParam:
        return ToolParam(
            name=tool.name,
            description=tool.description,
            input_schema={
                "type": "object",
                "properties": {
                    param["name"]: {
                        key: value
                        for key, value in {
                            "type": param["type"],
                            "description": param.get("description", ""),
                            "items": param.get("items"),
                        }.items()
                        if value is not None
                    }
                    for param in tool.parameters
                },
                "required": tool.required,
            },
        )

    @classmethod
    def _prep_message_and_tools(
        cls,
        *,
        messages: list[Message] | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
    ) -> tuple[list[MessageParam], list[ToolParam] | None, list[TextBlockParam] | None]:
        message_dicts = (
            [cls.to_message_param(message) for message in messages] if messages else []
        )
        if prompt:
            message_dicts.append(
                cls.to_message_param(Message(role="user", content=prompt))
            )
        if message_dicts:
            message_dicts[-1]["content"][0]["cache_control"] = {"type": "ephemeral"}  # type: ignore[index]

        tool_dicts = (
            [cls.to_tool_dict(tool) for tool in tools]
            if tools and len(tools) > 0
            else None
        )

        system_prompt_block = (
            [
                TextBlockParam(
                    type="text", text=system_prompt, cache_control={"type": "ephemeral"}
                )
            ]
            if system_prompt
            else None
        )

        return message_dicts, tool_dicts, system_prompt_block

    @observe(as_type="generation", name="Anthropic Stream")
    def generate_text_stream(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[str | ToolCall | Usage]:
        message_dicts, tool_dicts, system_prompt_block = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

        anthropic_client = self.get_client()

        stream = anthropic_client.messages.create(
            system=system_prompt_block or NOT_GIVEN,
            model=self.model_name,
            tools=cast(Iterable[ToolParam], tool_dicts) if tool_dicts else NOT_GIVEN,
            messages=cast(Iterable[MessageParam], message_dicts),
            max_tokens=max_tokens or 8192,
            temperature=temperature or NOT_GIVEN,
            timeout=timeout or NOT_GIVEN,
            stream=True,
        )

        try:
            current_tool_call: dict[str, Any] | None = None
            current_input_json = []
            total_input_tokens = 0
            total_output_tokens = 0

            for chunk in stream:
                if chunk.type == "message_start" and chunk.message.usage:
                    total_input_tokens += chunk.message.usage.input_tokens
                    total_output_tokens += chunk.message.usage.output_tokens
                elif chunk.type == "message_delta" and chunk.usage:
                    total_output_tokens += chunk.usage.output_tokens

                if chunk.type == "message_stop":
                    break
                elif (
                    chunk.type == "content_block_delta"
                    and chunk.delta.type == "text_delta"
                ):
                    yield chunk.delta.text
                elif (
                    chunk.type == "content_block_start"
                    and chunk.content_block.type == "tool_use"
                ):
                    # Start accumulating a new tool call
                    current_tool_call = {
                        "id": chunk.content_block.id,
                        "function": chunk.content_block.name,
                        "args": "",
                    }
                elif (
                    chunk.type == "content_block_delta"
                    and chunk.delta.type == "input_json_delta"
                ):
                    # Accumulate the input JSON
                    if current_tool_call:
                        current_input_json.append(chunk.delta.partial_json)
                elif chunk.type == "content_block_stop" and current_tool_call:
                    # Tool call is complete, yield it
                    current_tool_call["args"] = "".join(current_input_json)
                    yield ToolCall(**current_tool_call)
                    current_tool_call = None
                    current_input_json = []
        finally:
            usage = Usage(
                completion_tokens=total_output_tokens,
                prompt_tokens=total_input_tokens,
                total_tokens=(total_input_tokens or 0) + (total_output_tokens or 0),
            )
            yield usage
            langfuse_context.update_current_observation(
                model=self.model_name, usage=usage
            )
            stream.response.close()

    def construct_message_from_stream(
        self, content_chunks: list[str], tool_calls: list[ToolCall]
    ) -> Message:
        message = Message(
            role="tool_use" if tool_calls else "assistant",
            content="".join(content_chunks) if content_chunks else None,
        )

        if tool_calls:
            message.tool_calls = tool_calls
            message.tool_call_id = tool_calls[0].id

        return message


@dataclass
class GeminiProvider:
    model_name: str
    provider_name = LlmProviderType.GEMINI
    defaults: LlmProviderDefaults | None = None

    default_configs: ClassVar[list[LlmModelDefaultConfig]] = [
        LlmModelDefaultConfig(
            match=r".*",
            defaults=LlmProviderDefaults(temperature=0.0),
        ),
    ]

    @staticmethod
    def get_client() -> genai.Client:
        client = genai.Client(
            vertexai=True,
            location="us-central1",
        )
        # The gemini client currently doesn't have a built-in retry mechanism.
        retrier = backoff_on_exception(
            GeminiProvider.is_completion_exception_retryable, max_tries=4
        )
        client.models.generate_content = retrier(client.models.generate_content)
        return client

    @classmethod
    def model(cls, model_name: str) -> "GeminiProvider":
        model_config = cls._get_config(model_name)
        return cls(
            model_name=model_name,
            defaults=model_config.defaults if model_config else None,
        )

    @classmethod
    def _get_config(cls, model_name: str):
        for config in cls.default_configs:
            if re.match(config.match, model_name):
                return config
        return None

    @observe(as_type="generation", name="Gemini Generation with Grounding")
    def search_the_web(self, prompt: str, temperature: float | None = None) -> str:
        client = self.get_client()
        google_search_tool = GeminiTool(google_search=GoogleSearch())

        response = client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=GenerateContentConfig(
                tools=[google_search_tool],
                response_modalities=["TEXT"],
                temperature=temperature or 0.0,
            ),
        )
        answer = ""
        if (
            response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts
        ):
            for each in response.candidates[0].content.parts:
                if each.text:
                    answer += each.text
        return answer

    @staticmethod
    def is_completion_exception_retryable(exception: Exception) -> bool:
        retryable_errors = ("Resource exhausted. Please try again later.",)
        return any(error in str(exception) for error in retryable_errors)

    @observe(as_type="generation", name="Gemini Generation")
    def generate_structured(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        response_format: Type[StructuredOutputType],
        max_tokens: int | None = None,
    ) -> LlmGenerateStructuredResponse[StructuredOutputType]:
        message_dicts, tool_dicts, system_prompt = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

        client = self.get_client()

        response = client.models.generate_content(
            model=self.model_name,
            contents=message_dicts,  # type: ignore
            config=GenerateContentConfig(
                tools=tool_dicts,  # type: ignore
                response_modalities=["TEXT"],
                temperature=temperature or 0.0,
                response_mime_type="application/json",
                max_output_tokens=max_tokens or 8192,
                response_schema=response_format,
            ),
        )

        usage = (
            Usage(
                completion_tokens=(response.usage_metadata.candidates_token_count or 0),
                prompt_tokens=(response.usage_metadata.prompt_token_count or 0),
                total_tokens=(response.usage_metadata.total_token_count or 0),
            )
            if response.usage_metadata
            else Usage()
        )
        langfuse_context.update_current_observation(model=self.model_name, usage=usage)

        return LlmGenerateStructuredResponse(
            parsed=response.parsed,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )  # type: ignore

    @observe(as_type="generation", name="Gemini Stream")
    def generate_text_stream(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str | ToolCall | Usage]:
        message_dicts, tool_dicts, system_prompt = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

        client = self.get_client()

        total_prompt_tokens = 0
        total_completion_tokens = 0

        try:
            stream = client.models.generate_content_stream(
                model=self.model_name,
                contents=message_dicts,  # type: ignore
                config=GenerateContentConfig(
                    tools=tool_dicts,  # type: ignore
                    system_instruction=system_prompt,
                    response_modalities=["TEXT"],
                    temperature=temperature or 0.0,
                    max_output_tokens=max_tokens or 8192,
                ),
            )

            current_tool_call: dict[str, Any] | None = None

            for chunk in stream:
                # Handle function calls
                if chunk.candidates[0].content.parts[0].function_call:
                    function_call = chunk.candidates[0].content.parts[0].function_call
                    if not current_tool_call:
                        current_tool_call = {
                            "id": str(
                                hash(function_call.name + str(function_call.args))
                            ),
                            "function": function_call.name,
                            "args": json.dumps(function_call.args),
                        }
                        yield ToolCall(**current_tool_call)
                        current_tool_call = None
                # Handle text chunks
                elif chunk.text:
                    yield chunk.text

                # Update token counts if available
                if chunk.usage_metadata:
                    if chunk.usage_metadata.prompt_token_count:
                        total_prompt_tokens = chunk.usage_metadata.prompt_token_count
                    if chunk.usage_metadata.candidates_token_count:
                        total_completion_tokens = (
                            chunk.usage_metadata.candidates_token_count
                        )

        finally:
            # Yield final usage statistics
            usage = Usage(
                completion_tokens=total_completion_tokens,
                prompt_tokens=total_prompt_tokens,
                total_tokens=total_prompt_tokens + total_completion_tokens,
            )
            yield usage
            langfuse_context.update_current_observation(
                model=self.model_name, usage=usage
            )

    @observe(as_type="generation", name="Gemini Generation")
    def generate_text(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        message_dicts, tool_dicts, system_prompt = self._prep_message_and_tools(
            messages=messages,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
        )

        client = self.get_client()
        response = client.models.generate_content(
            model=self.model_name,
            contents=message_dicts,
            config=GenerateContentConfig(
                tools=tool_dicts,
                system_instruction=system_prompt,
                temperature=temperature or 0.0,
                max_output_tokens=max_tokens or 8192,
            ),
        )

        message = self._format_gemini_response_to_message(response)

        usage = Usage(
            completion_tokens=response.usage_metadata.candidates_token_count or 0,
            prompt_tokens=response.usage_metadata.prompt_token_count or 0,
            total_tokens=response.usage_metadata.total_token_count or 0,
        )

        langfuse_context.update_current_observation(model=self.model_name, usage=usage)

        return LlmGenerateTextResponse(
            message=message,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )

    @classmethod
    def _prep_message_and_tools(
        cls,
        *,
        messages: list[Message] | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
    ) -> tuple[list[Content], list[GeminiTool] | None, str | None]:
        contents: list[Content] = []

        if messages:
            # Group consecutive tool messages together
            grouped_messages: list[list[Message]] = []
            current_group: list[Message] = []

            for message in messages:
                if message.role == "tool":
                    current_group.append(message)
                else:
                    if current_group:
                        grouped_messages.append(current_group)
                        current_group = []
                    grouped_messages.append([message])

            if current_group:
                grouped_messages.append(current_group)

            # Convert each group into a Content object
            for group in grouped_messages:
                if len(group) == 1 and group[0].role != "tool":
                    contents.append(cls.to_content(group[0]))
                elif group[0].role == "tool":
                    # Combine multiple tool messages into a single Content
                    parts = [
                        Part.from_function_response(
                            name=msg.tool_call_function or "",
                            response={"response": msg.content},
                        )
                        for msg in group
                    ]
                    contents.append(Content(role="user", parts=parts))

        if prompt:
            contents.append(
                Content(
                    role="user",
                    parts=[Part(text=prompt)],
                )
            )

        processed_tools = [cls.to_tool(tool) for tool in tools] if tools else []

        return contents, processed_tools, system_prompt

    @staticmethod
    def to_content(message: Message) -> Content:
        if message.role == "tool_use" or (
            message.role == "assistant" and message.tool_calls
        ):
            if not message.tool_calls:
                return Content(
                    role="model",
                    parts=[Part(text=message.content or "")],
                )

            parts = []
            if message.content:
                parts.append(Part(text=message.content))
            for tool_call in message.tool_calls:
                parts.append(
                    Part.from_function_call(
                        name=tool_call.function,
                        args=json.loads(tool_call.args),
                    )
                )
            return Content(role="model", parts=parts)

        elif message.role == "assistant":
            return Content(
                role="model",
                parts=[Part(text=message.content or "")],
            )
        else:
            return Content(
                role="user",
                parts=[Part(text=message.content or "")],
            )

    @staticmethod
    def to_tool(tool: FunctionTool) -> GeminiTool:
        return GeminiTool(
            function_declarations=[
                FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters={
                        "type": "OBJECT",
                        "properties": {
                            param["name"]: {
                                key: value
                                for key, value in {
                                    "type": param["type"].upper(),  # type: ignore
                                    "description": param.get("description", ""),
                                    "items": (
                                        {
                                            **param.get("items", {}),  # type: ignore
                                            "type": param.get("items", {}).get("type", "").upper(),  # type: ignore
                                        }
                                        if param.get("items")
                                        and "type" in param.get("items", {})
                                        else param.get("items")
                                    ),
                                }.items()
                                if value is not None
                            }
                            for param in tool.parameters
                        },
                        "required": tool.required,
                    },
                )
            ],
        )

    def construct_message_from_stream(
        self, content_chunks: list[str], tool_calls: list[ToolCall]
    ) -> Message:
        message = Message(
            role="tool_use" if tool_calls else "assistant",
            content="".join(content_chunks) if content_chunks else None,
        )

        if tool_calls:
            message.tool_calls = tool_calls
            message.tool_call_id = tool_calls[0].id

        return message

    def _format_gemini_response_to_message(
        self, response: GenerateContentResponse
    ) -> Message:
        parts = (
            response.candidates[0].content.parts
            if (
                response.candidates
                and len(response.candidates) > 0
                and response.candidates[0].content
                and response.candidates[0].content.parts
            )
            else []
        )

        message = Message(
            role="assistant",
            content=(parts[0].text if parts and parts[0].text else None),
        )

        for part in parts:
            if part.function_call:
                if not message.tool_calls:
                    message.tool_calls = []
                message.tool_calls.append(
                    ToolCall(
                        id=part.function_call.id,
                        function=part.function_call.name or "",
                        args=json.dumps(part.function_call.args),
                    )
                )
                message.role = "tool_use"
                message.tool_call_id = part.function_call.id
            if part.text:
                message.content = part.text

        return message


@dataclass
class FireworksProvider:
    model_name: str
    provider_name = LlmProviderType.FIREWORKS
    defaults: LlmProviderDefaults | None = None

    default_configs: ClassVar[list[LlmModelDefaultConfig]] = [
        LlmModelDefaultConfig(
            match=r".*",
            defaults=LlmProviderDefaults(temperature=0.0),
        ),
    ]

    @staticmethod
    def get_client() -> Fireworks:
        return Fireworks(api_key=os.environ.get("FIREWORKS_API_KEY"))

    @classmethod
    def model(cls, model_name: str) -> "FireworksProvider":
        model_config = cls._get_config(model_name)
        return cls(
            model_name=model_name,
            defaults=model_config.defaults if model_config else None,
        )

    @classmethod
    def _get_config(cls, model_name: str):
        for config in cls.default_configs:
            if re.match(config.match, model_name):
                return config
        return None

    @staticmethod
    def is_completion_exception_retryable(exception: Exception) -> bool:
        return "overloaded" in str(exception).lower()

    @observe(as_type="generation", name="Fireworks Generation")
    def generate_text(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ):
        message_dicts = []

        if system_prompt:
            message_dicts.append({"role": "system", "content": system_prompt})

        if prompt:
            message_dicts.append({"role": "user", "content": prompt})

        if messages:
            for message in messages:
                message_dict = {"role": message.role, "content": message.content or ""}
                message_dicts.append(message_dict)

        fireworks_client = self.get_client()

        completion = fireworks_client.chat.completions.create(
            model=f"accounts/fireworks/models/{self.model_name}",
            messages=message_dicts,
            temperature=temperature or 0.0,
            max_tokens=max_tokens or 16 * 1024,
            stream=False,
        )

        message = Message(
            content=completion.choices[0].message.content,  # type: ignore
            role=completion.choices[0].message.role,  # type: ignore
        )

        usage = Usage(
            completion_tokens=(
                completion.usage.completion_tokens if completion.usage else 0  # type: ignore
            ),
            prompt_tokens=(
                completion.usage.prompt_tokens if completion.usage else 0  # type: ignore
            ),
            total_tokens=(
                completion.usage.total_tokens if completion.usage else 0  # type: ignore
            ),
        )

        return LlmGenerateTextResponse(
            message=message,
            metadata=LlmResponseMetadata(
                model=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
            ),
        )

    @observe(as_type="generation", name="Fireworks Stream")
    def generate_text_stream(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[str | ToolCall | Usage]:
        message_dicts = []

        if system_prompt:
            message_dicts.append({"role": "system", "content": system_prompt})

        if prompt:
            message_dicts.append({"role": "user", "content": prompt})

        if messages:
            for message in messages:
                message_dict = {"role": message.role, "content": message.content or ""}
                message_dicts.append(message_dict)

        fireworks_client = self.get_client()

        stream = fireworks_client.chat.completions.create(
            model=f"accounts/fireworks/models/{self.model_name}",
            messages=message_dicts,
            temperature=temperature or 0.0,
            max_tokens=max_tokens or 16 * 1024,
            stream=True,
        )

        content = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0

        try:
            for chunk in stream:
                if chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    content += delta
                    yield delta

                if chunk.usage:
                    total_prompt_tokens = chunk.usage.prompt_tokens
                    total_completion_tokens = chunk.usage.completion_tokens

        finally:
            usage = Usage(
                completion_tokens=total_completion_tokens,
                prompt_tokens=total_prompt_tokens,
                total_tokens=total_prompt_tokens + total_completion_tokens,
            )
            yield usage
            langfuse_context.update_current_observation(
                model=self.model_name, usage=usage
            )

    def construct_message_from_stream(
        self, content_chunks: list[str], tool_calls: list[ToolCall]
    ) -> Message:
        return Message(
            role="assistant",
            content="".join(content_chunks) if content_chunks else None,
            tool_calls=tool_calls if tool_calls else None,
        )


LlmProvider = Union[
    OpenAiProvider, AnthropicProvider, GeminiProvider, FireworksProvider
]


class LlmClient:
    @observe(name="Generate Text")
    def generate_text(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        model: LlmProvider,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        run_name: str | None = None,
        timeout: float | None = None,
        predicted_output: str | None = None,
        reasoning_effort: str | None = None,
    ) -> LlmGenerateTextResponse:
        try:
            if run_name:
                langfuse_context.update_current_observation(
                    name=run_name + " - Generate Text"
                )

            defaults = model.defaults
            default_temperature = defaults.temperature if defaults else None

            messages = LlmClient.clean_message_content(messages if messages else [])
            if not tools:
                messages = LlmClient.clean_tool_call_assistant_messages(messages)

            if model.provider_name == LlmProviderType.OPENAI:
                model = cast(OpenAiProvider, model)
                return model.generate_text(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                    predicted_output=predicted_output,
                    reasoning_effort=reasoning_effort,
                )
            elif model.provider_name == LlmProviderType.ANTHROPIC:
                model = cast(AnthropicProvider, model)
                return model.generate_text(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                )
            elif model.provider_name == LlmProviderType.GEMINI:
                model = cast(GeminiProvider, model)
                return model.generate_text(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                )
            elif model.provider_name == LlmProviderType.FIREWORKS:
                model = cast(FireworksProvider, model)
                return model.generate_text(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                )
            else:
                raise ValueError(f"Invalid provider: {model.provider_name}")
        except Exception as e:
            logger.exception(
                f"Text generation failed with provider {model.provider_name}: {e}"
            )
            raise e

    @observe(name="Generate Structured")
    def generate_structured(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        model: LlmProvider,
        system_prompt: str | None = None,
        response_format: Type[StructuredOutputType],
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        run_name: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
    ) -> LlmGenerateStructuredResponse[StructuredOutputType]:
        try:
            if run_name:
                langfuse_context.update_current_observation(
                    name=run_name + " - Generate Structured"
                )

            messages = LlmClient.clean_message_content(messages if messages else [])

            if model.provider_name == LlmProviderType.OPENAI:
                model = cast(OpenAiProvider, model)
                messages = LlmClient.clean_tool_call_assistant_messages(messages)
                return model.generate_structured(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    response_format=response_format,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    tools=tools,
                    timeout=timeout,
                    reasoning_effort=reasoning_effort,
                )
            elif model.provider_name == LlmProviderType.ANTHROPIC:
                raise NotImplementedError(
                    "Anthropic structured outputs are not yet supported"
                )
            elif model.provider_name == LlmProviderType.GEMINI:
                model = cast(GeminiProvider, model)
                return model.generate_structured(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    response_format=response_format,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    tools=tools,
                )
            elif model.provider_name == LlmProviderType.FIREWORKS:
                raise NotImplementedError(
                    "Fireworks structured outputs are not yet supported"
                )
            else:
                raise ValueError(f"Invalid provider: {model.provider_name}")
        except Exception as e:
            logger.exception(
                f"Text generation failed with provider {model.provider_name}: {e}"
            )
            raise e

    @observe(name="Generate Text Stream")
    def generate_text_stream(
        self,
        *,
        prompt: str | None = None,
        messages: list[Message] | None = None,
        model: LlmProvider,
        system_prompt: str | None = None,
        tools: list[FunctionTool] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        run_name: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[str | ToolCall | Usage]:
        try:
            if run_name:
                langfuse_context.update_current_observation(
                    name=run_name + " - Generate Text Stream"
                )

            defaults = model.defaults
            default_temperature = defaults.temperature if defaults else None

            messages = LlmClient.clean_message_content(messages if messages else [])
            if not tools:
                messages = LlmClient.clean_tool_call_assistant_messages(messages)

            if model.provider_name == LlmProviderType.OPENAI:
                model = cast(OpenAiProvider, model)
                yield from model.generate_text_stream(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                    reasoning_effort=reasoning_effort,
                )
            elif model.provider_name == LlmProviderType.ANTHROPIC:
                model = cast(AnthropicProvider, model)
                yield from model.generate_text_stream(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                )
            elif model.provider_name == LlmProviderType.GEMINI:
                model = cast(GeminiProvider, model)
                yield from model.generate_text_stream(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                )
            elif model.provider_name == LlmProviderType.FIREWORKS:
                model = cast(FireworksProvider, model)
                yield from model.generate_text_stream(
                    max_tokens=max_tokens,
                    messages=messages,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature or default_temperature,
                    tools=tools,
                    timeout=timeout,
                )
            else:
                raise ValueError(f"Invalid provider: {model.provider_name}")
        except Exception as e:
            logger.exception(
                f"Text stream generation failed with provider {model.provider_name}: {e}"
            )
            raise e

    @observe(name="Generate Text from Web Search")
    def generate_text_from_web_search(
        self,
        *,
        prompt: str,
        model: LlmProvider,
        temperature: float | None = None,
        run_name: str | None = None,
    ) -> str:
        try:
            if run_name:
                langfuse_context.update_current_observation(
                    name=run_name + " - Generate Text"
                )

            defaults = model.defaults
            default_temperature = defaults.temperature if defaults else None

            if model.provider_name == LlmProviderType.GEMINI:
                model = cast(GeminiProvider, model)
                return model.search_the_web(prompt, temperature or default_temperature)
            else:
                raise ValueError(f"Invalid provider: {model.provider_name}")
        except Exception as e:
            logger.exception(
                f"Text generation from web failed with provider {model.provider_name}: {e}"
            )
            raise e

    @staticmethod
    def clean_tool_call_assistant_messages(messages: list[Message]) -> list[Message]:
        new_messages = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                new_messages.append(
                    Message(role="assistant", content=message.content, tool_calls=[])
                )
            elif message.role == "tool":
                new_messages.append(
                    Message(role="user", content=message.content, tool_calls=[])
                )
            elif message.role == "tool_use":
                new_messages.append(
                    Message(role="assistant", content=message.content, tool_calls=[])
                )
            else:
                new_messages.append(message)
        return new_messages

    @staticmethod
    def clean_assistant_messages(messages: list[Message]) -> list[Message]:
        new_messages = []
        for message in messages:
            if message.role == "assistant" or message.role == "tool_use":
                message.content = "."
                new_messages.append(message)
            else:
                new_messages.append(message)
        return new_messages

    @staticmethod
    def clean_message_content(messages: list[Message]) -> list[Message]:
        new_messages = []
        for message in messages:
            if not message.content:
                message.content = "."
            new_messages.append(message)
        return new_messages

    def construct_message_from_stream(
        self,
        content_chunks: list[str],
        tool_calls: list[ToolCall],
        model: LlmProvider,
    ) -> Message:
        if model.provider_name == LlmProviderType.OPENAI:
            model = cast(OpenAiProvider, model)
            return model.construct_message_from_stream(content_chunks, tool_calls)
        elif model.provider_name == LlmProviderType.ANTHROPIC:
            model = cast(AnthropicProvider, model)
            return model.construct_message_from_stream(content_chunks, tool_calls)
        elif model.provider_name == LlmProviderType.GEMINI:
            model = cast(GeminiProvider, model)
            return model.construct_message_from_stream(content_chunks, tool_calls)
        elif model.provider_name == LlmProviderType.FIREWORKS:
            model = cast(FireworksProvider, model)
            return model.construct_message_from_stream(content_chunks, tool_calls)
        else:
            raise ValueError(f"Invalid provider: {model.provider_name}")
