import sys,json
from pathlib import Path
root=Path('results/trial-001/operator-review/stage23')
evalroot=Path('@ORIGINAL_PROJECT@/quality/evaluator')
snapshot=Path('results/trial-001/output')
sys.path.insert(0,str(evalroot));from execution import run_isolated
result=run_isolated((root/sys.argv[1]).read_text(),work=root/'work',scratch=root/'scratch',control=root/'control',read_roots=[snapshot,evalroot],seconds=60)
(root/(sys.argv[1]+'.result.json')).write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
