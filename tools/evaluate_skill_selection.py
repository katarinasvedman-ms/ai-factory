"""Local, opt-in selection evaluation; no skill publication or Azure deployment."""

from pathlib import Path

from ai_factory.skill_evaluation import main

if __name__ == "__main__":
    main(Path(__file__).resolve().parents[1])
