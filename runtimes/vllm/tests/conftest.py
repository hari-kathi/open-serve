import os
import sys

# Make `import model_source` resolve to runtimes/vllm/model_source.py
# regardless of the pytest invocation directory. Mirrors the image
# layout, where the module sits directly on PYTHONPATH (/home/ray).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
