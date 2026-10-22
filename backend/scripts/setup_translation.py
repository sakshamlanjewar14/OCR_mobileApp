"""Explicit one-time download; normal OCR never downloads or sends text online."""
from pathlib import Path
from urllib.request import urlopen
import shutil

REVISION = "55c2e61bbf05dfb8d7abccdc3fae6fc8512fd636"
FILES = ("README.md", "config.json", "generation_config.json", "sentencepiece.bpe.model",
         "special_tokens_map.json", "tokenizer_config.json", "vocab.json", "pytorch_model.bin")


def main():
    target = Path(__file__).resolve().parents[1] / "ocr-runtime" / "m2m100_418M"
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        temporary = target / (name + ".partial")
        url = f"https://huggingface.co/facebook/m2m100_418M/resolve/{REVISION}/{name}"
        with urlopen(url, timeout=600) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(target / name)
        print(f"Installed {name}")
    (target / "REVISION.txt").write_text(REVISION, encoding="utf-8")
    print(f"Set TRANSLATION_MODEL_DIR={target.as_posix()} in backend/.env, then restart the API.")


if __name__ == "__main__":
    main()
