import glob
import json
import os
import fitz  # PyMuPDF
import openai
import pandas as pd

# -------------------------------------------------------------------
# Acquire API Key Safely
# -------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

if not OPENROUTER_API_KEY:
    raise RuntimeError("Set OPENROUTER_API_KEY in the environment before screening.")

# Initialize OpenAI Client pointing to OpenRouter
client = openai.OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "SLR_Screener",
    },
)

# You can use "openai/gpt-4o", "meta-llama/llama-3.3-70b-instruct", or "openrouter/free"
MODEL_NAME = "openai/gpt-4o"

SYSTEM_PROMPT = """
You are screening full-text software engineering papers for a systematic literature review.
Apply every criterion literally. Inclusion requires ALL four inclusion criteria to be true.
Exclude a paper if ANY exclusion criterion is true. If a criterion cannot be established from
the provided text, mark it Unclear and set the overall decision to Unclear rather than guessing.

Inclusion criteria:
IC1: The study collects original empirical data, such as a controlled experiment, developer
survey, repository mining, telemetry, or interview.
IC2: It evaluates at least one real-world commercial AI coding tool: Claude Code, GitHub Copilot,
Cursor, OpenAI Codex, or Copilot CLI.
IC3: Data collection or publication is from 2023-01-01 through 2026-10-19, inclusive.
IC4: The paper is written in English.

Exclusion criteria:
EC1: Research prototypes/benchmarks only, with no commercial tool evaluation.
EC2: Benchmark scores only (such as Pass@k), with no empirical analysis of developer usage,
code quality, or productivity.
EC3: Editorial, opinion piece, tutorial, or secondary review.
EC4: Full text is unavailable. A readable local PDF means EC4 is No.

Return ONLY a valid JSON object, with each criterion set to Yes, No, or Unclear:
{
    "decision": "Include", "Exclude", or "Unclear",
    "reason": "Brief evidence-based rationale referencing criteria",
    "tool_found": "Tool name or None",
    "method_found": "Study method",
    "ic1": "Yes/No/Unclear", "ic2": "Yes/No/Unclear",
    "ic3": "Yes/No/Unclear", "ic4": "Yes/No/Unclear",
    "ec1": "Yes/No/Unclear", "ec2": "Yes/No/Unclear",
    "ec3": "Yes/No/Unclear", "ec4": "Yes/No/Unclear",
    "publication_or_collection_date": "Date or Unclear",
    "evidence": "Short supporting quote or precise paper detail"
}
"""


def extract_method_text(pdf_path):
    """Extracts text from the first 5 pages of the PDF."""
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc[:5]:  # Title, abstract, & method sections
        text += page.get_text()
    return text


def screen_pdf(pdf_path):
    text = extract_method_text(pdf_path)
    prompt = f"PDF file: {os.path.basename(pdf_path)}\nPaper content:\n{text[:12000]}\n\nApply all criteria and return ONLY valid JSON."

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=500,
    )

    raw_content = response.choices[0].message.content.strip()

    # Clean potential markdown formatting ```json ... ```
    if raw_content.startswith("```"):
        raw_content = raw_content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    return json.loads(raw_content)


if __name__ == "__main__":
    pdf_files = glob.glob("04-fulltext-pdfs/*.pdf")
    os.makedirs("03-screening", exist_ok=True)
    results = []

    print(
        f"Found {len(pdf_files)} PDFs in 04-fulltext-pdfs/. Starting screening with {MODEL_NAME}...\n"
    )

    for pdf in pdf_files:
        pdf_name = os.path.basename(pdf)
        print(f"Screening: {pdf_name}...")
        try:
            res = screen_pdf(pdf)
            res["pdf"] = pdf_name
            results.append(res)
            print(
                f"  -> {res.get('decision')}: {res.get('reason')} [{res.get('tool_found')}]"
            )
        except Exception as e:
            print(f"  -> Error processing {pdf_name}: {e}")
            results.append(
                {
                    "pdf": pdf_name,
                    "decision": "Exclude" if isinstance(e, (fitz.FileDataError, fitz.EmptyFileError)) else "Error",
                    "reason": f"EC4: full text unavailable ({e})" if isinstance(e, (fitz.FileDataError, fitz.EmptyFileError)) else str(e),
                    "tool_found": "N/A",
                    "method_found": "N/A",
                }
            )

    df_results = pd.DataFrame(results)
    output_file = "03-screening/agent_fulltext_screening_results.csv"
    df_results.to_csv(output_file, index=False)
    print(f"\n✅ Agent Screening Complete! Results saved to: {output_file}")