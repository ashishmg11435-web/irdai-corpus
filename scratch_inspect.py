import json

with open(r'D:\Agentic RAG\irda_scraper\agentic-rag-deepeval-14b-v3.ipynb', 'r', encoding='utf-8') as f:
    nb = json.load(f)

for i in range(10):
    cell = nb['cells'][i]
    source = ''.join(cell.get('source', []))
    ctype = cell["cell_type"]
    print(f"=== Cell {i} (type={ctype}) ===")
    print(source[:5000])
    print()
