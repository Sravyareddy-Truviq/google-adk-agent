"""
Google ADK Banking Agent
- Connects to shared MCP server using HTTP+SSE transport
- Authenticates with X-Agent-Key header (GOOGLEADK_AGENT_KEY)
- Exposes /chat endpoint for the frontend
- Validates Supabase JWT from frontend
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import dns_patch  # noqa: F401 — patches socket before httpx/supabase open connections

import os
import json
import asyncio
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseServerParams
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

# ── Config ──────────────────────────────────────────────────────────────────
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
AGENT_KEY = os.environ.get("GOOGLEADK_AGENT_KEY", "4744fed74ea318b429f0fb0047842891")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://huhdpxvsdxnsfsqxwpux.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

app = FastAPI(title="Google ADK Banking Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── Session store ────────────────────────────────────────────────────────────
session_service = InMemorySessionService()
APP_NAME = "banking-adk"

# ── Supabase JWT verification ────────────────────────────────────────────────
async def verify_supabase_jwt(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Supabase token")
    return resp.json()


# ── Build agent (created fresh per request to reuse MCP toolset) ─────────────
def get_toolset() -> MCPToolset:
    return MCPToolset(
        connection_params=SseServerParams(
            url=f"{MCP_SERVER_URL}/mcp/sse",
            headers={"X-Agent-Key": AGENT_KEY},
        )
    )


def build_agent() -> tuple[LlmAgent, MCPToolset]:
    toolset = get_toolset()
    agent = LlmAgent(
        model="gemini-2.5-flash",
        name="banking_adk_agent",
        instruction=(
            "You are a professional banking assistant. Help users check balances, "
            "transfer funds, view transaction history, pay bills, open/close accounts, "
            "check loan eligibility, and convert currencies. "
            "Be concise, accurate, and always confirm destructive actions."
        ),
        tools=[toolset],
    )
    return agent, toolset


GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip("\"'")
GROQ_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "openai/gpt-oss-120b").strip("\"'")
if "llama-3.3" in GROQ_MODEL:
    GROQ_MODEL = "openai/gpt-oss-120b"


async def run_groq_fallback(message: str) -> str:
    if not GROQ_API_KEY:
        return "Google Gemini is currently unavailable and Groq fallback is not configured."
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY)

    # 1. Fetch available tools from MCP server
    async with httpx.AsyncClient() as client:
        t_resp = await client.post(
            f"{MCP_SERVER_URL}/mcp/tools/list",
            headers={"X-Agent-Key": AGENT_KEY},
            json={"jsonrpc": "2.0", "id": 1}
        )
        tools_data = t_resp.json().get("result", {}).get("tools", [])

    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}})
            }
        }
        for t in tools_data
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are a professional banking assistant. Help users check balances, "
                "transfer funds, view transaction history, pay bills, open/close accounts, "
                "check loan eligibility, and convert currencies. Be concise and confirm before transfers."
            ),
        },
        {"role": "user", "content": message}
    ]

    resp = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=GROQ_MODEL,
        messages=messages,
        tools=openai_tools or None,
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        return msg.content or "I couldn't process that request."

    # Execute every tool call via MCP server (each tool_call id needs a tool reply)
    messages.append(msg)
    async with httpx.AsyncClient() as client:
        for tool_call in msg.tool_calls:
            c_resp = await client.post(
                f"{MCP_SERVER_URL}/mcp/tools/call",
                headers={"X-Agent-Key": AGENT_KEY},
                json={"jsonrpc": "2.0", "id": 1, "params": {
                    "name": tool_call.function.name,
                    "arguments": json.loads(tool_call.function.arguments)}},
            )
            tool_output = c_resp.json().get("result", {}).get("content", [{}])[0].get("text", "")
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_output})

    final_resp = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=GROQ_MODEL,
        messages=messages,
    )
    return final_resp.choices[0].message.content or "Completed."


# ── Request/response models ───────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    model: str = "auto"  # "auto" (Gemini with Groq fallback), "gemini", or "groq"


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "agent": "google-adk"}


@app.post("/chat")
async def chat(
    body: ChatRequest,
    user: dict = Depends(verify_supabase_jwt),
):
    user_id = user.get("id", "anon")
    session_id = f"{user_id}_{body.session_id}"

    # Explicit Groq selection
    if body.model == "groq":
        reply = await run_groq_fallback(body.message)
        return {"reply": reply}

    agent, toolset = build_agent()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    try:
        # Ensure session exists
        sess = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        if not sess:
            await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

        user_msg = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=body.message)]
        )

        for attempt in range(2):
            try:
                reply_parts = []
                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=user_msg,
                ):
                    if event.is_final_response() and event.content:
                        for part in event.content.parts:
                            if part.text:
                                reply_parts.append(part.text)

                return {"reply": " ".join(reply_parts) or "I couldn't process that request."}
            except Exception as e:
                err_str = str(e)
                if attempt == 0 and ("503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str):
                    await asyncio.sleep(1.5)
                    continue
                # On Gemini 503 or failure, fall back to Groq unless Gemini was explicitly requested
                if GROQ_API_KEY and body.model != "gemini":
                    print(f"[googleadk] Gemini capacity error ({err_str[:40]}), falling back to Groq...")
                    fallback_reply = await run_groq_fallback(body.message)
                    return {"reply": fallback_reply}
                raise
    except Exception as err:
        if GROQ_API_KEY and body.model != "gemini":
            return {"reply": await run_groq_fallback(body.message)}
        return {"reply": f"Agent error: {err}"}
    finally:
        await toolset.close()
