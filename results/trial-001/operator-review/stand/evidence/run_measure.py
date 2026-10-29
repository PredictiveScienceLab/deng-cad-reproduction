from pathlib import Path
import sys,json
BASE=Path('results/trial-001/operator-review/stand/evidence')
EVALUATOR=Path('@ORIGINAL_PROJECT@/quality/evaluator')
SNAPSHOT=Path('results/trial-001/output')
sys.path.insert(0,str(EVALUATOR))
from execution import run_isolated
result=run_isolated((BASE/'measure.py').read_text(),work=BASE/'work',scratch=BASE/'scratch',control=BASE/'control',read_roots=[SNAPSHOT,EVALUATOR],seconds=60)
print(json.dumps(result,indent=2))
