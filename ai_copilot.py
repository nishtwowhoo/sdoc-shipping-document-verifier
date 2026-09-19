import os
import openai

#initialize client // ensure openai api key environtment variable is set

client = openai.OpenAI()

def ask_hitl_assistant(
    user_query: str,
    email_id: str,
    email_meta: dict,
    si_text: str,
    bl_text: str,
    pipeline_results: dict,
) -> str:
    """Context-aware AI Copilot for Human-in-the-Loop inspection."""
    
    system_prompt = f"""
    You are an expert Shipping Operations AI Copilot assisting human operators in inspecting flagged emails.
    
    ### CONTEXT FOR EMAIL INSPECTION {email_id}:
    - Subject: {email_meta.get('subject', '')}
    - From: {email_meta.get('from', '')}
    - Email Body: {email_meta.get('body', '')}
    
    ### PIPELINE PREDICTION:
    - Status: {pipeline_results.get('status')}
    - Category: {pipeline_results.get('category')}
    - Review Reason: {pipeline_results.get('review_reason', '')}
    - Defect Fields: {pipeline_results.get('defect_fields', '')}
    
    ### ATTACHED DOCUMENTS:
    --- SHIPPING INSTRUCTION (SI) ---
    {si_text if si_text else "No SI document text found."}
    
    --- BILL OF LADING (BL) ---
    {bl_text if bl_text else "No BL document text found."}
    
    INSTRUCTIONS:
        Answer the human operator's questions accurately based on the source text above.
        If a field is missing or ambiguous, explain why and recommend whether the operator should mark it as OK, MISMATCH, or keep it in NEEDS_REVIEW.'
        """
        
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
        ],
        temperature=0.2,
        max_tokens=500
    )

    return response.choices[0].message.content