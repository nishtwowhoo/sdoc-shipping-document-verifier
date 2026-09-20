import os
import streamlit as st

try:
  import google.generativeai as genai

  HAS_GEMINI = True
except ImportError:
  HAS_GEMINI = False


def get_gemini_key() -> str:
  if hasattr(st, "secrets") and "GEMINI_API_KEY" in st.secrets:
    return st.secrets["GEMINI_API_KEY"]
  return os.environ.get("GEMINI_API_KEY", "")


def ask_hitl_assistant(
    user_query: str,
    email_id: str,
    email_meta: dict,
    si_text: str,
    bl_text: str,
    pipeline_results: dict,
) -> str:
  api_key = get_gemini_key()

  if not HAS_GEMINI or not api_key:
    return (
        f"💡 **[Mock Copilot Mode — Gemini Key Not Found]**\n\n"
        f"Query for `{email_id}`: \"{user_query}\"\n\n"
        f"*Add `GEMINI_API_KEY` to `.streamlit/secrets.toml` to activate live Gemini responses.*"
    )

  genai.configure(api_key=api_key)

  system_prompt = f"""
    You are an expert Shipping Operations AI Copilot assisting human operators in inspecting flagged emails.
    Email ID: {email_id}
    Subject: {email_meta.get('subject', '')}
    Body: {email_meta.get('body', '')}
    Status: {pipeline_results.get('status')}
    Review Reason: {pipeline_results.get('review_reason')}
    Defect Fields: {pipeline_results.get('defect_fields')}
    
    SI Text: {si_text[:2000]}
    BL Text: {bl_text[:2000]}
    """

  model = genai.GenerativeModel(
      model_name="gemini-1.5-flash", system_instruction=system_prompt
  )

  response = model.generate_content(user_query)
  return response.text
