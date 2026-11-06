FROM cognee/cognee:main

# 叠加：本地 OCR（发票等固定版式）+ Office 文档 loader（docx/xlsx/pptx/pdf）
#
# rapidocr：scan-inbox.py 的 OCR_SCRIPT 与 docling 的 rapidocr 引擎都 import rapidocr 包
#           （rapidocr-onnxruntime 只提供 rapidocr_onnxruntime 模块，两者都不认）
# torchvision：docling 的表格结构模型 v2 直接 import 它，缺了连带文本层的 PDF 都转不了。
#           必须和 torch 同源：镜像里的 torch 是 2.14.1+cpu（CPU 版），
#           所以 torchvision 也得从 download.pytorch.org/whl/cpu 装 0.29.1+cpu；
#           装 PyPI 的 0.29.2（CUDA 构建）会报 "operator torchvision::nms does not exist"。
# docling-ibm-models：docling 的表格结构模型 v1 在 __init__ 里 import 它，
#           缺了 PDF 一进来就 ModuleNotFoundError（跟 do_table_structure 开不开无关）。
RUN /usr/local/bin/pip --python /app/.venv/bin/python install --no-cache-dir \
        "rapidocr>=3.9.1,<4" \
        "docling-ibm-models>=3.9.1,<5" \
        "docling-slim[convert-core,format-office,format-pdf,format-markdown]>=2.115,<3" \
 && /usr/local/bin/pip --python /app/.venv/bin/python install --no-cache-dir --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        "torchvision==0.29.1" \
 && /usr/local/bin/pip --python /app/.venv/bin/python uninstall -y opencv-python \
 && /usr/local/bin/pip --python /app/.venv/bin/python install --no-cache-dir \
        opencv-python-headless
