from ingestion.embedder import get_collection
from analysis.screener import extract_rows, run_pipeline
from config import ANTHROPIC_API_KEY

col = get_collection()
res = col.get(where={"type": "email_attachment_excel"}, limit=200, include=["documents", "metadatas"])
chunks = [{"text": d} for d in res["documents"]] if res and res.get("documents") else []
print(f"Chunks found: {len(chunks)}")

rows = extract_rows(chunks)
print(f"Rows extracted: {len(rows)}")
if rows:
    print(f"Sample row: {rows[0][:200]}")

result = run_pipeline(chunks, api_key=ANTHROPIC_API_KEY, top_n=30)
print(f"Total: {result['total']}")
print(f"Stage 1 eliminated: {result['stage1_eliminated']}")
print(f"Stage 2 cut: {result['stage2_eliminated']}")
print(f"Finalists: {len(result['finalists'])}")
print(f"Rules generated: {len(result['hard_rules'])}")
print(f"Dimensions generated: {len(result['scoring_dimensions'])}")

if result["hard_rules"]:
    print("\nStage 0 rules:")
    for r in result["hard_rules"]:
        print(f"  - {r['description']}")

if result["scoring_dimensions"]:
    print("\nStage 0 dimensions:")
    for d in result["scoring_dimensions"]:
        print(f"  - {d['description']} (max {d['max_points']} pts)")