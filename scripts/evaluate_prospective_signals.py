import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from app.db import SessionLocal
from app.services.prospective import evaluate_prospective_signals

def main():
    p = argparse.ArgumentParser(description="Evaluate only genuine prospective M8 signals.")
    p.add_argument("--hypothesis-id", required=True)
    with SessionLocal() as db: result = evaluate_prospective_signals(db, **vars(p.parse_args()))
    print(json.dumps(result, indent=2, sort_keys=True))
if __name__ == "__main__": main()
