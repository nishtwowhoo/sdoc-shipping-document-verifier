FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir pypdf python-docx openpyxl
COPY . .
# HITL defaults (secrets passed at runtime with -e, never baked in):
ENV GEMINI_MODEL=gemini-3.6-flash \
    SUPABASE_TABLE=hitl_reviews \
    HITL_LOCAL_FILE=hitl_overrides.json
EXPOSE 8081
CMD ["python", "webui.py", "--host", "0.0.0.0", "--port", "8081", "--data-dir", "."]
