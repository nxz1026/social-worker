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

# 预热 docling 模型权重（~500 MB）：构建时跑一次真实转换，把布局/表格模型拉进镜像。
# 不预热的话模型落在容器可写层，docker compose up -d --force-recreate 一重建就丢，
# 下次转换得重新联网下载（构建时 HOME=/app，缓存落在 /app/.cache/huggingface）。
RUN /app/.venv/bin/python -c "from PIL import Image; Image.new('RGB', (900, 1200), 'white').save('/tmp/warmup.pdf')" \
 && /app/.venv/bin/python -c "from docling.document_converter import DocumentConverter; DocumentConverter().convert('/tmp/warmup.pdf')" \
 && rm -f /tmp/warmup.pdf

# 预热 cognee 自己的模型（~840 MB）：
#   Qdrant/bge-small-zh-v1.5   fastembed 的 ONNX 嵌入模型（配置里 EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
#                               是 cognee 侧的名字，fastembed 实际拉 Qdrant 那个）
#   fastino/gliner2.5-base-v1  gliner_demo 图谱抽取器（GRAPH_EXTRACTOR=gliner_demo）
#   BAAI/bge-small-zh-v1.5     只用到 tokenizer（460K）
# 不预热的话容器重建后首次 cognify 要联网下 840M。
RUN HF_HOME=/app/.cache/huggingface /app/.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-small-zh-v1.5', allow_patterns=['tokenizer.json', 'tokenizer_config.json']); snapshot_download('Qdrant/bge-small-zh-v1.5', allow_patterns=['config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json', 'model_optimized.onnx']); snapshot_download('fastino/gliner2.5-base-v1', allow_patterns=['config.json', 'tokenizer.json', 'tokenizer_config.json', 'model.safetensors']); print('cognee models warmed')"
