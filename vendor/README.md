# vendor/

Bundled binaries for Iris.

## tesseract/

Iris prefers a portable Tesseract here at `vendor/tesseract/tesseract.exe`. If absent, falls back to:
1. `tesseract` on PATH
2. `C:/Program Files/Tesseract-OCR/tesseract.exe`
3. `C:/Program Files (x86)/Tesseract-OCR/tesseract.exe`
4. `%LOCALAPPDATA%/Programs/Tesseract-OCR/tesseract.exe`

Default install location used by `winget install UB-Mannheim.TesseractOCR` is option 2.

To make Iris truly portable later, copy the install dir contents into `vendor/tesseract/` and Iris will prefer the bundled copy.
