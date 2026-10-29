from pathlib import Path
import sys,json
BASE=Path('results/trial-001/operator-review/stand/evidence')
E=Path('@ORIGINAL_PROJECT@/quality/evaluator')
S=Path('results/trial-001/output')
sys.path.insert(0,str(E))
from execution import run_isolated
for name in ('work-contact','scratch-contact','control-contact'):(BASE/name).mkdir(exist_ok=True)
result=run_isolated((BASE/'contact_and_holes.py').read_text(),work=BASE/'work-contact',scratch=BASE/'scratch-contact',control=BASE/'control-contact',read_roots=[S,E],seconds=60)
print(json.dumps(result,indent=2))
