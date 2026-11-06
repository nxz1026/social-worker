FROM cognee/cognee:main

# 叠加：本地 OCR（发票等固定版式）+ Office 文档 loader（docx/xlsx/pptx/pdf）
RUN /usr/local/bin/pip --python /app/.venv/bin/python install --no-cache-dir \
        "rapidocr-onnxruntime>=1.3.0,<2" \
        "docling-slim[convert-core,format-office,format-pdf,format-markdown]>=2.115,<3" \
 && /usr/local/bin/pip --python /app/.venv/bin/python uninstall -y opencv-python \
 && /usr/local/bin/pip --python /app/.venv/bin/python install --no-cache-dir \
        opencv-python-headless
