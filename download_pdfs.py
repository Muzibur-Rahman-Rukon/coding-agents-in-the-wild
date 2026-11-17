import os
import time
import urllib.request
import pandas as pd

# Load screening results
df = pd.read_csv("slr_screening_results.csv")

# Filter papers marked as Included / Full-Text Check needed by either reviewer
candidates = df[
    df["screening_result"].str.contains("Included", na=False)
    | df["scr_res"].str.contains("Included", na=False)
]

# Output directory
output_dir = "04-fulltext-pdfs"
os.makedirs(output_dir, exist_ok=True)

print(f"Starting download for {len(candidates)} candidate papers...")

success_count = 0
fail_count = 0

for idx, row in candidates.iterrows():
    arxiv_id = str(row["arxiv_id"]).strip()
    pdf_url = row.get("pdf_url")

    # Fallback to standard arXiv PDF URL if missing
    if pd.isna(pdf_url) or not str(pdf_url).startswith("http"):
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    # Destination filename: 04-fulltext-pdfs/2301.04991.pdf
    filename = os.path.join(output_dir, f"{arxiv_id}.pdf")

    # Skip if already downloaded
    if os.path.exists(filename):
        print(f"[SKIP] {arxiv_id}.pdf already exists.")
        success_count += 1
        continue

    try:
        req = urllib.request.Request(
            pdf_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as response, open(
            filename, "wb"
        ) as out_file:
            out_file.write(response.read())
        print(f"[SUCCESS] Downloaded {arxiv_id}.pdf")
        success_count += 1
        time.sleep(1)  # Polite delay to respect server rate limits
    except Exception as e:
        print(f"[FAILED] Could not download {arxiv_id}: {e}")
        fail_count += 1

print(
    f"\nDone! Successfully acquired: {success_count}, Failed: {fail_count}"
)