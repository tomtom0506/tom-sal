# vendor

ספריות שנשמרות בתוך האתר ולא נטענות מ-CDN חיצוני. כל הקריאה של חשבונית נעשית בדפדפן. כל הספריות ברישיון Apache-2.0.

| קובץ | מקור | גרסה |
|---|---|---|
| ocr/tesseract.min.js, ocr/worker.min.js | npm tesseract.js | 5.1.1 |
| ocr/tesseract-core-*lstm.wasm.js | npm tesseract.js-core | 5.1.1 |
| ocr/eng.traineddata.gz | github.com/tesseract-ocr/tessdata_fast (eng), gzip | main |
| pdfjs/pdf.min.mjs, pdfjs/pdf.worker.min.mjs | npm pdfjs-dist, legacy build | 4.10.38 |

pdf.js מעל 4.2.67 בגלל CVE-2024-4367, ובנוסף נטען עם isEvalSupported: false.

```
8f04aa0cc81e7bde33f80e92fa01a7a665f0b4884d098acf5de9c7104a11dfaa  ocr/tesseract-core-lstm.wasm.js
ce20eda9533cbed1e6c2b4276fbae1e0adc61b6754b5513084be601787b457cf  ocr/tesseract-core-simd-lstm.wasm.js
a8e29918d098b2b06e1012bdaeffb4aec0445c5d5654709023e0bd1f442a80e8  ocr/tesseract.min.js
aca1229639fc9907d86f96e825955a2b7c5716d17f3bc3acd71f9c7ab66181fc  ocr/worker.min.js
e5d09a4b9ca0dae6251e9e1f2f834ec99cdb0941f3e8425a83a9a1862c5416d8  ocr/eng.traineddata.gz
44ec6f011027ee77791386b66c14876a5fc29e20bf0433c07c6726fff7212b72  pdfjs/pdf.min.mjs
bd88805178a26c729db8c0107a5b630cb900ec070f4d8c7529a3e45530afd41d  pdfjs/pdf.worker.min.mjs
```
