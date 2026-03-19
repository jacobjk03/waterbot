from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from anthropic import Anthropic

from .base import ModelAdapter


class ClaudeAdapter(ModelAdapter):
    """
    Claude implementation that matches the existing OpenAIAdapter interface.

    Notes:
    - Claude provides chat/completions, but not embeddings in the same way OpenAI does.
      For pgvector embeddings, we still rely on `langchain_openai.OpenAIEmbeddings`
      (same as the current codebase).
    """

    def __init__(self, model_id: str = "claude-sonnet-4-6", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_id = model_id

        api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            # Don't hard-fail at import-time; fail when the model is actually called.
            # This keeps unit tests and non-LLM code paths from crashing.
            self._api_key_missing = True
            self.client = None
        else:
            self._api_key_missing = False
            self.client = Anthropic(api_key=api_key)

        self._embeddings = None

    def get_embeddings(self) -> Any:
        """
        Return an embedding function for pgvector.

        The project currently uses OpenAI embeddings for pgvector ingestion.
        """
        if self._embeddings is None:
            from langchain_openai import OpenAIEmbeddings

            self._embeddings = OpenAIEmbeddings()
        return self._embeddings

    def _ensure_client(self) -> Anthropic:
        if self.client is not None:
            return self.client
        # Fail lazily so imports/tests work even if keys are absent.
        api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "CLAUDE_API_KEY/ANTHROPIC_API_KEY is required to call Claude."
            )
        self.client = Anthropic(api_key=api_key)
        return self.client

    async def generate_llm_payload(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ):
        return json.dumps(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )

    def _convert_to_anthropic_messages(self, messages: list[dict[str, str]]):
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, str]] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(content)
            elif role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": content})
            else:
                # Unknown roles are ignored (shouldn't happen in our prompt builder).
                continue

        system_prompt = "\n\n".join(system_parts).strip() or None
        return system_prompt, anthropic_messages

    async def get_llm_detailed_body(
        self,
        kb_data: str,
        user_query: str,
        bot_response: str,
        max_tokens: int = 512,
        temperature: float = 0.5,
        language: str = "en",
    ):
        system_prompt = await self.get_chat_detailed_prompt(kb_data, language=language)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        inject_user_query = "<NEXTSTEPS_REQUEST>Provide me the action items<NEXTSTEPS_REQUEST>"
        messages = await self.build_message_chain_for_action(
            user_query=user_query,
            bot_response=bot_response,
            inject_user_query=inject_user_query,
            messages=messages,
        )

        llm_payload = await self.generate_llm_payload(
            messages=messages, temperature=temperature, max_tokens=max_tokens
        )
        return llm_payload

    async def get_llm_nextsteps_body(
        self,
        kb_data: str,
        user_query: str,
        bot_response: str,
        max_tokens: int = 512,
        temperature: float = 0.5,
        language: str = "en",
    ):
        system_prompt = await self.get_action_item_prompt(kb_data, language=language)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        inject_user_query = "<NEXTSTEPS_REQUEST>Provide me the action items<NEXTSTEPS_REQUEST>"
        messages = await self.build_message_chain_for_action(
            user_query=user_query,
            bot_response=bot_response,
            inject_user_query=inject_user_query,
            messages=messages,
        )

        llm_payload = await self.generate_llm_payload(
            messages=messages, temperature=temperature, max_tokens=max_tokens
        )
        return llm_payload

    async def get_llm_body(
        self,
        kb_data: str,
        chat_history: list[dict[str, str]],
        max_tokens: int = 512,
        temperature: float = 0.5,
        endpoint_type: str = "default",
    ):
        # System prompt based on endpoint type
        if endpoint_type == "riverbot":
            system_prompt = "You are River. Answer as a river would."
        elif endpoint_type == "spanish":
            system_prompt = f"""
        Eres una asistente amable llamada Blue que ofrece información sobre el agua en Arizona.

        Responde siempre en español (registro neutral) y adapta los ejemplos a residentes de Arizona.

        Cuando te pregunten por nombres de funcionarios electos, excepto la gobernadora, responde: "La información más actualizada sobre los funcionarios electos está disponible en az.gov."

        Evita incluir información irrelevante o especulativa.

        Utiliza el siguiente conocimiento para responder las preguntas:
        <knowledge>
        {kb_data}
        </knowledge>

        Responde en 150 palabras o menos con un tono cercano, sin usar listas.
        En respuestas más largas, separa los párrafos con saltos de línea y agrega un salto adicional antes de la frase de cierre.
        Al final de cada mensaje incluye:

        "¡Me encantaría contarte más! Solo haz clic en los botones de abajo o haz una pregunta de seguimiento."
        """
        else:
            system_prompt = f"""
        You are a helpful assistant named Blue that provides information about water in Arizona.

        You will be provided with Arizona water-related queries.

        For any other inquiries regarding the names of elected officials excluding the name of the governor, you should respond: 'The most current information on the names of elected officials is available at az.gov.'

        Verify not to include any information that is irrelevant to the current query.

        Use the following knowledge to answer questions:
        <knowledge>
        {kb_data}
        </knowledge>

        You should answer in 150 words or less in a friendly tone and include details within the word limit.
        Avoid lists.

        For longer responses (2 sentences), please separate each paragraph with a line break to improve readability. Additionally, add a line break before the closing line.

        At the end of each message, please include -

        "I would love to tell you more! Just click the buttons below or ask a follow-up question."
        """

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        for message in chat_history:
            messages.append(message)

        llm_payload = await self.generate_llm_payload(
            messages=messages, temperature=temperature, max_tokens=max_tokens
        )
        return llm_payload

    async def generate_response(self, llm_body: str) -> str:
        llm_body_obj = json.loads(llm_body)
        messages = llm_body_obj["messages"]
        temperature = llm_body_obj.get("temperature", 0.5)
        max_tokens = int(llm_body_obj.get("max_tokens", 512))

        system_prompt, anthropic_messages = self._convert_to_anthropic_messages(messages)
        client = self._ensure_client()

        response = await asyncio.to_thread(
            lambda: client.messages.create(
                model=self.model_id,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=anthropic_messages,
            )
        )

        # Anthropic returns a list of content blocks; for chat it's usually text blocks.
        response_text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                response_text_parts.append(block_text)
        response_body = "".join(response_text_parts).strip()

        response_content = re.sub(r"\n", "<br>", response_body)
        return response_content

    async def safety_checks(self, user_query: str):
        """
        Bypass safety checks by returning safe, default values.

        (The current codebase already bypasses moderation/intent checks.)
        """
        moderation_result = False
        intent_result = json.dumps(
            {
                "user_intent": 0,
                "prompt_injection": 0,
                "unrelated_topic": 0,
            }
        )
        return moderation_result, intent_result

