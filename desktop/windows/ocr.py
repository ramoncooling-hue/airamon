"""Bundled offline Chinese/English OCR; image or page-wise scanned PDF."""

import argparse
import json
from pathlib import Path
import sys


def recognize(engine, image):
    result, _ = engine(image)
    return '\n'.join(row[1] for row in (result or []) if row[1].strip())


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--image', type=Path)
    group.add_argument('--pdf', type=Path)
    args = parser.parse_args()
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    if args.image:
        print(recognize(engine, str(args.image)))
        return
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(args.pdf))
    pages = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=2.0)
                try:
                    text = recognize(engine, bitmap.to_pil())
                finally:
                    bitmap.close()
            finally:
                page.close()
            if text.strip():
                pages.append({'page': index + 1, 'text': text.strip()})
            print(json.dumps({'event': 'progress', 'page': index + 1, 'total': len(document)}),
                  file=sys.stderr, flush=True)
    finally:
        document.close()
    print(json.dumps(pages, ensure_ascii=False))


if __name__ == '__main__':
    main()
